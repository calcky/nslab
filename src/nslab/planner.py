from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv4Network,
    IPv6Address,
    IPv6Interface,
    IPv6Network,
    ip_address,
    ip_interface,
    ip_network,
)
from types import MappingProxyType
from typing import Literal

from nslab.errors import NslabError
from nslab.manifest import (
    MAIN_ROUTE_TABLE,
    NAME_PATTERN,
    BgpConfig,
    BondDeviceConfig,
    BridgeNode,
    CakeConfig,
    DummyDeviceConfig,
    FqCodelConfig,
    GeneveDeviceConfig,
    GreDeviceConfig,
    HtbConfig,
    InterfaceConfig,
    IpipDeviceConfig,
    IpvlanDeviceConfig,
    LinuxNode,
    MacvlanDeviceConfig,
    Manifest,
    NodeConfig,
    OspfConfig,
    PimConfig,
    QdiscConfig,
    RoutingConfig,
    TbfConfig,
    VlanDeviceConfig,
    VrfDeviceConfig,
    VxlanDeviceConfig,
    manifest_fingerprint,
)
from nslab.naming import namespace_name, temporary_veth_names

type NodeKind = Literal["linux", "bridge"]
type LinkKind = Literal["veth"]
type IPAddress = IPv4Address | IPv6Address
type IPInterface = IPv4Interface | IPv6Interface
type IPNetwork = IPv4Network | IPv6Network


@dataclass(frozen=True, slots=True)
class RouteNextHopPlan:
    via: IPAddress | None
    dev: str
    weight: int = 1


@dataclass(frozen=True, slots=True)
class RoutePlan:
    dst: IPNetwork
    via: IPAddress | None
    dev: str | None
    table: int = MAIN_ROUTE_TABLE
    nexthops: tuple[RouteNextHopPlan, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "nexthops", tuple(self.nexthops))


def route_interfaces(route: RoutePlan) -> tuple[str, ...]:
    if route.nexthops:
        return tuple(nexthop.dev for nexthop in route.nexthops)
    assert route.dev is not None
    return (route.dev,)


type NeighborState = Literal[
    "incomplete",
    "reachable",
    "stale",
    "delay",
    "probe",
    "failed",
    "noarp",
    "permanent",
]


@dataclass(frozen=True, slots=True)
class NeighborPlan:
    dst: IPAddress
    dev: str
    lladdr: str | None
    state: NeighborState | None
    proxy: bool = False


@dataclass(frozen=True, slots=True)
class PolicyRulePlan:
    priority: int
    family: Literal[4, 6]
    action: str = "lookup"
    table: int | None = None
    goto: int | None = None
    source: IPNetwork | None = None
    destination: IPNetwork | None = None
    invert: bool = False
    tos: int | None = None
    fwmark: int | None = None
    fwmask: int | None = None
    iif: str | None = None
    oif: str | None = None
    l3mdev: bool = False
    uid_range: tuple[int, int] | None = None
    protocol: int = 0
    ip_protocol: int | None = None
    source_port: tuple[int, int] | None = None
    destination_port: tuple[int, int] | None = None
    tunnel_id: int | None = None
    suppress_prefix_length: int | None = None
    suppress_interface_group: int | None = None
    realms: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class OspfPlan:
    router_id: IPv4Address
    area: IPv4Address
    networks: tuple[IPv4Network, ...]
    passive_interfaces: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BgpNeighborPlan:
    address: IPv4Address
    remote_as: int


@dataclass(frozen=True, slots=True)
class BgpPlan:
    local_as: int
    router_id: IPv4Address
    neighbors: tuple[BgpNeighborPlan, ...]
    networks: tuple[IPv4Network, ...]


@dataclass(frozen=True, slots=True)
class PimPlan:
    rp_address: IPv4Address
    interfaces: tuple[str, ...]
    igmp_interfaces: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoutingPlan:
    ospf: OspfPlan | None = None
    bgp: BgpPlan | None = None
    pim: PimPlan | None = None


