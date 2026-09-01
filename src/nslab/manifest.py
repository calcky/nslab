from __future__ import annotations

import hashlib
import json
import re
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
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
ALLOWED_SYSCTLS = frozenset({"net.ipv4.ip_forward"})


def _require_name(value: str, pattern: re.Pattern[str], label: str) -> str:
    if pattern.fullmatch(value) is None:
        raise ValueError(f"invalid {label}: {value!r}")
    return value


class InterfaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    addresses: tuple[IPv4Interface, ...] = ()

    @field_validator("addresses", mode="before")
    @classmethod
    def validate_address_inputs(cls, addresses: object) -> object:
        if not isinstance(addresses, (list, tuple)):
            raise ValueError("interface addresses must be a list or tuple")
        for address in addresses:
            if not isinstance(address, (str, IPv4Interface)):
                raise ValueError("interface addresses must be IPv4 strings")
        return addresses

    @field_validator("addresses")
    @classmethod
    def validate_unique_addresses(
        cls, addresses: tuple[IPv4Interface, ...]
    ) -> tuple[IPv4Interface, ...]:
        seen_addresses: set[IPv4Interface] = set()
        for address in addresses:
            if address in seen_addresses:
                raise ValueError(f"duplicate interface address: {str(address)!r}")
            seen_addresses.add(address)
        return addresses


class RouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dst: IPv4Network
    via: IPv4Address | None = None
    dev: str

    @field_validator("dst", mode="before")
    @classmethod
    def normalize_default_route(cls, value: object) -> object:
        if not isinstance(value, (str, IPv4Network)):
            raise ValueError("route destination must be an IPv4 string")
        if value == "default":
            return "0.0.0.0/0"
        return value

    @field_validator("via", mode="before")
    @classmethod
    def validate_gateway_input(cls, value: object) -> object:
        if value is not None and not isinstance(value, (str, IPv4Address)):
            raise ValueError("route gateway must be an IPv4 string")
        return value

    @field_validator("dev")
    @classmethod
    def validate_device_name(cls, value: str) -> str:
        return _require_name(value, IFNAME_PATTERN, "route interface name")


SysctlValue = Annotated[StrictInt, Field(ge=0, le=1)]


class _NodeBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    interfaces: dict[str, InterfaceConfig] = Field(default_factory=dict)
    routes: tuple[RouteConfig, ...] = ()
    sysctls: dict[str, SysctlValue] = Field(default_factory=dict)

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
        seen_destinations: set[IPv4Network] = set()
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
                    f"route destination conflicts with connected network: "
                    f"{str(route.dst)!r}"
                )
        return self


class LinuxNode(_NodeBase):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["linux"]


class BridgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    stp: StrictBool
    vlan_filtering: StrictBool

    @field_validator("name")
    @classmethod
    def validate_bridge_name(cls, value: str) -> str:
        name = _require_name(value, IFNAME_PATTERN, "bridge interface name")
        if name == "lo":
            raise ValueError("bridge interface name cannot be 'lo'")
        return name


class BridgeNode(_NodeBase):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["bridge"]
    bridge: BridgeConfig


type NodeConfig = Annotated[LinuxNode | BridgeNode, Field(discriminator="kind")]


class LinkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["veth"] = "veth"
    endpoints: tuple[str, str]
    mtu: Annotated[StrictInt, Field(ge=576, le=9216)] = 1500


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
    issues: list[dict[str, Any]] = json.loads(
        error.json(include_url=False, include_input=False)
    )
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
    return normalized


def manifest_fingerprint(manifest: Manifest) -> str:
    encoded = json.dumps(
        normalized_manifest(manifest),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
