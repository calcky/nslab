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
from nslab.tc import normalize_rate, parse_size

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

MAIN_ROUTE_TABLE = 254
_RESERVED_VRF_TABLES = frozenset({253, 254, 255})


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


VlanDeviceId = Annotated[StrictInt, Field(ge=1, le=4094)]


class VlanDeviceConfig(InterfaceConfig):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["vlan"]
    link: str
    id: VlanDeviceId

    @field_validator("link")
    @classmethod
    def validate_parent_name(cls, value: str) -> str:
        parent = _require_name(value, IFNAME_PATTERN, "VLAN parent interface name")
        if parent == "lo":
            raise ValueError("VLAN parent interface cannot be 'lo'")
        return parent


RouteTableId = Annotated[StrictInt, Field(ge=1, le=4_294_967_295)]
VrfTableId = RouteTableId


class VrfDeviceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["vrf"]
    table: VrfTableId
    interfaces: Annotated[tuple[str, ...], Field(min_length=1)]

    @field_validator("table")
    @classmethod
    def validate_table(cls, value: int) -> int:
        if value in _RESERVED_VRF_TABLES:
            raise ValueError(f"VRF table cannot use reserved table: {value}")
        return value

    @field_validator("interfaces", mode="before")
    @classmethod
    def validate_interface_inputs(cls, value: object) -> object:
        return _validate_sequence(value, "VRF interfaces")

    @field_validator("interfaces")
    @classmethod
    def validate_interfaces(cls, interfaces: tuple[str, ...]) -> tuple[str, ...]:
        for interface in interfaces:
            _require_name(interface, IFNAME_PATTERN, "VRF member interface name")
            if interface == "lo":
                raise ValueError("VRF member interface cannot be 'lo'")
        if len(set(interfaces)) != len(interfaces):
            raise ValueError("VRF member interfaces must be unique")
        return interfaces


BondMiimonMs = Annotated[StrictInt, Field(ge=0, le=60_000)]
BondMinLinks = Annotated[StrictInt, Field(ge=0, le=65_535)]


class BondDeviceConfig(InterfaceConfig):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["bond"]
    mode: Literal["active-backup", "802.3ad"]
    interfaces: Annotated[tuple[str, ...], Field(min_length=2)]
    miimon_ms: BondMiimonMs = 100
    primary: str | None = None
    lacp_rate: Literal["slow", "fast"] | None = None
    xmit_hash_policy: Literal["layer2", "layer2+3", "layer3+4"] | None = None
    min_links: BondMinLinks | None = None

    @field_validator("interfaces", mode="before")
    @classmethod
    def validate_interface_inputs(cls, value: object) -> object:
        return _validate_sequence(value, "bond interfaces")

    @field_validator("interfaces")
    @classmethod
    def validate_interfaces(cls, interfaces: tuple[str, ...]) -> tuple[str, ...]:
        for interface in interfaces:
            _require_name(interface, IFNAME_PATTERN, "bond member interface name")
            if interface == "lo":
                raise ValueError("bond member interface cannot be 'lo'")
        if len(set(interfaces)) != len(interfaces):
            raise ValueError("bond member interfaces must be unique")
        return interfaces

    @field_validator("primary")
    @classmethod
    def validate_primary_name(cls, primary: str | None) -> str | None:
        if primary is None:
            return None
        value = _require_name(primary, IFNAME_PATTERN, "bond primary interface name")
        if value == "lo":
            raise ValueError("bond primary interface cannot be 'lo'")
        return value

    @model_validator(mode="after")
    def validate_mode_options(self) -> Self:
        if self.primary is not None:
            if self.mode != "active-backup":
                raise ValueError("bond primary is only valid in active-backup mode")
            if self.primary not in self.interfaces:
                raise ValueError("bond primary must be one of its member interfaces")

        lacp_options = (self.lacp_rate, self.xmit_hash_policy, self.min_links)
        if self.mode != "802.3ad" and any(value is not None for value in lacp_options):
            raise ValueError("bond lacp_rate, xmit_hash_policy, and min_links require 802.3ad mode")
        if self.min_links is not None and self.min_links > len(self.interfaces):
            raise ValueError("bond min_links cannot exceed the number of member interfaces")
        return self