@dataclass(frozen=True, slots=True)
class BridgeVlanPlan:
    vid: int
    pvid: bool
    untagged: bool


@dataclass(frozen=True, slots=True)
class BridgePortPlan:
    path_cost: int | None
    priority: int | None
    vlans: tuple[BridgeVlanPlan, ...] = ()
    hairpin: bool | None = None
    isolated: bool | None = None
    learning: bool | None = None
    flood: bool | None = None
    multicast_flood: bool | None = None


@dataclass(frozen=True, slots=True)
class VlanDevicePlan:
    name: str
    link: str
    vlan_id: int
    addresses: tuple[IPInterface, ...] = ()


@dataclass(frozen=True, slots=True)
class VrfDevicePlan:
    name: str
    table: int
    interfaces: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BondDevicePlan:
    name: str
    mode: Literal["active-backup", "802.3ad"]
    interfaces: tuple[str, ...]
    addresses: tuple[IPInterface, ...] = ()
    miimon_ms: int = 100
    primary: str | None = None
    lacp_rate: Literal["slow", "fast"] | None = None
    xmit_hash_policy: Literal["layer2", "layer2+3", "layer3+4"] | None = None
    min_links: int | None = None


@dataclass(frozen=True, slots=True)
class GreDevicePlan:
    name: str
    link: str
    local: IPv4Address
    remote: IPv4Address
    key: int | None = None
    ttl: int = 64
    mtu: int | None = None
    addresses: tuple[IPInterface, ...] = ()


@dataclass(frozen=True, slots=True)
class IpipDevicePlan:
    name: str
    link: str
    local: IPv4Address
    remote: IPv4Address
    ttl: int = 64
    mtu: int | None = None
    addresses: tuple[IPInterface, ...] = ()


IPIPDevicePlan = IpipDevicePlan


@dataclass(frozen=True, slots=True)
class VxlanDevicePlan:
    name: str
    vni: int
    link: str
    local: IPAddress
    remote: IPAddress
    dst_port: int = 4789
    learning: bool = True
    mtu: int | None = None
    addresses: tuple[IPInterface, ...] = ()


@dataclass(frozen=True, slots=True)
class DummyDevicePlan:
    name: str
    mtu: int | None = None
    addresses: tuple[IPInterface, ...] = ()


@dataclass(frozen=True, slots=True)
class GeneveDevicePlan:
    name: str
    vni: int
    link: str
    remote: IPAddress
    dst_port: int = 6081
    mtu: int | None = None
    addresses: tuple[IPInterface, ...] = ()


@dataclass(frozen=True, slots=True)
class MacvlanDevicePlan:
    name: str
    link: str
    mode: Literal["private", "vepa", "bridge", "passthru", "source"] = "bridge"
    mtu: int | None = None
    addresses: tuple[IPInterface, ...] = ()


@dataclass(frozen=True, slots=True)
class IpvlanDevicePlan:
    name: str
    link: str
    mode: Literal["l2", "l3", "l3s"] = "l2"
    mtu: int | None = None
    addresses: tuple[IPInterface, ...] = ()


type DevicePlan = (
    VlanDevicePlan
    | VrfDevicePlan
    | BondDevicePlan
    | GreDevicePlan
    | IpipDevicePlan
    | VxlanDevicePlan
    | DummyDevicePlan
    | GeneveDevicePlan
    | MacvlanDevicePlan
    | IpvlanDevicePlan
)


def _empty_bridge_ports() -> Mapping[str, BridgePortPlan]:
    return MappingProxyType({})


def _empty_devices() -> Mapping[str, DevicePlan]:
    return MappingProxyType({})


