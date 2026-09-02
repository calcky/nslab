from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from ipaddress import IPv4Interface

from nslab.backend.base import (
    ExecResult,
    InterfaceInventory,
    LiveInventory,
    NamespaceInventory,
    expected_bridge_port_vlans,
    expected_main_table_routes,
)
from nslab.errors import NslabError
from nslab.planner import (
    EndpointPlan,
    LinkPlan,
    NetemPlan,
    NodeKind,
    NodePlan,
    RoutePlan,
    TopologyPlan,
)


@dataclass
class _FakeNamespaceState:
    node: str
    kind: NodeKind
    namespace: str
    interfaces: dict[str, InterfaceInventory]
    routes: tuple[RoutePlan, ...]
    sysctls: dict[str, int]


type _EndpointKey = tuple[str, str]


def _bridge_resource(node: NodePlan) -> str:
    bridge_name = node.bridge_name
    if bridge_name is None:
        return node.namespace
    return f"{node.namespace}:{bridge_name}"


def _link_resource(link: LinkPlan) -> str:
    return f"{link.left.temporary_name}<->{link.right.temporary_name}"


def _resource_error(code: str, operation: str, resource: str) -> NslabError:
    if code == "RESOURCE_EXISTS":
        message = f"network resource already exists: {resource}"
    else:
        message = f"network resource is missing: {resource}"
    return NslabError(
        code=code,
        message=message,
        details={"operation": operation, "resource": resource},
    )