VxlanVni = Annotated[StrictInt, Field(ge=1, le=16_777_215)]
VxlanPort = Annotated[StrictInt, Field(ge=1, le=65_535)]
VxlanMtu = Annotated[StrictInt, Field(ge=576, le=9_216)]


class VxlanDeviceConfig(InterfaceConfig):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["vxlan"]
    vni: VxlanVni
    link: str
    local: IPv4Address | IPv6Address
    remote: IPv4Address | IPv6Address
    dst_port: VxlanPort = 4789
    learning: StrictBool = True
    mtu: VxlanMtu | None = None

    @field_validator("link")
    @classmethod
    def validate_link_name(cls, value: str) -> str:
        link = _require_name(value, IFNAME_PATTERN, "VXLAN underlay interface name")
        if link == "lo":
            raise ValueError("VXLAN underlay interface cannot be 'lo'")
        return link

    @field_validator("local", "remote", mode="before")
    @classmethod
    def validate_address_input(cls, value: object) -> object:
        if not isinstance(value, (str, IPv4Address, IPv6Address)):
            raise ValueError("VXLAN endpoints must be IPv4 or IPv6 strings")
        return value

    @field_validator("local", "remote")
    @classmethod
    def validate_unicast_address(cls, value: IPAddress) -> IPAddress:
        if value.is_unspecified or value.is_multicast:
            raise ValueError("VXLAN endpoints must be unicast addresses")
        return value

    @model_validator(mode="after")
    def validate_endpoints(self) -> Self:
        if self.local.version != self.remote.version:
            raise ValueError("VXLAN local and remote must use the same address family")
        if self.local == self.remote:
            raise ValueError("VXLAN local and remote addresses must be different")
        return self


type LinuxDeviceConfig = Annotated[
    VlanDeviceConfig | VrfDeviceConfig | BondDeviceConfig | VxlanDeviceConfig,
    Field(discriminator="type"),
]


class RouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dst: IPv4Network | IPv6Network
    via: IPv4Address | IPv6Address | None = None
    dev: str
    table: RouteTableId | None = None

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

    @field_validator("table")
    @classmethod
    def validate_table(cls, value: int | None) -> int | None:
        if value == 255:
            raise ValueError("route cannot use reserved local table: 255")
        return value

    @model_validator(mode="after")
    def validate_address_family(self) -> Self:
        if self.via is not None and self.dst.version != self.via.version:
            raise ValueError("route destination and gateway must use the same address family")
        return self


PolicyRulePriority = Annotated[StrictInt, Field(ge=1, le=4_294_967_295)]
PolicyRuleUint32 = Annotated[StrictInt, Field(ge=0, le=4_294_967_295)]
PolicyRuleUint64 = Annotated[StrictInt, Field(ge=0, le=18_446_744_073_709_551_615)]
PolicyRuleUint16 = Annotated[StrictInt, Field(ge=0, le=65535)]
PolicyRuleUint8 = Annotated[StrictInt, Field(ge=0, le=255)]
PolicyRuleInterfaceGroup = Annotated[StrictInt, Field(ge=0, le=4_294_967_294)]
PolicyRuleFamily = Literal["ipv4", "ipv6"]
PolicyRuleAction = Literal[
    "lookup",
    "goto",
    "nop",
    "blackhole",
    "unreachable",
    "prohibit",
]


class PolicyRuleUidRangeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: PolicyRuleUint32
    end: PolicyRuleUint32

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.start > self.end:
            raise ValueError("policy rule UID range start cannot exceed end")
        return self


class PolicyRulePortRangeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: PolicyRuleUint16
    end: PolicyRuleUint16

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.start > self.end:
            raise ValueError("policy rule port range start cannot exceed end")
        return self


class PolicyRuleRealmsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: PolicyRuleUint16 = 0
    destination: PolicyRuleUint16 = 0

    @model_validator(mode="after")
    def validate_nonzero(self) -> Self:
        if self.source == 0 and self.destination == 0:
            raise ValueError("policy rule realms must select a source or destination realm")
        return self


class PolicyRuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    priority: PolicyRulePriority
    family: PolicyRuleFamily | None = None
    action: PolicyRuleAction = "lookup"
    table: RouteTableId | None = None
    goto: PolicyRulePriority | None = None
    source: IPv4Network | IPv6Network | None = Field(default=None, alias="from")
    destination: IPv4Network | IPv6Network | None = Field(default=None, alias="to")
    invert: StrictBool = Field(default=False, alias="not")
    tos: PolicyRuleUint8 | None = None
    fwmark: PolicyRuleUint32 | None = None
    fwmask: PolicyRuleUint32 | None = None
    iif: str | None = None
    oif: str | None = None
    l3mdev: StrictBool = False
    uid_range: PolicyRuleUidRangeConfig | None = None
    protocol: PolicyRuleUint8 = 0
    ip_protocol: PolicyRuleUint8 | None = None
    source_port: PolicyRulePortRangeConfig | None = None
    destination_port: PolicyRulePortRangeConfig | None = None
    tunnel_id: PolicyRuleUint64 | None = None
    suppress_prefix_length: PolicyRuleUint8 | None = None
    suppress_interface_group: PolicyRuleInterfaceGroup | None = None
    realms: PolicyRuleRealmsConfig | None = None

    @field_validator("source", mode="before")
    @classmethod
    def validate_source_input(cls, value: object) -> object:
        if value is not None and not isinstance(value, (str, IPv4Network, IPv6Network)):
            raise ValueError("policy rule source must be an IPv4 or IPv6 prefix")
        return value

    @field_validator("destination", mode="before")
    @classmethod
    def validate_destination_input(cls, value: object) -> object:
        if value is not None and not isinstance(value, (str, IPv4Network, IPv6Network)):
            raise ValueError("policy rule destination must be an IPv4 or IPv6 prefix")
        return value

    @field_validator("iif", "oif")
    @classmethod
    def validate_interface_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_name(value, IFNAME_PATTERN, "policy rule interface name")

    @property
    def ip_version(self) -> Literal[4, 6]:
        if self.family is not None:
            return 4 if self.family == "ipv4" else 6
        if self.source is not None:
            return 4 if self.source.version == 4 else 6
        if self.destination is not None:
            return 4 if self.destination.version == 4 else 6
        return 4

    @model_validator(mode="after")
    def validate_rule(self) -> Self:
        version = self.ip_version
        for label, network in (("source", self.source), ("destination", self.destination)):
            if network is not None and network.version != version:
                raise ValueError(f"policy rule {label} does not match IPv{version} rule family")
        if (
            self.source is not None
            and self.destination is not None
            and self.source.version != self.destination.version
        ):
            raise ValueError("policy rule source and destination families must match")
        if self.fwmask is not None and self.fwmark is None:
            raise ValueError("policy rule fwmask requires fwmark")
        if (self.source_port is not None or self.destination_port is not None) and (
            self.ip_protocol in {None, 0}
        ):
            raise ValueError("policy rule port ranges require a nonzero ip_protocol")
        if self.suppress_prefix_length is not None:
            maximum = 32 if version == 4 else 128
            if self.suppress_prefix_length > maximum:
                raise ValueError(
                    f"policy rule suppress_prefix_length must be at most {maximum} for IPv{version}"
                )

        if self.action == "lookup":
            if self.goto is not None:
                raise ValueError("lookup policy rule cannot declare goto")
            if self.l3mdev:
                if self.table is not None:
                    raise ValueError("l3mdev policy rule cannot declare table")
            elif self.table is None:
                raise ValueError("lookup policy rule requires table")
        elif self.action == "goto":
            if self.goto is None:
                raise ValueError("goto policy rule requires goto")
            if self.table is not None:
                raise ValueError("goto policy rule cannot declare table")
            if self.suppress_prefix_length is not None or self.suppress_interface_group is not None:
                raise ValueError("goto policy rule cannot declare suppress options")
        else:
            if self.table is not None or self.goto is not None:
                raise ValueError(f"{self.action} policy rule cannot declare table or goto")
            if self.suppress_prefix_length is not None or self.suppress_interface_group is not None:
                raise ValueError(f"{self.action} policy rule cannot declare suppress options")
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


