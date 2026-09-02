from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from ipaddress import IPv4Interface, IPv4Network
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from nslab.planner import (
    BridgeVlanPlan,
    IPInterface,
    LinkPlan,
    NetemPlan,
    NodeKind,
    NodePlan,
    PolicyRulePlan,
    RoutePlan,
    TopologyPlan,
    VlanDevicePlan,
    VrfDevicePlan,
    node_interface_addresses,
    node_interface_master,
    node_interface_route_table,
)


@dataclass(frozen=True, slots=True)
class InterfaceInventory:
    """Observed state for one interface inside a network namespace."""

    name: str
    kind: str
    ifindex: int | None
    master: str | None
    mtu: int
    up: bool
    addresses: tuple[IPInterface, ...] = ()
    stp: bool | None = None
    vlan_filtering: bool | None = None
    bridge_priority: int | None = None
    path_cost: int | None = None
    port_priority: int | None = None
    bridge_vlans: tuple[BridgeVlanPlan, ...] = ()
    netem: NetemPlan | None = None
    link_id: str | None = None
    parent: str | None = None
    vlan_id: int | None = None
    vrf_table: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "addresses", tuple(self.addresses))
        object.__setattr__(self, "bridge_vlans", tuple(self.bridge_vlans))


@dataclass(frozen=True, slots=True)
class NamespaceInventory:
    """Observed state for one planned node namespace."""

    node: str
    kind: NodeKind
    namespace: str
    exists: bool
    interfaces: Mapping[str, InterfaceInventory]
    routes: tuple[RoutePlan, ...]
    sysctls: Mapping[str, int]
    rules: tuple[PolicyRulePlan, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "interfaces",
            MappingProxyType(dict(self.interfaces)),
        )
        object.__setattr__(self, "routes", tuple(self.routes))
        object.__setattr__(self, "sysctls", MappingProxyType(dict(self.sysctls)))
        object.__setattr__(self, "rules", tuple(self.rules))


@dataclass(frozen=True, slots=True)
class LiveInventory:
    """Observed state for the namespaces owned by one topology plan."""

    namespaces: Mapping[str, NamespaceInventory]
    root_interfaces: Mapping[str, InterfaceInventory] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "namespaces",
            MappingProxyType(dict(self.namespaces)),
        )
        object.__setattr__(
            self,
            "root_interfaces",
            MappingProxyType(dict(self.root_interfaces)),
        )


