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
    BridgeNode,
    LinuxNode,
    Manifest,
    NodeConfig,
    OspfConfig,
    RoutingConfig,
    VlanDeviceConfig,
    VrfDeviceConfig,
    manifest_fingerprint,
)
from nslab.naming import namespace_name, temporary_veth_names

type NodeKind = Literal["linux", "bridge"]
type LinkKind = Literal["veth"]
type IPAddress = IPv4Address | IPv6Address
type IPInterface = IPv4Interface | IPv6Interface
type IPNetwork = IPv4Network | IPv6Network


@dataclass(frozen=True, slots=True)
class RoutePlan:
    dst: IPNetwork
    via: IPAddress | None
    dev: str
    table: int = MAIN_ROUTE_TABLE


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
class RoutingPlan:
    ospf: OspfPlan | None = None
    bgp: BgpPlan | None = None


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


type DevicePlan = VlanDevicePlan | VrfDevicePlan


def _empty_bridge_ports() -> Mapping[str, BridgePortPlan]:
    return MappingProxyType({})


def _empty_devices() -> Mapping[str, DevicePlan]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class NodePlan:
    name: str
    kind: NodeKind
    namespace: str
    interfaces: Mapping[str, tuple[IPInterface, ...]]
    routes: tuple[RoutePlan, ...]
    sysctls: Mapping[str, int]
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


@dataclass(frozen=True, slots=True)
class LinkPlan:
    index: int
    kind: LinkKind
    left: EndpointPlan
    right: EndpointPlan
    mtu: int
    netem: NetemPlan | None = None


@dataclass(frozen=True, slots=True)
class TopologyPlan:
    name: str
    fingerprint: str
    nodes: Mapping[str, NodePlan]
    links: tuple[LinkPlan, ...]


def node_interface_addresses(node: NodePlan) -> Mapping[str, tuple[IPInterface, ...]]:
    """Return address declarations for linked and namespace-local devices."""

    return MappingProxyType(
        {
            **node.interfaces,
            **{
                name: device.addresses
                for name, device in node.devices.items()
                if isinstance(device, VlanDevicePlan)
            },
        }
    )


def node_interface_master(node: NodePlan, interface: str) -> str | None:
    """Return the VRF device that owns an interface, if any."""

    return next(
        (
            device.name
            for device in node.devices.values()
            if isinstance(device, VrfDevicePlan) and interface in device.interfaces
        ),
        None,
    )


def node_interface_route_table(node: NodePlan, interface: str) -> int:
    """Return the routing table selected by an interface's VRF membership."""

    master = node_interface_master(node, interface)
    if master is None:
        return MAIN_ROUTE_TABLE
    device = node.devices[master]
    assert isinstance(device, VrfDevicePlan)
    return device.table


def node_route_tables(node: NodePlan) -> tuple[int, ...]:
    """Return the managed route tables for a node in deterministic order."""

    return (
        MAIN_ROUTE_TABLE,
        *(device.table for device in node.devices.values() if isinstance(device, VrfDevicePlan)),
    )


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
    sysctls = MappingProxyType(dict(manifest_node.sysctls))
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
            else:
                assert isinstance(config, VrfDeviceConfig)
                compiled_devices[device_name] = VrfDevicePlan(
                    name=device_name,
                    table=config.table,
                    interfaces=tuple(config.interfaces),
                )
    devices: Mapping[str, DevicePlan] = MappingProxyType(compiled_devices)
    tables_by_interface = {
        interface: device.table
        for device in devices.values()
        if isinstance(device, VrfDevicePlan)
        for interface in device.interfaces
    }
    routes = tuple(
        RoutePlan(
            dst=ip_network(str(route.dst)),
            via=ip_address(str(route.via)) if route.via is not None else None,
            dev=route.dev,
            table=tables_by_interface.get(route.dev, MAIN_ROUTE_TABLE),
        )
        for route in manifest_node.routes
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


def _compile_routing(config: RoutingConfig | None) -> RoutingPlan | None:
    if config is None:
        return None
    return RoutingPlan(
        ospf=None if config.ospf is None else _compile_ospf(config.ospf),
        bgp=None if config.bgp is None else _compile_bgp(config.bgp),
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
                    )
                ),
            )
        )

    return TopologyPlan(
        name=deployment,
        fingerprint=manifest_fingerprint(manifest),
        nodes=MappingProxyType(dict(mutable_nodes)),
        links=tuple(mutable_links),
    )