def _empty_mac_addresses() -> Mapping[str, str]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class NodePlan:
    name: str
    kind: NodeKind
    namespace: str
    interfaces: Mapping[str, tuple[IPInterface, ...]]
    routes: tuple[RoutePlan, ...]
    sysctls: Mapping[str, int]
    mac_addresses: Mapping[str, str] = field(default_factory=_empty_mac_addresses)
    neighbors: tuple[NeighborPlan, ...] = ()
    rules: tuple[PolicyRulePlan, ...] = ()
    devices: Mapping[str, DevicePlan] = field(default_factory=_empty_devices)
    bridge_name: str | None = None
    stp: bool | None = None
    vlan_filtering: bool | None = None
    bridge_priority: int | None = None
    bridge_ports: Mapping[str, BridgePortPlan] = field(default_factory=_empty_bridge_ports)
    routing: RoutingPlan | None = None


@dataclass(frozen=True, slots=True)
class EndpointPlan:
    node: str
    interface: str
    namespace: str
    temporary_name: str


@dataclass(frozen=True, slots=True)
class NetemPlan:
    delay_ms: int
    jitter_ms: int
    loss_percent: int
    rate: str | None = None


@dataclass(frozen=True, slots=True)
class TbfPlan:
    rate: str
    burst_bytes: int
    latency_ms: int


@dataclass(frozen=True, slots=True)
class FqCodelPlan:
    target_ms: int
    interval_ms: int
    limit: int
    ecn: bool


@dataclass(frozen=True, slots=True)
class HtbPlan:
    rate: str
    leaf: FqCodelPlan


@dataclass(frozen=True, slots=True)
class CakePlan:
    bandwidth: str
    flow_mode: str
    diffserv_mode: str
    rtt_ms: int
    nat: bool


type QdiscPlan = TbfPlan | FqCodelPlan | HtbPlan | CakePlan


@dataclass(frozen=True, slots=True)
class LinkPlan:
    index: int
    kind: LinkKind
    left: EndpointPlan
    right: EndpointPlan
    mtu: int
    netem: NetemPlan | None = None
    qdisc: QdiscPlan | None = None


@dataclass(frozen=True, slots=True)
class TopologyPlan:
    name: str
    fingerprint: str
    nodes: Mapping[str, NodePlan]
    links: tuple[LinkPlan, ...]


def bond_device_mtu(node: NodePlan, plan: TopologyPlan, device: BondDevicePlan) -> int:
    """Return the common MTU declared by a bond's member links."""

    mtus = {
        link.mtu
        for link in plan.links
        for endpoint in (link.left, link.right)
        if endpoint.node == node.name and endpoint.interface in device.interfaces
    }
    if len(mtus) != 1:
        raise ValueError(f"bond members do not have one planned MTU: {node.name}:{device.name}")
    return next(iter(mtus))


def vxlan_device_mtu(node: NodePlan, plan: TopologyPlan, device: VxlanDevicePlan) -> int:
    """Return an explicit VXLAN MTU or derive one from its underlay link."""

    if device.mtu is not None:
        return device.mtu
    underlay_mtu = next(
        (
            link.mtu
            for link in plan.links
            for endpoint in (link.left, link.right)
            if endpoint.node == node.name and endpoint.interface == device.link
        ),
        None,
    )
    if underlay_mtu is None:
        raise ValueError(f"VXLAN underlay is not planned: {node.name}:{device.link}")
    return underlay_mtu - (50 if device.local.version == 4 else 70)


def parent_device_mtu(node: NodePlan, plan: TopologyPlan, interface: str) -> int:
    """Return the MTU planned for a linked parent interface."""

    mtu = next(
        (
            link.mtu
            for link in plan.links
            for endpoint in (link.left, link.right)
            if endpoint.node == node.name and endpoint.interface == interface
        ),
        None,
    )
    if mtu is None:
        raise ValueError(f"device parent is not planned: {node.name}:{interface}")
    return mtu


def dummy_device_mtu(node: NodePlan, plan: TopologyPlan, device: DummyDevicePlan) -> int:
    del node, plan
    return 1500 if device.mtu is None else device.mtu