class FakeNetworkBackend:
    """Deterministic in-memory backend used by unit and lifecycle tests."""

    def __init__(
        self,
        fail_on_call: int | None = None,
        execute_result: ExecResult | None = None,
    ) -> None:
        self.fail_on_call = fail_on_call
        self.execute_result = execute_result
        self.calls: list[tuple[str, str]] = []
        self.execute_requests: list[tuple[str, tuple[str, ...], bool]] = []
        self.namespaces: dict[str, _FakeNamespaceState] = {}
        self.root_interfaces: dict[str, InterfaceInventory] = {}
        self._call_count = 0
        self._next_link_id = 1
        self._next_ifindex: dict[str, int] = {}
        self._veth_peers: dict[_EndpointKey, _EndpointKey] = {}
        self.routing_deployments: set[str] = set()

    def _record(self, operation: str, resource: str) -> None:
        self._call_count += 1
        self.calls.append((operation, resource))
        if self._call_count == self.fail_on_call:
            raise NslabError(
                code="BACKEND_FAILURE",
                message=f"injected backend failure during {operation}: {resource}",
                details={
                    "call": self._call_count,
                    "operation": operation,
                    "resource": resource,
                },
            )

    def _allocate_ifindex(self, namespace: str) -> int:
        ifindex = self._next_ifindex.get(namespace, 1)
        self._next_ifindex[namespace] = ifindex + 1
        return ifindex

    def _require_namespace(
        self, namespace: str, operation: str, resource: str
    ) -> _FakeNamespaceState:
        state = self.namespaces.get(namespace)
        if state is None:
            raise _resource_error("RESOURCE_MISSING", operation, resource)
        return state

    def _insert_interface(
        self,
        namespace: str,
        interface: InterfaceInventory,
    ) -> None:
        state = self.namespaces[namespace]
        state.interfaces[interface.name] = interface

    def create_namespace(self, node: NodePlan) -> None:
        resource = node.namespace
        self._record("create_namespace", resource)
        if resource in self.namespaces:
            raise _resource_error("RESOURCE_EXISTS", "create_namespace", resource)

        self._next_ifindex[resource] = 1
        loopback = InterfaceInventory(
            name="lo",
            kind="loopback",
            ifindex=self._allocate_ifindex(resource),
            master=None,
            mtu=65536,
            up=True,
            addresses=(IPv4Interface("127.0.0.1/8"),),
        )
        self.namespaces[resource] = _FakeNamespaceState(
            node=node.name,
            kind=node.kind,
            namespace=resource,
            interfaces={"lo": loopback},
            routes=expected_main_table_routes(node)[:1],
            sysctls={},
        )

    def delete_namespace(self, namespace: str) -> None:
        self._record("delete_namespace", namespace)
        endpoints = tuple(endpoint for endpoint in self._veth_peers if endpoint[0] == namespace)
        for endpoint in endpoints:
            peer = self._veth_peers.pop(endpoint, None)
            if peer is None:
                continue
            self._veth_peers.pop(peer, None)
            peer_state = self.namespaces.get(peer[0])
            if peer_state is not None:
                peer_state.interfaces.pop(peer[1], None)
                peer_state.routes = tuple(
                    route for route in peer_state.routes if route.dev != peer[1]
                )

        self.namespaces.pop(namespace, None)
        self._next_ifindex.pop(namespace, None)

    def create_bridge(self, node: NodePlan) -> None:
        resource = _bridge_resource(node)
        self._record("create_bridge", resource)
        state = self._require_namespace(node.namespace, "create_bridge", resource)
        bridge_name = node.bridge_name
        if node.kind != "bridge" or bridge_name is None:
            raise NslabError(
                code="BACKEND_FAILURE",
                message=f"bridge operation requires a bridge node: {node.name}",
                details={"operation": "create_bridge", "resource": resource},
            )
        if bridge_name in state.interfaces:
            raise _resource_error("RESOURCE_EXISTS", "create_bridge", resource)

        self._insert_interface(
            node.namespace,
            InterfaceInventory(
                name=bridge_name,
                kind="bridge",
                ifindex=self._allocate_ifindex(node.namespace),
                master=None,
                mtu=1500,
                up=False,
                stp=node.stp,
                vlan_filtering=node.vlan_filtering,
                bridge_priority=node.bridge_priority,
            ),
        )

    def create_veth(self, link: LinkPlan) -> None:
        resource = _link_resource(link)
        self._record("create_veth", resource)
        left_state = self._require_namespace(link.left.namespace, "create_veth", resource)
        right_state = self._require_namespace(link.right.namespace, "create_veth", resource)
        for state, endpoint in ((left_state, link.left), (right_state, link.right)):
            if endpoint.interface in state.interfaces:
                raise _resource_error("RESOURCE_EXISTS", "create_veth", resource)

        link_id = f"fake-link-{self._next_link_id}"
        self._next_link_id += 1
        self._insert_veth_endpoint(link.left, link.mtu, link.netem, link_id)
        self._insert_veth_endpoint(link.right, link.mtu, link.netem, link_id)
        left_key = (link.left.namespace, link.left.interface)
        right_key = (link.right.namespace, link.right.interface)
        self._veth_peers[left_key] = right_key
        self._veth_peers[right_key] = left_key

    def _insert_veth_endpoint(
        self,
        endpoint: EndpointPlan,
        mtu: int,
        netem: NetemPlan | None,
        link_id: str,
    ) -> None:
        self._insert_interface(
            endpoint.namespace,
            InterfaceInventory(
                name=endpoint.interface,
                kind="veth",
                ifindex=self._allocate_ifindex(endpoint.namespace),
                master=None,
                mtu=mtu,
                up=False,
                netem=netem,
                link_id=link_id,
            ),
        )

    def configure_node(self, node: NodePlan, plan: TopologyPlan) -> None:
        resource = node.namespace
        self._record("configure_node", resource)
        state = self._require_namespace(resource, "configure_node", resource)
        interfaces = dict(state.interfaces)

        loopback = interfaces.get("lo")
        if loopback is None:
            raise _resource_error("RESOURCE_MISSING", "configure_node", f"{resource}:lo")
        interfaces["lo"] = replace(loopback, up=True)

        if node.kind == "bridge":
            bridge_name = node.bridge_name
            assert bridge_name is not None
            bridge = interfaces.get(bridge_name)
            if bridge is None:
                raise _resource_error(
                    "RESOURCE_MISSING",
                    "configure_node",
                    f"{resource}:{bridge_name}",
                )
            interfaces[bridge_name] = replace(
                bridge,
                master=None,
                up=True,
                addresses=node.interfaces.get(bridge_name, ()),
                stp=node.stp,
                vlan_filtering=node.vlan_filtering,
                bridge_priority=node.bridge_priority,
            )

        for link in plan.links:
            for endpoint in (link.left, link.right):
                if endpoint.namespace != resource:
                    continue
                interface = interfaces.get(endpoint.interface)
                if interface is None:
                    raise _resource_error(
                        "RESOURCE_MISSING",
                        "configure_node",
                        f"{resource}:{endpoint.interface}",
                    )
                port = node.bridge_ports.get(endpoint.interface)
                interfaces[endpoint.interface] = replace(
                    interface,
                    master=node.bridge_name if node.kind == "bridge" else None,
                    mtu=link.mtu,
                    up=True,
                    addresses=node.interfaces.get(endpoint.interface, ()),
                    path_cost=None if port is None else port.path_cost,
                    port_priority=None if port is None else port.priority,
                    bridge_vlans=expected_bridge_port_vlans(node, endpoint.interface),
                    netem=link.netem,
                )

        state.interfaces = interfaces
        state.routes = expected_main_table_routes(node)
        state.sysctls = dict(node.sysctls)

    def start_routing(self, plan: TopologyPlan) -> None:
        if not any(node.routing is not None for node in plan.nodes.values()):
            return
        self._record("start_routing", plan.name)
        self.routing_deployments.add(plan.name)

    def stop_routing(self, plan: TopologyPlan) -> None:
        if not any(node.routing is not None for node in plan.nodes.values()):
            return
        self._record("stop_routing", plan.name)
        self.routing_deployments.discard(plan.name)

    def routing_ready(self, plan: TopologyPlan) -> bool:
        if not any(node.routing is not None for node in plan.nodes.values()):
            return True
        return plan.name in self.routing_deployments

    def inventory(self, plan: TopologyPlan) -> LiveInventory:
        self._record("inventory", plan.name)
        namespaces: dict[str, NamespaceInventory] = {}
        for node in plan.nodes.values():
            state = self.namespaces.get(node.namespace)
            if state is None:
                namespace_inventory = NamespaceInventory(
                    node=node.name,
                    kind=node.kind,
                    namespace=node.namespace,
                    exists=False,
                    interfaces={},
                    routes=(),
                    sysctls={},
                )
            else:
                namespace_inventory = NamespaceInventory(
                    node=state.node,
                    kind=state.kind,
                    namespace=state.namespace,
                    exists=True,
                    interfaces=dict(state.interfaces),
                    routes=tuple(state.routes),
                    sysctls=dict(state.sysctls),
                )
            namespaces[node.namespace] = namespace_inventory
        return LiveInventory(
            namespaces=namespaces,
            root_interfaces=dict(self.root_interfaces),
        )

    def execute(
        self,
        namespace: str,
        argv: Sequence[str],
        *,
        capture_output: bool = True,
    ) -> ExecResult:
        self._record("execute", namespace)
        self._require_namespace(namespace, "execute", namespace)
        immutable_argv = tuple(argv)
        self.execute_requests.append((namespace, immutable_argv, capture_output))
        if self.execute_result is None:
            return ExecResult(immutable_argv, 0, "", "")
        return ExecResult(
            immutable_argv,
            self.execute_result.returncode,
            self.execute_result.stdout if capture_output else "",
            self.execute_result.stderr if capture_output else "",
        )