@dataclass(frozen=True, slots=True)
class ExecResult:
    """The complete result of a direct argv execution in a namespace."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))


@runtime_checkable
class NetworkBackend(Protocol):
    """Backend-neutral network operations consumed by lifecycle services."""

    def create_namespace(self, node: NodePlan) -> None: ...

    def delete_namespace(self, namespace: str) -> None: ...

    def create_bridge(self, node: NodePlan) -> None: ...

    def create_veth(self, link: LinkPlan) -> None: ...

    def configure_node(self, node: NodePlan, plan: TopologyPlan) -> None: ...

    def start_routing(self, plan: TopologyPlan) -> None: ...

    def stop_routing(self, plan: TopologyPlan) -> None: ...

    def routing_ready(self, plan: TopologyPlan) -> bool: ...

    def inventory(self, plan: TopologyPlan) -> LiveInventory: ...

    def execute(
        self,
        namespace: str,
        argv: Sequence[str],
        *,
        capture_output: bool = True,
    ) -> ExecResult: ...


@dataclass(frozen=True, slots=True)
class _ExpectedInterface:
    kind: str
    master: str | None
    mtu: int | None
    up: bool
    addresses: tuple[IPInterface, ...]
    stp: bool | None = None
    vlan_filtering: bool | None = None
    bridge_priority: int | None = None
    path_cost: int | None = None
    port_priority: int | None = None
    bridge_vlans: tuple[BridgeVlanPlan, ...] = ()
    netem: NetemPlan | None = None
    parent: str | None = None
    vlan_id: int | None = None
    vrf_table: int | None = None


def expected_bridge_port_vlans(node: NodePlan, interface: str) -> tuple[BridgeVlanPlan, ...]:
    if node.kind != "bridge" or not node.vlan_filtering:
        return ()
    port = node.bridge_ports.get(interface)
    if port is not None and port.vlans:
        return port.vlans
    return (BridgeVlanPlan(vid=1, pvid=True, untagged=True),)


def expected_routes(node: NodePlan) -> tuple[RoutePlan, ...]:
    """Return deterministic connected and declared routes across managed tables."""

    routes = [
        RoutePlan(
            dst=IPv4Network("127.0.0.0/8"),
            via=None,
            dev="lo",
        )
    ]
    routes.extend(
        RoutePlan(
            dst=address.network,
            via=None,
            dev=interface,
            table=node_interface_route_table(node, interface),
        )
        for interface, addresses in node_interface_addresses(node).items()
        for address in addresses
    )
    routes.extend(node.routes)
    return tuple(dict.fromkeys(routes))


def expected_main_table_routes(node: NodePlan) -> tuple[RoutePlan, ...]:
    """Compatibility alias for callers that predate managed VRF tables."""

    return expected_routes(node)


def _expected_interfaces(node: NodePlan, plan: TopologyPlan) -> dict[str, _ExpectedInterface]:
    expected = {
        "lo": _ExpectedInterface(
            kind="loopback",
            master=None,
            mtu=None,
            up=True,
            addresses=(IPv4Interface("127.0.0.1/8"),),
        )
    }

    if node.kind == "bridge":
        assert node.bridge_name is not None
        expected[node.bridge_name] = _ExpectedInterface(
            kind="bridge",
            master=None,
            mtu=None,
            up=True,
            addresses=node.interfaces.get(node.bridge_name, ()),
            stp=node.stp,
            vlan_filtering=node.vlan_filtering,
            bridge_priority=node.bridge_priority,
        )

    for link in plan.links:
        for endpoint in (link.left, link.right):
            if endpoint.namespace != node.namespace:
                continue
            port = node.bridge_ports.get(endpoint.interface)
            expected[endpoint.interface] = _ExpectedInterface(
                kind="veth",
                master=(
                    node.bridge_name
                    if node.kind == "bridge"
                    else node_interface_master(node, endpoint.interface)
                ),
                mtu=link.mtu,
                up=True,
                addresses=node.interfaces.get(endpoint.interface, ()),
                path_cost=None if port is None else port.path_cost,
                port_priority=None if port is None else port.priority,
                bridge_vlans=expected_bridge_port_vlans(node, endpoint.interface),
                netem=link.netem,
            )

    for device in node.devices.values():
        if isinstance(device, VlanDevicePlan):
            expected[device.name] = _ExpectedInterface(
                kind="vlan",
                master=node_interface_master(node, device.name),
                mtu=None,
                up=True,
                addresses=device.addresses,
                parent=device.link,
                vlan_id=device.vlan_id,
            )
        else:
            assert isinstance(device, VrfDevicePlan)
            expected[device.name] = _ExpectedInterface(
                kind="vrf",
                master=None,
                mtu=None,
                up=True,
                addresses=(),
                vrf_table=device.table,
            )

    return expected


def _interfaces_match(
    expected: Mapping[str, _ExpectedInterface],
    actual: Mapping[str, InterfaceInventory],
) -> bool:
    if set(actual) != set(expected):
        return False

    for name, desired in expected.items():
        observed = actual[name]
        if observed.name != name:
            return False
        if observed.kind != desired.kind:
            return False
        if observed.master != desired.master:
            return False
        if desired.mtu is not None and observed.mtu != desired.mtu:
            return False
        if observed.up is not desired.up:
            return False
        if frozenset(observed.addresses) != frozenset(desired.addresses):
            return False
        if observed.stp is not desired.stp:
            return False
        if observed.vlan_filtering is not desired.vlan_filtering:
            return False
        if (
            desired.bridge_priority is not None
            and observed.bridge_priority != desired.bridge_priority
        ):
            return False
        if desired.path_cost is not None and observed.path_cost != desired.path_cost:
            return False
        if desired.port_priority is not None and observed.port_priority != desired.port_priority:
            return False
        if observed.bridge_vlans != desired.bridge_vlans:
            return False
        if observed.netem != desired.netem:
            return False
        if observed.parent != desired.parent:
            return False
        if observed.vlan_id != desired.vlan_id:
            return False
        if observed.vrf_table != desired.vrf_table:
            return False

    return True


def _routes_match(
    node: NodePlan,
    desired: Sequence[RoutePlan],
    actual: Sequence[RoutePlan],
) -> bool:
    actual_routes = frozenset(actual)
    desired_routes = frozenset(desired)
    if node.routing is not None:
        # OSPF/BGP legitimately add and withdraw routes asynchronously. Static
        # routes remain managed by nslab; learned routes are intentionally extra.
        return desired_routes <= actual_routes
    return actual_routes == desired_routes


def _declared_sysctls_match(desired: Mapping[str, int], actual: Mapping[str, int]) -> bool:
    return all(actual.get(key) == value for key, value in desired.items())


def _links_match(plan: TopologyPlan, inventory: LiveInventory) -> bool:
    observed_groups: dict[str, set[tuple[str, str]]] = {}
    for namespace, namespace_inventory in inventory.namespaces.items():
        for name, interface in namespace_inventory.interfaces.items():
            if interface.kind != "veth":
                continue
            link_id = interface.link_id
            if not isinstance(link_id, str) or not link_id:
                return False
            observed_groups.setdefault(link_id, set()).add((namespace, name))

    seen_link_ids: set[str] = set()
    for link in plan.links:
        endpoints = (link.left, link.right)
        observed_ids: list[str] = []
        expected_group = {(endpoint.namespace, endpoint.interface) for endpoint in endpoints}
        for endpoint in endpoints:
            endpoint_namespace = inventory.namespaces.get(endpoint.namespace)
            if endpoint_namespace is None:
                return False
            observed = endpoint_namespace.interfaces.get(endpoint.interface)
            if observed is None or not isinstance(observed.link_id, str):
                return False
            if not observed.link_id:
                return False
            observed_ids.append(observed.link_id)

        link_id = observed_ids[0]
        if observed_ids[1] != link_id or link_id in seen_link_ids:
            return False
        if observed_groups.get(link_id) != expected_group:
            return False
        seen_link_ids.add(link_id)

    return True


def recorded_link_ids_match_inventory(
    plan: TopologyPlan,
    inventory: LiveInventory,
    recorded_interfaces: Mapping[str, object],
) -> bool:
    """Return whether recorded veth identities exactly match current observations.

    Missing and malformed schema-1 identity data is deliberately inconclusive and
    returns ``False``. Callers that need to distinguish invalid state from legacy
    unknown state must validate the snapshot structure separately.
    """

    if not _links_match(plan, inventory):
        return False

    seen_link_ids: set[str] = set()
    for link in plan.links:
        recorded_ids: list[str] = []
        for endpoint in (link.left, link.right):
            ownership = recorded_interfaces.get(f"{endpoint.node}:{endpoint.interface}")
            if not isinstance(ownership, Mapping):
                return False
            link_id = ownership.get("link_id")
            if not isinstance(link_id, str) or not link_id:
                return False
            observed_namespace = inventory.namespaces.get(endpoint.namespace)
            if observed_namespace is None:
                return False
            observed = observed_namespace.interfaces.get(endpoint.interface)
            if observed is None or observed.link_id != link_id:
                return False
            recorded_ids.append(link_id)

        link_id = recorded_ids[0]
        if recorded_ids[1] != link_id or link_id in seen_link_ids:
            return False
        seen_link_ids.add(link_id)

    return True


def inventory_matches_plan(plan: TopologyPlan, inventory: LiveInventory) -> bool:
    """Return whether live state satisfies the plan, excluding volatile ifindexes.

    Expected routes from the main and declared VRF tables are compared as an
    order-independent semantic set. Sysctls not declared by the manifest remain
    intentionally unconstrained.
    """

    if inventory.root_interfaces:
        return False

    planned_namespaces = {node.namespace for node in plan.nodes.values()}
    if set(inventory.namespaces) != planned_namespaces:
        return False

    for node in plan.nodes.values():
        observed = inventory.namespaces[node.namespace]
        if not observed.exists:
            return False
        if observed.node != node.name:
            return False
        if observed.kind != node.kind:
            return False
        if observed.namespace != node.namespace:
            return False
        if not _interfaces_match(_expected_interfaces(node, plan), observed.interfaces):
            return False
        if not _routes_match(
            node,
            expected_routes(node),
            observed.routes,
        ):
            return False
        if frozenset(observed.rules) != frozenset(node.rules):
            return False
        if not _declared_sysctls_match(node.sysctls, observed.sysctls):
            return False

    return _links_match(plan, inventory)