def geneve_device_mtu(node: NodePlan, plan: TopologyPlan, device: GeneveDevicePlan) -> int:
    """Return an explicit Geneve MTU or derive one from its underlay link."""

    if device.mtu is not None:
        return device.mtu
    underlay_mtu = parent_device_mtu(node, plan, device.link)
    return underlay_mtu - (50 if device.remote.version == 4 else 70)


def gre_device_mtu(node: NodePlan, plan: TopologyPlan, device: GreDevicePlan) -> int:
    """Return an explicit GRE MTU or derive one from its IPv4 underlay."""

    if device.mtu is not None:
        return device.mtu
    overhead = 24 + (4 if device.key is not None else 0)
    return parent_device_mtu(node, plan, device.link) - overhead


def ipip_device_mtu(node: NodePlan, plan: TopologyPlan, device: IpipDevicePlan) -> int:
    """Return an explicit IPIP MTU or derive one from its IPv4 underlay."""

    if device.mtu is not None:
        return device.mtu
    return parent_device_mtu(node, plan, device.link) - 20


def macvlan_device_mtu(node: NodePlan, plan: TopologyPlan, device: MacvlanDevicePlan) -> int:
    return parent_device_mtu(node, plan, device.link) if device.mtu is None else device.mtu


def ipvlan_device_mtu(node: NodePlan, plan: TopologyPlan, device: IpvlanDevicePlan) -> int:
    return parent_device_mtu(node, plan, device.link) if device.mtu is None else device.mtu


def node_interface_addresses(node: NodePlan) -> Mapping[str, tuple[IPInterface, ...]]:
    """Return address declarations for linked and namespace-local devices."""

    return MappingProxyType(
        {
            **node.interfaces,
            **{
                name: device.addresses
                for name, device in node.devices.items()
                if isinstance(
                    device,
                    (
                        VlanDevicePlan,
                        BondDevicePlan,
                        VxlanDevicePlan,
                        DummyDevicePlan,
                        GeneveDevicePlan,
                        GreDevicePlan,
                        IpipDevicePlan,
                        MacvlanDevicePlan,
                        IpvlanDevicePlan,
                    ),
                )
            },
        }
    )


def node_interface_vrf_master(node: NodePlan, interface: str) -> str | None:
    """Return the VRF device that owns an interface, if any."""

    return next(
        (
            device.name
            for device in node.devices.values()
            if isinstance(device, VrfDevicePlan) and interface in device.interfaces
        ),
        None,
    )


def node_interface_master(node: NodePlan, interface: str) -> str | None:
    """Return the L2 or L3 master that owns an interface, if any."""

    bond = next(
        (
            device.name
            for device in node.devices.values()
            if isinstance(device, BondDevicePlan) and interface in device.interfaces
        ),
        None,
    )
    if bond is not None:
        return bond
    vrf = node_interface_vrf_master(node, interface)
    if vrf is not None:
        return vrf
    if node.kind != "bridge":
        return None
    if interface == node.bridge_name:
        return None
    if any(
        isinstance(device, (VxlanDevicePlan, GeneveDevicePlan)) and device.link == interface
        for device in node.devices.values()
    ):
        return None
    return node.bridge_name


def node_interface_route_table(node: NodePlan, interface: str) -> int:
    """Return the routing table selected by an interface's VRF membership."""

    master = node_interface_vrf_master(node, interface)
    if master is None:
        return MAIN_ROUTE_TABLE
    device = node.devices[master]
    assert isinstance(device, VrfDevicePlan)
    return device.table


def node_route_tables(node: NodePlan) -> tuple[int, ...]:
    """Return the managed route tables for a node in deterministic order."""

    return tuple(
        dict.fromkeys(
            (
                MAIN_ROUTE_TABLE,
                *(
                    device.table
                    for device in node.devices.values()
                    if isinstance(device, VrfDevicePlan)
                ),
                *(route.table for route in node.routes),
            )
        )
    )