def _validate_routes_by_table(
    routes: tuple[RouteConfig, ...],
    connected_networks: set[tuple[int, IPNetwork]],
    tables_by_interface: dict[str, int],
) -> None:
    seen_destinations: set[tuple[int, IPNetwork]] = set()
    for route in routes:
        table = (
            route.table
            if route.table is not None
            else tables_by_interface.get(route.dev, MAIN_ROUTE_TABLE)
        )
        identity = (table, route.dst)
        if identity in seen_destinations:
            raise ValueError(f"duplicate route destination: {str(route.dst)!r}")
        if identity in connected_networks:
            raise ValueError(
                f"route destination conflicts with connected network: {str(route.dst)!r}"
            )
        seen_destinations.add(identity)


class LinuxNode(_NodeBase):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["linux"]
    devices: dict[str, LinuxDeviceConfig] = Field(default_factory=dict)
    rules: tuple[PolicyRuleConfig, ...] = ()

    @field_validator("devices")
    @classmethod
    def validate_device_names(
        cls, devices: dict[str, LinuxDeviceConfig]
    ) -> dict[str, LinuxDeviceConfig]:
        for device_name in devices:
            _require_name(device_name, IFNAME_PATTERN, "device name")
            if device_name == "lo":
                raise ValueError("device name cannot be 'lo'")
        return devices

    @field_validator("rules", mode="before")
    @classmethod
    def validate_rule_inputs(cls, value: object) -> object:
        return _validate_sequence(value, "policy rules")

    @field_validator("rules")
    @classmethod
    def validate_rule_priorities(
        cls, rules: tuple[PolicyRuleConfig, ...]
    ) -> tuple[PolicyRuleConfig, ...]:
        seen: set[tuple[int, int]] = set()
        for rule in rules:
            identity = (rule.ip_version, rule.priority)
            if identity in seen:
                raise ValueError(
                    f"duplicate policy rule priority for IPv{rule.ip_version}: {rule.priority}"
                )
            seen.add(identity)
        for rule in rules:
            if rule.action != "goto":
                continue
            assert rule.goto is not None
            if rule.goto <= rule.priority:
                raise ValueError("policy rule goto must target a greater priority")
        return rules

    @model_validator(mode="after")
    def validate_devices_and_routes(self) -> Self:
        collisions = set(self.interfaces) & set(self.devices)
        if collisions:
            name = sorted(collisions)[0]
            raise ValueError(f"device name conflicts with an interface: {name!r}")

        vlan_devices = {
            name: device
            for name, device in self.devices.items()
            if isinstance(device, VlanDeviceConfig)
        }
        vrf_devices = {
            name: device
            for name, device in self.devices.items()
            if isinstance(device, VrfDeviceConfig)
        }
        bond_devices = {
            name: device
            for name, device in self.devices.items()
            if isinstance(device, BondDeviceConfig)
        }
        vxlan_devices = {
            name: device
            for name, device in self.devices.items()
            if isinstance(device, VxlanDeviceConfig)
        }

        bond_by_member: dict[str, str] = {}
        for name, bond in bond_devices.items():
            for interface in bond.interfaces:
                if interface in self.devices:
                    raise ValueError(
                        f"bond member must be a linked interface: {name!r} -> {interface!r}"
                    )
                previous_bond = bond_by_member.get(interface)
                if previous_bond is not None:
                    raise ValueError(f"interface belongs to more than one bond: {interface!r}")
                config = self.interfaces.get(interface)
                if config is not None and config.addresses:
                    raise ValueError(
                        f"bond member interface cannot declare addresses: {interface!r}"
                    )
                bond_by_member[interface] = name

        seen_vlan_links: set[tuple[str, int]] = set()
        for name, device in vlan_devices.items():
            if device.link in self.devices:
                raise ValueError(
                    f"VLAN device parent must be a linked interface: {name!r} -> {device.link!r}"
                )
            identity = (device.link, device.id)
            if identity in seen_vlan_links:
                raise ValueError(
                    f"duplicate VLAN ID {device.id} on parent interface {device.link!r}"
                )
            seen_vlan_links.add(identity)

        seen_vxlan_vnis: set[int] = set()
        for name, vxlan in vxlan_devices.items():
            if vxlan.link in self.devices:
                raise ValueError(
                    f"VXLAN underlay interface must be a linked interface: "
                    f"{name!r} -> {vxlan.link!r}"
                )
            if vxlan.vni in seen_vxlan_vnis:
                raise ValueError(f"duplicate VXLAN VNI: {vxlan.vni}")
            seen_vxlan_vnis.add(vxlan.vni)

        seen_tables: set[int] = set()
        tables_by_interface: dict[str, int] = {}
        for name, vrf in vrf_devices.items():
            if vrf.table in seen_tables:
                raise ValueError(f"duplicate VRF table: {vrf.table}")
            seen_tables.add(vrf.table)
            for interface in vrf.interfaces:
                if interface in vrf_devices or interface in bond_devices:
                    raise ValueError(
                        f"VRF member must be a linked interface or VLAN device: "
                        f"{name!r} -> {interface!r}"
                    )
                if interface in bond_by_member:
                    raise ValueError(
                        f"bond member interface cannot belong directly to a VRF: {interface!r}"
                    )
                previous = tables_by_interface.get(interface)
                if previous is not None:
                    raise ValueError(f"interface belongs to more than one VRF: {interface!r}")
                tables_by_interface[interface] = vrf.table

        if self.routing is not None and vrf_devices:
            raise ValueError("dynamic routing with VRF devices is not supported")

        if vrf_devices and any(rule.priority == 1000 for rule in self.rules):
            raise ValueError("policy rule priority 1000 is reserved when VRF devices are present")

        for route in self.routes:
            vrf_table = tables_by_interface.get(route.dev)
            if vrf_table is not None and route.table is not None and route.table != vrf_table:
                raise ValueError(
                    f"route table conflicts with VRF member interface {route.dev!r}: "
                    f"expected {vrf_table}, got {route.table}"
                )

        connected_networks = {
            (tables_by_interface.get(interface, MAIN_ROUTE_TABLE), address.network)
            for interface, config in self.interfaces.items()
            for address in config.addresses
        }
        connected_networks.update(
            (tables_by_interface.get(name, MAIN_ROUTE_TABLE), address.network)
            for name, device in vlan_devices.items()
            for address in device.addresses
        )
        connected_networks.update(
            (MAIN_ROUTE_TABLE, address.network)
            for device in bond_devices.values()
            for address in device.addresses
        )
        connected_networks.update(
            (tables_by_interface.get(name, MAIN_ROUTE_TABLE), address.network)
            for name, device in vxlan_devices.items()
            for address in device.addresses
        )
        _validate_routes_by_table(self.routes, connected_networks, tables_by_interface)
        return self


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
    devices: dict[str, VxlanDeviceConfig] = Field(default_factory=dict)
    bridge: BridgeConfig

    @field_validator("devices")
    @classmethod
    def validate_device_names(
        cls, devices: dict[str, VxlanDeviceConfig]
    ) -> dict[str, VxlanDeviceConfig]:
        for device_name in devices:
            _require_name(device_name, IFNAME_PATTERN, "device name")
            if device_name == "lo":
                raise ValueError("device name cannot be 'lo'")
        return devices

    @model_validator(mode="after")
    def validate_routes(self) -> Self:
        collisions = set(self.interfaces) & set(self.devices)
        if collisions:
            name = sorted(collisions)[0]
            raise ValueError(f"device name conflicts with an interface: {name!r}")
        if self.bridge.name in self.devices:
            raise ValueError(
                f"device name conflicts with the bridge interface: {self.bridge.name!r}"
            )
        seen_vnis: set[int] = set()
        for device_name, device in self.devices.items():
            if device.addresses:
                raise ValueError(f"bridge VXLAN device cannot declare addresses: {device_name!r}")
            if device.vni in seen_vnis:
                raise ValueError(f"duplicate VXLAN VNI: {device.vni}")
            seen_vnis.add(device.vni)

        connected_networks = {
            (MAIN_ROUTE_TABLE, address.network)
            for interface in self.interfaces.values()
            for address in interface.addresses
        }
        _validate_routes_by_table(self.routes, connected_networks, {})
        return self


