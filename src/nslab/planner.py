from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from types import MappingProxyType
from typing import Literal

from nslab.errors import NslabError
from nslab.manifest import NAME_PATTERN, BridgeNode, Manifest, NodeConfig, manifest_fingerprint
from nslab.naming import namespace_name, temporary_veth_names

type NodeKind = Literal["linux", "bridge"]
type LinkKind = Literal["veth"]


@dataclass(frozen=True, slots=True)
class RoutePlan:
    dst: IPv4Network
    via: IPv4Address | None
    dev: str


@dataclass(frozen=True, slots=True)
class NodePlan:
    name: str
    kind: NodeKind
    namespace: str
    interfaces: Mapping[str, tuple[IPv4Interface, ...]]
    routes: tuple[RoutePlan, ...]
    sysctls: Mapping[str, int]
    bridge_name: str | None = None
    stp: bool | None = None
    vlan_filtering: bool | None = None


@dataclass(frozen=True, slots=True)
class EndpointPlan:
    node: str
    interface: str
    namespace: str
    temporary_name: str


@dataclass(frozen=True, slots=True)
class LinkPlan:
    index: int
    kind: LinkKind
    left: EndpointPlan
    right: EndpointPlan
    mtu: int


@dataclass(frozen=True, slots=True)
class TopologyPlan:
    name: str
    fingerprint: str
    nodes: Mapping[str, NodePlan]
    links: tuple[LinkPlan, ...]


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
            interface_name: tuple(IPv4Interface(str(address)) for address in config.addresses)
            for interface_name, config in manifest_node.interfaces.items()
        }
    )
    routes = tuple(
        RoutePlan(
            dst=IPv4Network(str(route.dst)),
            via=IPv4Address(str(route.via)) if route.via is not None else None,
            dev=route.dev,
        )
        for route in manifest_node.routes
    )
    sysctls = MappingProxyType(dict(manifest_node.sysctls))

    if isinstance(manifest_node, BridgeNode):
        bridge_name = manifest_node.bridge.name
        stp = manifest_node.bridge.stp
        vlan_filtering = manifest_node.bridge.vlan_filtering
    else:
        bridge_name = None
        stp = None
        vlan_filtering = None

    return NodePlan(
        name=name,
        kind=manifest_node.kind,
        namespace=namespace_name(deployment, name),
        interfaces=interfaces,
        routes=routes,
        sysctls=sysctls,
        bridge_name=bridge_name,
        stp=stp,
        vlan_filtering=vlan_filtering,
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
            )
        )

    return TopologyPlan(
        name=deployment,
        fingerprint=manifest_fingerprint(manifest),
        nodes=MappingProxyType(dict(mutable_nodes)),
        links=tuple(mutable_links),
    )