def _policy_rule_fwmask(fwmark: int | None, fwmask: int | None) -> int | None:
    if fwmark is None:
        return None
    return 4_294_967_295 if fwmask is None else fwmask


def _effective_deployment_name(manifest: Manifest, name_override: str | None) -> str:
    if name_override is None:
        return manifest.name
    if NAME_PATTERN.fullmatch(name_override) is None:
        raise NslabError(
            code="DEPLOYMENT_NAME_INVALID",
            message=f"invalid deployment name: {name_override!r}",
            details={"name": name_override},
        )
    return name_override


def _compile_node(deployment: str, name: str, manifest_node: NodeConfig) -> NodePlan:
    interfaces = MappingProxyType(
        {
            interface_name: tuple(ip_interface(str(address)) for address in config.addresses)
            for interface_name, config in manifest_node.interfaces.items()
        }
    )
    compiled_sysctls = dict(manifest_node.sysctls)
    for neighbor in manifest_node.neighbors:
        if not neighbor.proxy:
            continue
        key = (
            f"net.ipv4.conf.{neighbor.dev}.proxy_arp"
            if neighbor.dst.version == 4
            else f"net.ipv6.conf.{neighbor.dev}.proxy_ndp"
        )
        compiled_sysctls[key] = 1
    sysctls = MappingProxyType(compiled_sysctls)
    compiled_devices: dict[str, DevicePlan] = {}
    if isinstance(manifest_node, LinuxNode):
        for device_name, config in manifest_node.devices.items():
            if isinstance(config, VlanDeviceConfig):
                compiled_devices[device_name] = VlanDevicePlan(
                    name=device_name,
                    link=config.link,
                    vlan_id=config.id,
                    addresses=tuple(ip_interface(str(address)) for address in config.addresses),
                )
            elif isinstance(config, VrfDeviceConfig):
                compiled_devices[device_name] = VrfDevicePlan(
                    name=device_name,
                    table=config.table,
                    interfaces=tuple(config.interfaces),
                )
            elif isinstance(config, VxlanDeviceConfig):
                compiled_devices[device_name] = VxlanDevicePlan(
                    name=device_name,
                    vni=config.vni,
                    link=config.link,
                    local=ip_address(str(config.local)),
                    remote=ip_address(str(config.remote)),
                    addresses=tuple(ip_interface(str(address)) for address in config.addresses),
                    dst_port=config.dst_port,
                    learning=config.learning,
                    mtu=config.mtu,
                )
            elif isinstance(config, DummyDeviceConfig):
                compiled_devices[device_name] = DummyDevicePlan(
                    name=device_name,
                    mtu=config.mtu,
                    addresses=tuple(ip_interface(str(address)) for address in config.addresses),
                )
            elif isinstance(config, GreDeviceConfig):
                compiled_devices[device_name] = GreDevicePlan(
                    name=device_name,
                    link=config.link,
                    local=config.local,
                    remote=config.remote,
                    key=config.key,
                    ttl=config.ttl,
                    mtu=config.mtu,
                    addresses=tuple(ip_interface(str(address)) for address in config.addresses),
                )
            elif isinstance(config, IpipDeviceConfig):
                compiled_devices[device_name] = IpipDevicePlan(
                    name=device_name,
                    link=config.link,
                    local=config.local,
                    remote=config.remote,
                    ttl=config.ttl,
                    mtu=config.mtu,
                    addresses=tuple(ip_interface(str(address)) for address in config.addresses),
                )
            elif isinstance(config, GeneveDeviceConfig):
                compiled_devices[device_name] = GeneveDevicePlan(
                    name=device_name,
                    vni=config.vni,
                    link=config.link,
                    remote=ip_address(str(config.remote)),
                    addresses=tuple(ip_interface(str(address)) for address in config.addresses),
                    dst_port=config.dst_port,
                    mtu=config.mtu,
                )
            elif isinstance(config, MacvlanDeviceConfig):
                compiled_devices[device_name] = MacvlanDevicePlan(
                    name=device_name,
                    link=config.link,
                    mode=config.mode,
                    mtu=config.mtu,
                    addresses=tuple(ip_interface(str(address)) for address in config.addresses),
                )
            elif isinstance(config, IpvlanDeviceConfig):
                compiled_devices[device_name] = IpvlanDevicePlan(
                    name=device_name,
                    link=config.link,
                    mode=config.mode,
                    mtu=config.mtu,
                    addresses=tuple(ip_interface(str(address)) for address in config.addresses),
                )
            else:
                assert isinstance(config, BondDeviceConfig)
                is_lacp = config.mode == "802.3ad"
                compiled_devices[device_name] = BondDevicePlan(
                    name=device_name,
                    mode=config.mode,
                    interfaces=tuple(config.interfaces),
                    addresses=tuple(ip_interface(str(address)) for address in config.addresses),
                    miimon_ms=config.miimon_ms,
                    primary=config.primary,
                    lacp_rate=(config.lacp_rate or "slow") if is_lacp else None,
                    xmit_hash_policy=(config.xmit_hash_policy or "layer2" if is_lacp else None),
                    min_links=(0 if config.min_links is None else config.min_links)
                    if is_lacp
                    else None,
                )
    elif isinstance(manifest_node, BridgeNode):
        for device_name, config in manifest_node.devices.items():
            if isinstance(config, VxlanDeviceConfig):
                compiled_devices[device_name] = VxlanDevicePlan(
                    name=device_name,
                    vni=config.vni,
                    link=config.link,
                    local=ip_address(str(config.local)),
                    remote=ip_address(str(config.remote)),
                    addresses=tuple(ip_interface(str(address)) for address in config.addresses),
                    dst_port=config.dst_port,
                    learning=config.learning,
                    mtu=config.mtu,
                )
            else:
                assert isinstance(config, GeneveDeviceConfig)
                compiled_devices[device_name] = GeneveDevicePlan(
                    name=device_name,
                    vni=config.vni,
                    link=config.link,
                    remote=ip_address(str(config.remote)),
                    addresses=tuple(ip_interface(str(address)) for address in config.addresses),
                    dst_port=config.dst_port,
                    mtu=config.mtu,
                )
    devices: Mapping[str, DevicePlan] = MappingProxyType(compiled_devices)
    mac_addresses = MappingProxyType(
        {
            **{
                interface_name: config.mac
                for interface_name, config in manifest_node.interfaces.items()
                if config.mac is not None
            },
            **{
                device_name: config.mac
                for device_name, config in manifest_node.devices.items()
                if isinstance(config, InterfaceConfig) and config.mac is not None
            },
        }
    )
    tables_by_interface = {
        interface: device.table
        for device in devices.values()
        if isinstance(device, VrfDevicePlan)
        for interface in device.interfaces
    }
    compiled_routes: list[RoutePlan] = []
    for route in manifest_node.routes:
        nexthops = tuple(
            RouteNextHopPlan(
                via=ip_address(str(nexthop.via)) if nexthop.via is not None else None,
                dev=nexthop.dev,
                weight=nexthop.weight,
            )
            for nexthop in route.nexthops
        )
        if nexthops:
            route_devices = tuple(nexthop.dev for nexthop in route.nexthops)
        else:
            assert route.dev is not None
            route_devices = (route.dev,)
        inferred_tables = {
            tables_by_interface.get(interface, MAIN_ROUTE_TABLE) for interface in route_devices
        }
        assert len(inferred_tables) == 1
        compiled_routes.append(
            RoutePlan(
                dst=ip_network(str(route.dst)),
                via=ip_address(str(route.via)) if route.via is not None else None,
                dev=route.dev,
                table=route.table if route.table is not None else inferred_tables.pop(),
                nexthops=nexthops,
            )
        )
    routes = tuple(compiled_routes)
    neighbors = tuple(
        NeighborPlan(
            dst=ip_address(str(neighbor.dst)),
            dev=neighbor.dev,
            lladdr=neighbor.lladdr,
            state=None if neighbor.proxy else (neighbor.state or "permanent"),
            proxy=neighbor.proxy,
        )
        for neighbor in manifest_node.neighbors
    )
    rules = (
        tuple(
            PolicyRulePlan(
                priority=rule.priority,
                family=rule.ip_version,
                action=rule.action,
                table=rule.table,
                goto=rule.goto,
                source=(
                    None
                    if rule.source is None or rule.source.prefixlen == 0
                    else ip_network(str(rule.source))
                ),
                destination=(
                    None
                    if rule.destination is None or rule.destination.prefixlen == 0
                    else ip_network(str(rule.destination))
                ),
                invert=rule.invert,
                tos=None if rule.tos in {None, 0} else rule.tos,
                fwmark=rule.fwmark,
                fwmask=_policy_rule_fwmask(rule.fwmark, rule.fwmask),
                iif=rule.iif,
                oif=rule.oif,
                l3mdev=rule.l3mdev,
                uid_range=(
                    None if rule.uid_range is None else (rule.uid_range.start, rule.uid_range.end)
                ),
                protocol=rule.protocol,
                ip_protocol=None if rule.ip_protocol in {None, 0} else rule.ip_protocol,
                source_port=(
                    None
                    if rule.source_port is None
                    else (rule.source_port.start, rule.source_port.end)
                ),
                destination_port=(
                    None
                    if rule.destination_port is None
                    else (rule.destination_port.start, rule.destination_port.end)
                ),
                tunnel_id=None if rule.tunnel_id in {None, 0} else rule.tunnel_id,
                suppress_prefix_length=rule.suppress_prefix_length,
                suppress_interface_group=rule.suppress_interface_group,
                realms=(
                    None if rule.realms is None else (rule.realms.source, rule.realms.destination)
                ),
            )
            for rule in manifest_node.rules
        )
        if isinstance(manifest_node, LinuxNode)
        else ()
    )

    routing = _compile_routing(manifest_node.routing)

    if isinstance(manifest_node, BridgeNode):
        bridge_name = manifest_node.bridge.name
        stp = manifest_node.bridge.stp
        vlan_filtering = manifest_node.bridge.vlan_filtering
        bridge_priority = manifest_node.bridge.priority
        bridge_ports = MappingProxyType(
            {
                interface: BridgePortPlan(
                    path_cost=config.path_cost,
                    priority=config.priority,
                    vlans=tuple(
                        BridgeVlanPlan(
                            vid=vlan.vid,
                            pvid=vlan.pvid,
                            untagged=vlan.untagged,
                        )
                        for vlan in config.vlans
                    ),
                    hairpin=config.hairpin,
                    isolated=config.isolated,
                    learning=config.learning,
                    flood=config.flood,
                    multicast_flood=config.multicast_flood,
                )
                for interface, config in manifest_node.bridge.ports.items()
            }
        )
    else:
        bridge_name = None
        stp = None
        vlan_filtering = None
        bridge_priority = None
        bridge_ports = MappingProxyType({})

    return NodePlan(
        name=name,
        kind=manifest_node.kind,
        namespace=namespace_name(deployment, name),
        interfaces=interfaces,
        routes=routes,
        sysctls=sysctls,
        mac_addresses=mac_addresses,
        neighbors=neighbors,
        rules=rules,
        devices=devices,
        routing=routing,
        bridge_name=bridge_name,
        stp=stp,
        vlan_filtering=vlan_filtering,
        bridge_priority=bridge_priority,
        bridge_ports=bridge_ports,
    )