type NodeConfig = Annotated[LinuxNode | BridgeNode, Field(discriminator="kind")]


NetemDelayMs = Annotated[StrictInt, Field(ge=0, le=60_000)]
NetemLossPercent = Annotated[StrictInt, Field(ge=0, le=100)]


class NetemConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delay_ms: NetemDelayMs = 0
    jitter_ms: NetemDelayMs = 0
    loss_percent: NetemLossPercent = 0
    rate: str | None = None

    @field_validator("rate", mode="before")
    @classmethod
    def validate_rate(cls, value: object) -> object:
        if value is None:
            return None
        return normalize_rate(value)

    @model_validator(mode="after")
    def validate_effects(self) -> Self:
        if self.jitter_ms and not self.delay_ms:
            raise ValueError("netem jitter_ms requires delay_ms")
        if not (self.delay_ms or self.jitter_ms or self.loss_percent or self.rate):
            raise ValueError("netem must declare a non-zero delay, jitter, loss, or rate")
        return self


class TbfConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["tbf"]
    rate: str
    burst: StrictInt = 32 * 1024
    latency_ms: Annotated[StrictInt, Field(ge=1, le=60_000)] = 400

    @field_validator("rate", mode="before")
    @classmethod
    def validate_rate(cls, value: object) -> object:
        return normalize_rate(value)

    @field_validator("burst", mode="before")
    @classmethod
    def validate_burst(cls, value: object) -> object:
        return parse_size(value)


class FqCodelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["fq_codel"]
    target_ms: Annotated[StrictInt, Field(ge=1, le=60_000)] = 5
    interval_ms: Annotated[StrictInt, Field(ge=1, le=60_000)] = 100
    limit: Annotated[StrictInt, Field(ge=1, le=1_000_000)] = 10_240
    ecn: StrictBool = True

    @model_validator(mode="after")
    def validate_timing(self) -> Self:
        if self.target_ms > self.interval_ms:
            raise ValueError("fq_codel target_ms cannot exceed interval_ms")
        return self


type QdiscConfig = Annotated[TbfConfig | FqCodelConfig, Field(discriminator="kind")]


class LinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["veth"] = "veth"
    endpoints: tuple[str, str]
    mtu: Annotated[StrictInt, Field(ge=576, le=9216)] = 1500
    netem: NetemConfig | None = None
    qdisc: QdiscConfig | None = None

    @model_validator(mode="after")
    def validate_qdisc(self) -> Self:
        if self.netem is not None and self.qdisc is not None:
            raise ValueError("link netem and qdisc are mutually exclusive")
        return self


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
        linked_mtus = {node_name: dict[str, int]() for node_name in self.nodes}
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
                linked_mtus[node_name][interface_name] = link.mtu

        for node_name, node in self.nodes.items():
            linked = linked_interfaces[node_name]
            available = linked.copy()
            if isinstance(node, LinuxNode):
                vlan_devices = {
                    name: device
                    for name, device in node.devices.items()
                    if isinstance(device, VlanDeviceConfig)
                }
                vrf_devices = {
                    name: device
                    for name, device in node.devices.items()
                    if isinstance(device, VrfDeviceConfig)
                }
                bond_devices = {
                    name: device
                    for name, device in node.devices.items()
                    if isinstance(device, BondDeviceConfig)
                }
                vxlan_devices = {
                    name: device
                    for name, device in node.devices.items()
                    if isinstance(device, VxlanDeviceConfig)
                }
                for device_name in node.devices:
                    if device_name in linked:
                        raise ValueError(
                            f"device name conflicts with a linked endpoint on node "
                            f"{node_name!r}: {device_name!r}"
                        )
                for device in vlan_devices.values():
                    if device.link not in linked:
                        raise ValueError(
                            f"VLAN parent interface is not linked on node "
                            f"{node_name!r}: {device.link!r}"
                        )
                available.update(vlan_devices)
                for bond_name, bond in bond_devices.items():
                    missing = [
                        interface for interface in bond.interfaces if interface not in linked
                    ]
                    if missing:
                        raise ValueError(
                            f"bond member interface is not linked on node {node_name!r}: "
                            f"{bond_name!r} -> {missing[0]!r}"
                        )
                    member_mtus = {
                        linked_mtus[node_name][interface] for interface in bond.interfaces
                    }
                    if len(member_mtus) != 1:
                        raise ValueError(
                            f"bond member interfaces must use the same MTU on node "
                            f"{node_name!r}: {bond_name!r}"
                        )
                available.update(bond_devices)
                for device_name, vxlan in vxlan_devices.items():
                    if vxlan.link not in linked:
                        raise ValueError(
                            f"VXLAN underlay interface is not linked on node "
                            f"{node_name!r}: {device_name!r} -> {vxlan.link!r}"
                        )
                    configured_addresses = node.interfaces.get(vxlan.link)
                    if configured_addresses is None or vxlan.local not in {
                        address.ip for address in configured_addresses.addresses
                    }:
                        raise ValueError(
                            f"VXLAN local address is not configured on underlay interface "
                            f"{node_name!r}: {device_name!r} -> {vxlan.link!r}"
                        )
                    overhead = 50 if vxlan.local.version == 4 else 70
                    maximum_mtu = linked_mtus[node_name][vxlan.link] - overhead
                    if maximum_mtu < 576:
                        raise ValueError(
                            f"VXLAN underlay MTU is too small on node {node_name!r}: "
                            f"{device_name!r}"
                        )
                    if vxlan.mtu is not None and vxlan.mtu > maximum_mtu:
                        raise ValueError(
                            f"VXLAN MTU exceeds encapsulation limit on node {node_name!r}: "
                            f"{device_name!r} maximum is {maximum_mtu}"
                        )
                available.update(vxlan_devices)
                for vrf_name, vrf in vrf_devices.items():
                    for interface in vrf.interfaces:
                        if interface not in available:
                            raise ValueError(
                                f"VRF member interface is not linked or a VLAN device on node "
                                f"{node_name!r}: {vrf_name!r} -> {interface!r}"
                            )
                rule_interfaces = available | set(vrf_devices) | {"lo"}
                for rule in node.rules:
                    for rule_interface in (rule.iif, rule.oif):
                        if rule_interface is not None and rule_interface not in rule_interfaces:
                            raise ValueError(
                                f"policy rule interface is not available on node "
                                f"{node_name!r}: {rule_interface!r}"
                            )
            if isinstance(node, BridgeNode):
                vxlan_devices = node.devices
                for device_name in vxlan_devices:
                    if device_name in linked:
                        raise ValueError(
                            f"device name conflicts with a linked endpoint on node "
                            f"{node_name!r}: {device_name!r}"
                        )
                underlay_interfaces: set[str] = set()
                for device_name, vxlan in vxlan_devices.items():
                    if vxlan.link not in linked:
                        raise ValueError(
                            f"VXLAN underlay interface is not linked on node "
                            f"{node_name!r}: {device_name!r} -> {vxlan.link!r}"
                        )
                    configured_addresses = node.interfaces.get(vxlan.link)
                    if configured_addresses is None or vxlan.local not in {
                        address.ip for address in configured_addresses.addresses
                    }:
                        raise ValueError(
                            f"VXLAN local address is not configured on underlay interface "
                            f"{node_name!r}: {device_name!r} -> {vxlan.link!r}"
                        )
                    overhead = 50 if vxlan.local.version == 4 else 70
                    maximum_mtu = linked_mtus[node_name][vxlan.link] - overhead
                    if maximum_mtu < 576:
                        raise ValueError(
                            f"VXLAN underlay MTU is too small on node {node_name!r}: "
                            f"{device_name!r}"
                        )
                    if vxlan.mtu is not None and vxlan.mtu > maximum_mtu:
                        raise ValueError(
                            f"VXLAN MTU exceeds encapsulation limit on node {node_name!r}: "
                            f"{device_name!r} maximum is {maximum_mtu}"
                        )
                    underlay_interfaces.add(vxlan.link)

                if node.bridge.name in linked:
                    raise ValueError(
                        f"bridge interface conflicts with a linked endpoint on node "
                        f"{node_name!r}: {node.bridge.name!r}"
                    )
                available.add(node.bridge.name)
                for port_name in node.bridge.ports:
                    if port_name in underlay_interfaces:
                        raise ValueError(
                            f"VXLAN underlay interface cannot be a bridge port on node "
                            f"{node_name!r}: {port_name!r}"
                        )
                    if port_name not in linked and port_name not in vxlan_devices:
                        raise ValueError(
                            f"configured bridge port is not linked or a VXLAN device on node "
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
                    for interface in (
                        *node.interfaces.values(),
                        *(
                            device
                            for device in node.devices.values()
                            if isinstance(
                                device,
                                (VlanDeviceConfig, BondDeviceConfig, VxlanDeviceConfig),
                            )
                        ),
                    )
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
    normalized: dict[str, object] = manifest.model_dump(
        mode="json", exclude_none=True, by_alias=True
    )
    topology = normalized["topology"]
    assert isinstance(topology, dict)
    nodes = topology["nodes"]
    assert isinstance(nodes, dict)
    for node_name, node in manifest.topology.nodes.items():
        if isinstance(node, (LinuxNode, BridgeNode)) and not node.devices:
            node_document = nodes[node_name]
            assert isinstance(node_document, dict)
            node_document.pop("devices", None)
        if isinstance(node, LinuxNode):
            node_document = nodes[node_name]
            assert isinstance(node_document, dict)
            devices_document = node_document.get("devices")
            if isinstance(devices_document, dict):
                for device_name, device in node.devices.items():
                    if not isinstance(device, BondDeviceConfig) or device.mode != "802.3ad":
                        continue
                    device_document = devices_document[device_name]
                    assert isinstance(device_document, dict)
                    device_document["lacp_rate"] = device.lacp_rate or "slow"
                    device_document["xmit_hash_policy"] = device.xmit_hash_policy or "layer2"
                    device_document["min_links"] = (
                        0 if device.min_links is None else device.min_links
                    )
        if isinstance(node, LinuxNode) and not node.rules:
            node_document = nodes[node_name]
            assert isinstance(node_document, dict)
            node_document.pop("rules", None)
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
