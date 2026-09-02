from __future__ import annotations

import hashlib
import json
import re
from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv4Network,
    IPv6Address,
    IPv6Interface,
    IPv6Network,
)
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml  # type: ignore[import-untyped]
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from nslab.errors import NslabError

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
IFNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
ALLOWED_SYSCTLS = frozenset(
    {
        "net.ipv4.ip_forward",
        "net.ipv6.conf.all.forwarding",
    }
)

type IPAddress = IPv4Address | IPv6Address
type IPInterface = IPv4Interface | IPv6Interface
type IPNetwork = IPv4Network | IPv6Network


def _require_name(value: str, pattern: re.Pattern[str], label: str) -> str:
    if pattern.fullmatch(value) is None:
        raise ValueError(f"invalid {label}: {value!r}")
    return value


class InterfaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    addresses: tuple[IPv4Interface | IPv6Interface, ...] = ()

    @field_validator("addresses", mode="before")
    @classmethod
    def validate_address_inputs(cls, addresses: object) -> object:
        if not isinstance(addresses, (list, tuple)):
            raise ValueError("interface addresses must be a list or tuple")
        for address in addresses:
            if not isinstance(address, (str, IPv4Interface, IPv6Interface)):
                raise ValueError("interface addresses must be IPv4 or IPv6 strings")
        return addresses

    @field_validator("addresses")
    @classmethod
    def validate_unique_addresses(
        cls, addresses: tuple[IPInterface, ...]
    ) -> tuple[IPInterface, ...]:
        seen_addresses: set[IPInterface] = set()
        for address in addresses:
            if address in seen_addresses:
                raise ValueError(f"duplicate interface address: {str(address)!r}")
            seen_addresses.add(address)
        return addresses


class RouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dst: IPv4Network | IPv6Network
    via: IPv4Address | IPv6Address | None = None
    dev: str

    @field_validator("dst", mode="before")
    @classmethod
    def normalize_default_route(cls, value: object) -> object:
        if not isinstance(value, (str, IPv4Network, IPv6Network)):
            raise ValueError("route destination must be an IPv4 or IPv6 string")
        if value == "default":
            return "0.0.0.0/0"
        return value

    @field_validator("via", mode="before")
    @classmethod
    def validate_gateway_input(cls, value: object) -> object:
        if value is not None and not isinstance(value, (str, IPv4Address, IPv6Address)):
            raise ValueError("route gateway must be an IPv4 or IPv6 string")
        return value

    @field_validator("dev")
    @classmethod
    def validate_device_name(cls, value: str) -> str:
        return _require_name(value, IFNAME_PATTERN, "route interface name")

    @model_validator(mode="after")
    def validate_address_family(self) -> Self:
        if self.via is not None and self.dst.version != self.via.version:
            raise ValueError("route destination and gateway must use the same address family")
        return self


RoutingAsn = Annotated[StrictInt, Field(ge=1, le=4_294_967_295)]


def _validate_sequence(value: object, label: str) -> object:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a list or tuple")
    return value


class OspfConfig(BaseModel):
    """The small, deterministic OSPFv2 subset emitted to FRRouting."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    router_id: IPv4Address
    area: IPv4Address = IPv4Address("0.0.0.0")
    networks: tuple[IPv4Network, ...] = ()
    passive_interfaces: tuple[str, ...] = ()

    @field_validator("networks", mode="before")
    @classmethod
    def validate_network_inputs(cls, value: object) -> object:
        return _validate_sequence(value, "OSPF networks")

    @field_validator("networks")
    @classmethod
    def validate_unique_networks(cls, networks: tuple[IPv4Network, ...]) -> tuple[IPv4Network, ...]:
        if len(set(networks)) != len(networks):
            raise ValueError("OSPF networks must be unique")
        return networks

    @field_validator("passive_interfaces", mode="before")
    @classmethod
    def validate_passive_interface_inputs(cls, value: object) -> object:
        return _validate_sequence(value, "OSPF passive_interfaces")

    @field_validator("passive_interfaces")
    @classmethod
    def validate_passive_interfaces(cls, interfaces: tuple[str, ...]) -> tuple[str, ...]:
        for interface in interfaces:
            _require_name(interface, IFNAME_PATTERN, "OSPF interface name")
        if len(set(interfaces)) != len(interfaces):
            raise ValueError("OSPF passive_interfaces must be unique")
        return interfaces


class BgpNeighborConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    address: IPv4Address
    remote_as: RoutingAsn


class BgpConfig(BaseModel):
    """The directly-connected IPv4 BGP subset emitted to FRRouting."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_as: RoutingAsn
    router_id: IPv4Address
    neighbors: tuple[BgpNeighborConfig, ...]
    networks: tuple[IPv4Network, ...] = ()

    @field_validator("networks", mode="before")
    @classmethod
    def validate_network_inputs(cls, value: object) -> object:
        return _validate_sequence(value, "BGP networks")

    @field_validator("networks")
    @classmethod
    def validate_unique_networks(cls, networks: tuple[IPv4Network, ...]) -> tuple[IPv4Network, ...]:
        if len(set(networks)) != len(networks):
            raise ValueError("BGP networks must be unique")
        return networks

    @field_validator("neighbors", mode="before")
    @classmethod
    def validate_neighbor_inputs(cls, value: object) -> object:
        return _validate_sequence(value, "BGP neighbors")

    @field_validator("neighbors")
    @classmethod
    def validate_unique_neighbors(
        cls, neighbors: tuple[BgpNeighborConfig, ...]
    ) -> tuple[BgpNeighborConfig, ...]:
        addresses = [neighbor.address for neighbor in neighbors]
        if len(set(addresses)) != len(addresses):
            raise ValueError("BGP neighbor addresses must be unique")
        return neighbors


class RoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ospf: OspfConfig | None = None
    bgp: BgpConfig | None = None

    @model_validator(mode="after")
    def validate_protocols(self) -> Self:
        if self.ospf is None and self.bgp is None:
            raise ValueError("routing must enable OSPF or BGP")
        return self


SysctlValue = Annotated[StrictInt, Field(ge=0, le=1)]


class _NodeBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    interfaces: dict[str, InterfaceConfig] = Field(default_factory=dict)
    routes: tuple[RouteConfig, ...] = ()
    sysctls: dict[str, SysctlValue] = Field(default_factory=dict)
    routing: RoutingConfig | None = None

    @field_validator("interfaces")
    @classmethod
    def validate_interface_names(
        cls, interfaces: dict[str, InterfaceConfig]
    ) -> dict[str, InterfaceConfig]:
        for interface_name in interfaces:
            _require_name(interface_name, IFNAME_PATTERN, "interface name")
        return interfaces

    @field_validator("routes")
    @classmethod
    def validate_unique_route_destinations(
        cls, routes: tuple[RouteConfig, ...]
    ) -> tuple[RouteConfig, ...]:
        seen_destinations: set[IPNetwork] = set()
        for route in routes:
            if route.dst in seen_destinations:
                raise ValueError(f"duplicate route destination: {str(route.dst)!r}")
            seen_destinations.add(route.dst)
        return routes

    @field_validator("sysctls", mode="before")
    @classmethod
    def validate_sysctls(cls, sysctls: object) -> object:
        if not isinstance(sysctls, dict):
            return sysctls

        for key, value in sysctls.items():
            if key not in ALLOWED_SYSCTLS:
                raise ValueError(f"unsupported sysctl: {key!r}")
            if type(value) is not int or value not in (0, 1):
                raise ValueError(f"sysctl {key!r} must be integer 0 or 1")
        return sysctls

    @model_validator(mode="after")
    def validate_connected_routes(self) -> Self:
        connected_networks = {
            address.network
            for interface in self.interfaces.values()
            for address in interface.addresses
        }
        for route in self.routes:
            if route.dst in connected_networks:
                raise ValueError(
                    f"route destination conflicts with connected network: {str(route.dst)!r}"
                )
        return self


class LinuxNode(_NodeBase):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["linux"]


BridgePriority = Annotated[StrictInt, Field(ge=0, le=65535)]
BridgePortPriority = Annotated[StrictInt, Field(ge=0, le=63)]
BridgePathCost = Annotated[StrictInt, Field(ge=1, le=65535)]
BridgeVlanId = Annotated[StrictInt, Field(ge=1, le=4094)]


class BridgeVlanConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    vid: BridgeVlanId
    pvid: StrictBool = False
    untagged: StrictBool = False


class BridgePortConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path_cost: BridgePathCost | None = None
    priority: BridgePortPriority | None = None
    vlans: tuple[BridgeVlanConfig, ...] = ()

    @field_validator("vlans")
    @classmethod
    def validate_vlans(cls, vlans: tuple[BridgeVlanConfig, ...]) -> tuple[BridgeVlanConfig, ...]:
        seen: set[int] = set()
        pvids = 0
        for vlan in vlans:
            if vlan.vid in seen:
                raise ValueError(f"duplicate bridge VLAN: {vlan.vid}")
            seen.add(vlan.vid)
            pvids += int(vlan.pvid)
        if pvids > 1:
            raise ValueError("bridge port may declare at most one PVID")
        return vlans

    @model_validator(mode="after")
    def validate_nonempty_settings(self) -> Self:
        if self.path_cost is None and self.priority is None and not self.vlans:
            raise ValueError("bridge port settings must declare STP or VLAN settings")
        return self


class BridgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    stp: StrictBool
    vlan_filtering: StrictBool
    priority: BridgePriority | None = None
    ports: dict[str, BridgePortConfig] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_bridge_name(cls, value: str) -> str:
        name = _require_name(value, IFNAME_PATTERN, "bridge interface name")
        if name == "lo":
            raise ValueError("bridge interface name cannot be 'lo'")
        return name

    @field_validator("ports")
    @classmethod
    def validate_port_names(cls, ports: dict[str, BridgePortConfig]) -> dict[str, BridgePortConfig]:
        for port_name in ports:
            _require_name(port_name, IFNAME_PATTERN, "bridge port interface name")
            if port_name == "lo":
                raise ValueError("bridge port interface name cannot be 'lo'")
        return ports

    @model_validator(mode="after")
    def validate_port_settings(self) -> Self:
        if not self.stp and any(
            port.path_cost is not None or port.priority is not None for port in self.ports.values()
        ):
            raise ValueError("bridge port STP settings require stp: true")
        if not self.vlan_filtering and any(port.vlans for port in self.ports.values()):
            raise ValueError("bridge port VLAN settings require vlan_filtering: true")
        return self


class BridgeNode(_NodeBase):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["bridge"]
    bridge: BridgeConfig


type NodeConfig = Annotated[LinuxNode | BridgeNode, Field(discriminator="kind")]


NetemDelayMs = Annotated[StrictInt, Field(ge=0, le=60_000)]
NetemLossPercent = Annotated[StrictInt, Field(ge=0, le=100)]


class NetemConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delay_ms: NetemDelayMs = 0
    jitter_ms: NetemDelayMs = 0
    loss_percent: NetemLossPercent = 0

    @model_validator(mode="after")
    def validate_effects(self) -> Self:
        if self.jitter_ms and not self.delay_ms:
            raise ValueError("netem jitter_ms requires delay_ms")
        if not (self.delay_ms or self.jitter_ms or self.loss_percent):
            raise ValueError("netem must declare a non-zero delay, jitter, or loss")
        return self


class LinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["veth"] = "veth"
    endpoints: tuple[str, str]
    mtu: Annotated[StrictInt, Field(ge=576, le=9216)] = 1500
    netem: NetemConfig | None = None