def _compile_ospf(config: OspfConfig) -> OspfPlan:
    return OspfPlan(
        router_id=config.router_id,
        area=config.area,
        networks=tuple(config.networks),
        passive_interfaces=tuple(config.passive_interfaces),
    )


def _compile_bgp(config: BgpConfig) -> BgpPlan:
    return BgpPlan(
        local_as=config.local_as,
        router_id=config.router_id,
        neighbors=tuple(
            BgpNeighborPlan(address=neighbor.address, remote_as=neighbor.remote_as)
            for neighbor in config.neighbors
        ),
        networks=tuple(config.networks),
    )


def _compile_pim(config: PimConfig) -> PimPlan:
    return PimPlan(
        rp_address=config.rp_address,
        interfaces=tuple(config.interfaces),
        igmp_interfaces=tuple(config.igmp_interfaces),
    )


def _compile_routing(config: RoutingConfig | None) -> RoutingPlan | None:
    if config is None:
        return None
    return RoutingPlan(
        ospf=None if config.ospf is None else _compile_ospf(config.ospf),
        bgp=None if config.bgp is None else _compile_bgp(config.bgp),
        pim=None if config.pim is None else _compile_pim(config.pim),
    )


def _compile_endpoint(
    endpoint: str, temporary_name: str, nodes: Mapping[str, NodePlan]
) -> EndpointPlan:
    node, separator, interface = endpoint.partition(":")
    if not separator:
        raise ValueError(f"invalid planned endpoint: {endpoint!r}")
    return EndpointPlan(
        node=node,
        interface=interface,
        namespace=nodes[node].namespace,
        temporary_name=temporary_name,
    )


