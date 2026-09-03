from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from ipaddress import IPv4Interface, IPv4Network
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from nslab.manifest import PIM_REGISTER_INTERFACE_NAME
from nslab.planner import (
    BondDevicePlan,
    BridgeVlanPlan,
    DummyDevicePlan,
    GeneveDevicePlan,
    GreDevicePlan,
    IPAddress,
    IPInterface,
    IpipDevicePlan,
    IpvlanDevicePlan,
    LinkPlan,
    MacvlanDevicePlan,
    NeighborPlan,
    NetemPlan,
    NodeKind,
    NodePlan,
    PolicyRulePlan,
    QdiscPlan,
    RoutePlan,
    TbfPlan,
    TopologyPlan,
    VlanDevicePlan,
    VrfDevicePlan,
    VxlanDevicePlan,
    bond_device_mtu,
    dummy_device_mtu,
    geneve_device_mtu,
    gre_device_mtu,
    ipip_device_mtu,
    ipvlan_device_mtu,
    macvlan_device_mtu,
    node_interface_addresses,
    node_interface_master,
    node_interface_route_table,
    vxlan_device_mtu,
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
    mac: str | None = None
    stp: bool | None = None
    vlan_filtering: bool | None = None
    bridge_priority: int | None = None
    path_cost: int | None = None
    port_priority: int | None = None
    hairpin: bool | None = None
    isolated: bool | None = None
    learning: bool | None = None
    flood: bool | None = None
    multicast_flood: bool | None = None
    bridge_vlans: tuple[BridgeVlanPlan, ...] = ()
    netem: NetemPlan | None = None
    qdisc: QdiscPlan | None = None
    link_id: str | None = None
    parent: str | None = None
    vlan_id: int | None = None
    vrf_table: int | None = None
    bond_mode: str | None = None
    bond_miimon_ms: int | None = None
    bond_primary: str | None = None
    bond_lacp_rate: str | None = None
    bond_xmit_hash_policy: str | None = None
    bond_min_links: int | None = None
    vxlan_vni: int | None = None
    vxlan_link: str | None = None
    vxlan_local: IPAddress | None = None
    vxlan_remote: IPAddress | None = None
    vxlan_dst_port: int | None = None
    vxlan_learning: bool | None = None
    geneve_vni: int | None = None
    geneve_link: str | None = None
    geneve_remote: IPAddress | None = None
    geneve_dst_port: int | None = None
    gre_link: str | None = None
    gre_local: IPAddress | None = None
    gre_remote: IPAddress | None = None
    gre_key: int | None = None
    gre_ttl: int | None = None
    ipip_link: str | None = None
    ipip_local: IPAddress | None = None
    ipip_remote: IPAddress | None = None
    ipip_ttl: int | None = None
    macvlan_mode: str | None = None
    ipvlan_mode: str | None = None

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
    neighbors: tuple[NeighborPlan, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "interfaces",
            MappingProxyType(dict(self.interfaces)),
        )
        object.__setattr__(self, "routes", tuple(self.routes))
        object.__setattr__(self, "sysctls", MappingProxyType(dict(self.sysctls)))
        object.__setattr__(self, "rules", tuple(self.rules))
        object.__setattr__(self, "neighbors", tuple(self.neighbors))


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
    mac: str | None = None
    stp: bool | None = None
    vlan_filtering: bool | None = None
    bridge_priority: int | None = None
    path_cost: int | None = None
    port_priority: int | None = None
    hairpin: bool | None = None
    isolated: bool | None = None
    learning: bool | None = None
    flood: bool | None = None
    multicast_flood: bool | None = None
    bridge_vlans: tuple[BridgeVlanPlan, ...] = ()
    netem: NetemPlan | None = None
    qdisc: QdiscPlan | None = None
    parent: str | None = None
    vlan_id: int | None = None
    vrf_table: int | None = None
    bond_mode: str | None = None
    bond_miimon_ms: int | None = None
    bond_primary: str | None = None
    bond_lacp_rate: str | None = None
    bond_xmit_hash_policy: str | None = None
    bond_min_links: int | None = None
    vxlan_vni: int | None = None
    vxlan_link: str | None = None
    vxlan_local: IPAddress | None = None
    vxlan_remote: IPAddress | None = None
    vxlan_dst_port: int | None = None
    vxlan_learning: bool | None = None
    geneve_vni: int | None = None
    geneve_link: str | None = None
    geneve_remote: IPAddress | None = None
    geneve_dst_port: int | None = None
    gre_link: str | None = None
    gre_local: IPAddress | None = None
    gre_remote: IPAddress | None = None
    gre_key: int | None = None
    gre_ttl: int | None = None
    ipip_link: str | None = None
    ipip_local: IPAddress | None = None
    ipip_remote: IPAddress | None = None
    ipip_ttl: int | None = None
    macvlan_mode: str | None = None
    ipvlan_mode: str | None = None


def expected_bridge_port_vlans(node: NodePlan, interface: str) -> tuple[BridgeVlanPlan, ...]:
    if (
        node.kind != "bridge"
        or not node.vlan_filtering
        or node_interface_master(node, interface) != node.bridge_name
    ):
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
                master=node_interface_master(node, endpoint.interface),
                mtu=link.mtu,
                up=True,
                addresses=node.interfaces.get(endpoint.interface, ()),
                path_cost=None if port is None else port.path_cost,
                port_priority=None if port is None else port.priority,
                hairpin=None if port is None else port.hairpin,
                isolated=None if port is None else port.isolated,
                learning=None if port is None else port.learning,
                flood=None if port is None else port.flood,
                multicast_flood=None if port is None else port.multicast_flood,
                bridge_vlans=expected_bridge_port_vlans(node, endpoint.interface),
                netem=link.netem,
                qdisc=link.qdisc,
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
        elif isinstance(device, VrfDevicePlan):
            expected[device.name] = _ExpectedInterface(
                kind="vrf",
                master=None,
                mtu=None,
                up=True,
                addresses=(),
                vrf_table=device.table,
            )
        elif isinstance(device, BondDevicePlan):
            expected[device.name] = _ExpectedInterface(
                kind="bond",
                master=node_interface_master(node, device.name),
                mtu=bond_device_mtu(node, plan, device),
                up=True,
                addresses=device.addresses,
                bond_mode=device.mode,
                bond_miimon_ms=device.miimon_ms,
                bond_primary=device.primary,
                bond_lacp_rate=device.lacp_rate,
                bond_xmit_hash_policy=device.xmit_hash_policy,
                bond_min_links=device.min_links,
            )
        elif isinstance(device, GreDevicePlan):
            expected[device.name] = _ExpectedInterface(
                kind="gre",
                master=node_interface_master(node, device.name),
                mtu=gre_device_mtu(node, plan, device),
                up=True,
                addresses=device.addresses,
                gre_link=device.link,
                gre_local=device.local,
                gre_remote=device.remote,
                gre_key=device.key,
                gre_ttl=device.ttl,
            )
        elif isinstance(device, IpipDevicePlan):
            expected[device.name] = _ExpectedInterface(
                kind="ipip",
                master=node_interface_master(node, device.name),
                mtu=ipip_device_mtu(node, plan, device),
                up=True,
                addresses=device.addresses,
                ipip_link=device.link,
                ipip_local=device.local,
                ipip_remote=device.remote,
                ipip_ttl=device.ttl,
            )
        else:
            if isinstance(device, DummyDevicePlan):
                expected[device.name] = _ExpectedInterface(
                    kind="dummy",
                    master=node_interface_master(node, device.name),
                    mtu=dummy_device_mtu(node, plan, device),
                    up=True,
                    addresses=device.addresses,
                )
                continue
            if isinstance(device, GeneveDevicePlan):
                port = node.bridge_ports.get(device.name)
                expected[device.name] = _ExpectedInterface(
                    kind="geneve",
                    master=node_interface_master(node, device.name),
                    mtu=geneve_device_mtu(node, plan, device),
                    up=True,
                    addresses=device.addresses,
                    path_cost=None if port is None else port.path_cost,
                    port_priority=None if port is None else port.priority,
                    hairpin=None if port is None else port.hairpin,
                    isolated=None if port is None else port.isolated,
                    learning=None if port is None else port.learning,
                    flood=None if port is None else port.flood,
                    multicast_flood=None if port is None else port.multicast_flood,
                    bridge_vlans=expected_bridge_port_vlans(node, device.name),
                    geneve_vni=device.vni,
                    geneve_link=device.link,
                    geneve_remote=device.remote,
                    geneve_dst_port=device.dst_port,
                )
                continue
            if isinstance(device, MacvlanDevicePlan):
                expected[device.name] = _ExpectedInterface(
                    kind="macvlan",
                    master=node_interface_master(node, device.name),
                    mtu=macvlan_device_mtu(node, plan, device),
                    up=True,
                    addresses=device.addresses,
                    parent=device.link,
                    macvlan_mode=device.mode,
                )
                continue
            if isinstance(device, IpvlanDevicePlan):
                expected[device.name] = _ExpectedInterface(
                    kind="ipvlan",
                    master=node_interface_master(node, device.name),
                    mtu=ipvlan_device_mtu(node, plan, device),
                    up=True,
                    addresses=device.addresses,
                    parent=device.link,
                    ipvlan_mode=device.mode,
                )
                continue
            assert isinstance(device, VxlanDevicePlan)
            port = node.bridge_ports.get(device.name)
            expected[device.name] = _ExpectedInterface(
                kind="vxlan",
                master=node_interface_master(node, device.name),
                mtu=vxlan_device_mtu(node, plan, device),
                up=True,
                addresses=device.addresses,
                path_cost=None if port is None else port.path_cost,
                port_priority=None if port is None else port.priority,
                hairpin=None if port is None else port.hairpin,
                isolated=None if port is None else port.isolated,
                learning=None if port is None else port.learning,
                flood=None if port is None else port.flood,
                multicast_flood=None if port is None else port.multicast_flood,
                bridge_vlans=expected_bridge_port_vlans(node, device.name),
                vxlan_vni=device.vni,
                vxlan_link=device.link,
                vxlan_local=device.local,
                vxlan_remote=device.remote,
                vxlan_dst_port=device.dst_port,
                vxlan_learning=device.learning,
            )

    return {
        name: replace(interface, mac=node.mac_addresses.get(name))
        for name, interface in expected.items()
    }


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
        if desired.mac is not None and observed.mac != desired.mac:
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
        if desired.hairpin is not None and observed.hairpin is not desired.hairpin:
            return False
        if desired.isolated is not None and observed.isolated is not desired.isolated:
            return False
        if desired.learning is not None and observed.learning is not desired.learning:
            return False
        if desired.flood is not None and observed.flood is not desired.flood:
            return False
        if (
            desired.multicast_flood is not None
            and observed.multicast_flood is not desired.multicast_flood
        ):
            return False
        if observed.bridge_vlans != desired.bridge_vlans:
            return False
        if observed.netem != desired.netem:
            return False
        if not _qdisc_matches(desired.qdisc, observed.qdisc):
            return False
        if observed.parent != desired.parent:
            return False
        if observed.vlan_id != desired.vlan_id:
            return False
        if observed.vrf_table != desired.vrf_table:
            return False
        if observed.bond_mode != desired.bond_mode:
            return False
        if observed.bond_miimon_ms != desired.bond_miimon_ms:
            return False
        if observed.bond_primary != desired.bond_primary:
            return False
        if observed.bond_lacp_rate != desired.bond_lacp_rate:
            return False
        if observed.bond_xmit_hash_policy != desired.bond_xmit_hash_policy:
            return False
        if observed.bond_min_links != desired.bond_min_links:
            return False
        if observed.vxlan_vni != desired.vxlan_vni:
            return False
        if observed.vxlan_link != desired.vxlan_link:
            return False
        if observed.vxlan_local != desired.vxlan_local:
            return False
        if observed.vxlan_remote != desired.vxlan_remote:
            return False
        if observed.vxlan_dst_port != desired.vxlan_dst_port:
            return False
        if observed.vxlan_learning is not desired.vxlan_learning:
            return False
        if observed.geneve_vni != desired.geneve_vni:
            return False
        # Linux Geneve has no netlink attribute for a fixed underlay device;
        # the route to the remote endpoint determines the egress interface.
        # Compare a link only when a backend can actually report one.
        if observed.geneve_link is not None and observed.geneve_link != desired.geneve_link:
            return False
        if observed.geneve_remote != desired.geneve_remote:
            return False
        if observed.geneve_dst_port != desired.geneve_dst_port:
            return False
        if observed.gre_link != desired.gre_link:
            return False
        if observed.gre_local != desired.gre_local:
            return False
        if observed.gre_remote != desired.gre_remote:
            return False
        if observed.gre_key != desired.gre_key:
            return False
        if observed.gre_ttl != desired.gre_ttl:
            return False
        if observed.ipip_link != desired.ipip_link:
            return False
        if observed.ipip_local != desired.ipip_local:
            return False
        if observed.ipip_remote != desired.ipip_remote:
            return False
        if observed.ipip_ttl != desired.ipip_ttl:
            return False
        if observed.macvlan_mode != desired.macvlan_mode:
            return False
        if observed.ipvlan_mode != desired.ipvlan_mode:
            return False

    return True


def runtime_managed_interface_names(node: NodePlan) -> frozenset[str]:
    """Return kernel interfaces created as a side effect of managed runtimes."""

    if node.routing is not None and node.routing.pim is not None:
        return frozenset({PIM_REGISTER_INTERFACE_NAME})
    return frozenset()


def _qdisc_matches(desired: QdiscPlan | None, observed: QdiscPlan | None) -> bool:
    """Compare qdisc state while allowing TBF's kernel tick quantization."""

    if desired is None or observed is None:
        return desired is observed
    if type(desired) is not type(observed):
        return False
    if isinstance(desired, TbfPlan):
        assert isinstance(observed, TbfPlan)
        burst_tolerance = max(2, desired.burst_bytes // 1000)
        return (
            desired.rate == observed.rate
            and abs(desired.burst_bytes - observed.burst_bytes) <= burst_tolerance
            and abs(desired.latency_ms - observed.latency_ms) <= 1
        )
    return desired == observed


def _routes_match(
    node: NodePlan,
    desired: Sequence[RoutePlan],
    actual: Sequence[RoutePlan],
) -> bool:
    actual_routes = frozenset(actual)
    desired_routes = frozenset(desired)
    if node.routing is not None:
        # Routing daemons legitimately add and withdraw routes asynchronously. Static
        # routes remain managed by nslab; learned routes are intentionally extra.
        return desired_routes <= actual_routes
    return actual_routes == desired_routes


def _declared_sysctls_match(desired: Mapping[str, int], actual: Mapping[str, int]) -> bool:
    return all(actual.get(key) == value for key, value in desired.items())


_HEALTHY_DYNAMIC_NEIGHBOR_STATES = frozenset({"reachable", "stale", "delay", "probe"})


def neighbors_match(
    desired: Sequence[NeighborPlan],
    actual: Sequence[NeighborPlan],
) -> bool:
    """Compare declared neighbors while allowing normal healthy NUD transitions."""

    if len(desired) != len(actual):
        return False
    actual_by_identity = {(neighbor.dst, neighbor.dev): neighbor for neighbor in actual}
    if len(actual_by_identity) != len(actual):
        return False
    for expected in desired:
        observed = actual_by_identity.get((expected.dst, expected.dev))
        if observed is None:
            return False
        if (
            observed.lladdr != expected.lladdr
            or observed.proxy is not expected.proxy
            or (
                expected.state in {"reachable", "stale"}
                and observed.state not in _HEALTHY_DYNAMIC_NEIGHBOR_STATES
            )
            or (expected.state not in {"reachable", "stale"} and observed.state != expected.state)
        ):
            return False
    return True


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
        ignored_interfaces = runtime_managed_interface_names(node)
        observed_interfaces = {
            name: interface
            for name, interface in observed.interfaces.items()
            if name not in ignored_interfaces
        }
        if not _interfaces_match(_expected_interfaces(node, plan), observed_interfaces):
            return False
        if not _routes_match(
            node,
            expected_routes(node),
            observed.routes,
        ):
            return False
        if frozenset(observed.rules) != frozenset(node.rules):
            return False
        if not neighbors_match(node.neighbors, observed.neighbors):
            return False
        if not _declared_sysctls_match(node.sysctls, observed.sysctls):
            return False

    return _links_match(plan, inventory)