class Topology(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    nodes: dict[str, NodeConfig]
    links: tuple[LinkConfig, ...]

    @field_validator("nodes")
    @classmethod
    def validate_node_names(cls, nodes: dict[str, NodeConfig]) -> dict[str, NodeConfig]:
        for node_name in nodes:
            _require_name(node_name, NAME_PATTERN, "node name")
        return nodes

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        linked_interfaces = {node_name: set[str]() for node_name in self.nodes}
        used_endpoints: set[str] = set()
        ospf_router_ids: dict[IPv4Address, str] = {}
        bgp_router_ids: dict[IPv4Address, str] = {}

        for link in self.links:
            for endpoint in link.endpoints:
                node_name, separator, interface_name = endpoint.partition(":")
                if not separator or not node_name or not interface_name:
                    raise ValueError(f"invalid link endpoint: {endpoint!r}")
                _require_name(interface_name, IFNAME_PATTERN, "endpoint interface name")
                if interface_name == "lo":
                    raise ValueError(f"link endpoint cannot use loopback: {endpoint!r}")
                if node_name not in self.nodes:
                    raise ValueError(f"link endpoint references unknown node: {node_name!r}")
                if endpoint in used_endpoints:
                    raise ValueError(f"link endpoint is used more than once: {endpoint!r}")

                used_endpoints.add(endpoint)
                linked_interfaces[node_name].add(interface_name)

        for node_name, node in self.nodes.items():
            linked = linked_interfaces[node_name]
            available = linked.copy()
            if isinstance(node, BridgeNode):
                if node.bridge.name in linked:
                    raise ValueError(
                        f"bridge interface conflicts with a linked endpoint on node "
                        f"{node_name!r}: {node.bridge.name!r}"
                    )
                available.add(node.bridge.name)
                for port_name in node.bridge.ports:
                    if port_name not in linked:
                        raise ValueError(
                            f"configured bridge port is not linked on node "
                            f"{node_name!r}: {port_name!r}"
                        )

            for interface_name in node.interfaces:
                if interface_name not in available:
                    raise ValueError(
                        f"configured interface is not linked: {node_name}:{interface_name}"
                    )
            for route in node.routes:
                if route.dev not in available:
                    raise ValueError(
                        f"route device is not linked on node {node_name!r}: {route.dev!r}"
                    )

            routing = node.routing
            if routing is None:
                continue
            if not isinstance(node, LinuxNode):
                raise ValueError(f"dynamic routing is only supported on linux nodes: {node_name!r}")
            if node.sysctls.get("net.ipv4.ip_forward") != 1:
                raise ValueError(
                    f"dynamic routing requires net.ipv4.ip_forward: 1 on node {node_name!r}"
                )

            if routing.ospf is not None:
                previous = ospf_router_ids.get(routing.ospf.router_id)
                if previous is not None:
                    raise ValueError(
                        f"duplicate OSPF router_id {routing.ospf.router_id} on nodes "
                        f"{previous!r} and {node_name!r}"
                    )
                ospf_router_ids[routing.ospf.router_id] = node_name
                for interface in routing.ospf.passive_interfaces:
                    if interface not in available:
                        raise ValueError(
                            f"OSPF passive interface is not linked on node "
                            f"{node_name!r}: {interface!r}"
                        )

            if routing.bgp is not None:
                previous = bgp_router_ids.get(routing.bgp.router_id)
                if previous is not None:
                    raise ValueError(
                        f"duplicate BGP router_id {routing.bgp.router_id} on nodes "
                        f"{previous!r} and {node_name!r}"
                    )
                bgp_router_ids[routing.bgp.router_id] = node_name
                interface_networks = tuple(
                    address.network
                    for interface_name, interface in node.interfaces.items()
                    if interface_name in available
                    for address in interface.addresses
                    if address.version == 4
                )
                for neighbor in routing.bgp.neighbors:
                    if not any(neighbor.address in network for network in interface_networks):
                        raise ValueError(
                            f"BGP neighbor {neighbor.address} is not directly connected on "
                            f"node {node_name!r}"
                        )

        return self


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    name: str
    topology: Topology

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("manifest version must be integer 1")
        return value

    @field_validator("name")
    @classmethod
    def validate_deployment_name(cls, value: str) -> str:
        return _require_name(value, NAME_PATTERN, "deployment name")


def _validation_issues(error: ValidationError) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = json.loads(error.json(include_url=False, include_input=False))
    return issues


def _manifest_error(path: Path, issues: object) -> NslabError:
    return NslabError(
        code="MANIFEST_INVALID",
        message=f"invalid topology manifest: {path}",
        details={"path": str(path), "issues": issues},
    )


def load_manifest(path: Path) -> Manifest:
    absolute_path = path.expanduser().resolve()
    try:
        with absolute_path.open(encoding="utf-8") as manifest_file:
            document = yaml.safe_load(manifest_file)
    except (UnicodeError, yaml.YAMLError) as error:
        issue: dict[str, object] = {"type": "yaml_error", "msg": str(error)}
        problem_mark = getattr(error, "problem_mark", None)
        line = getattr(problem_mark, "line", None)
        column = getattr(problem_mark, "column", None)
        if isinstance(line, int):
            issue["line"] = line + 1
        if isinstance(column, int):
            issue["column"] = column + 1
        raise _manifest_error(absolute_path, [issue]) from error
    except OSError as error:
        issue = {"type": "file_error", "msg": str(error)}
        raise _manifest_error(absolute_path, [issue]) from error

    try:
        return Manifest.model_validate(document)
    except ValidationError as error:
        raise _manifest_error(absolute_path, _validation_issues(error)) from error


def normalized_manifest(manifest: Manifest) -> dict[str, object]:
    normalized: dict[str, object] = manifest.model_dump(mode="json", exclude_none=True)
    topology = normalized["topology"]
    assert isinstance(topology, dict)
    nodes = topology["nodes"]
    assert isinstance(nodes, dict)
    for node_name, node in manifest.topology.nodes.items():
        if not isinstance(node, BridgeNode) or node.bridge.ports:
            continue
        node_document = nodes[node_name]
        assert isinstance(node_document, dict)
        bridge_document = node_document["bridge"]
        assert isinstance(bridge_document, dict)
        bridge_document.pop("ports", None)
    for node_name, node in manifest.topology.nodes.items():
        if not isinstance(node, BridgeNode) or not node.bridge.ports:
            continue
        node_document = nodes[node_name]
        assert isinstance(node_document, dict)
        bridge_document = node_document["bridge"]
        assert isinstance(bridge_document, dict)
        ports_document = bridge_document["ports"]
        assert isinstance(ports_document, dict)
        for port_name, port in node.bridge.ports.items():
            if port.vlans:
                continue
            port_document = ports_document[port_name]
            assert isinstance(port_document, dict)
            port_document.pop("vlans", None)
    return normalized


def manifest_fingerprint(manifest: Manifest) -> str:
    encoded = json.dumps(
        normalized_manifest(manifest),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