def _compile_qdisc(config: QdiscConfig) -> QdiscPlan:
    if isinstance(config, TbfConfig):
        return TbfPlan(
            rate=config.rate,
            burst_bytes=config.burst,
            latency_ms=config.latency_ms,
        )
    if isinstance(config, FqCodelConfig):
        return _compile_fq_codel(config)
    if isinstance(config, HtbConfig):
        return HtbPlan(rate=config.rate, leaf=_compile_fq_codel(config.leaf))
    assert isinstance(config, CakeConfig)
    return CakePlan(
        bandwidth=config.bandwidth,
        flow_mode=config.flow_mode,
        diffserv_mode=config.diffserv_mode,
        rtt_ms=config.rtt_ms,
        nat=config.nat,
    )


def _compile_fq_codel(config: FqCodelConfig) -> FqCodelPlan:
    return FqCodelPlan(
        target_ms=config.target_ms,
        interval_ms=config.interval_ms,
        limit=config.limit,
        ecn=config.ecn,
    )


def compile_plan(manifest: Manifest, name_override: str | None = None) -> TopologyPlan:
    deployment = _effective_deployment_name(manifest, name_override)
    mutable_nodes = {
        name: _compile_node(deployment, name, node)
        for name, node in manifest.topology.nodes.items()
    }

    mutable_links: list[LinkPlan] = []
    for index, link in enumerate(manifest.topology.links):
        temporary_left, temporary_right = temporary_veth_names(deployment, index)
        left_endpoint, right_endpoint = link.endpoints
        mutable_links.append(
            LinkPlan(
                index=index,
                kind=link.kind,
                left=_compile_endpoint(left_endpoint, temporary_left, mutable_nodes),
                right=_compile_endpoint(right_endpoint, temporary_right, mutable_nodes),
                mtu=link.mtu,
                netem=(
                    None
                    if link.netem is None
                    else NetemPlan(
                        delay_ms=link.netem.delay_ms,
                        jitter_ms=link.netem.jitter_ms,
                        loss_percent=link.netem.loss_percent,
                        rate=link.netem.rate,
                    )
                ),
                qdisc=None if link.qdisc is None else _compile_qdisc(link.qdisc),
            )
        )

    return TopologyPlan(
        name=deployment,
        fingerprint=manifest_fingerprint(manifest),
        nodes=MappingProxyType(dict(mutable_nodes)),
        links=tuple(mutable_links),
    )
