from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from ipaddress import IPv4Interface
from typing import Literal, cast

from nslab.backend.base import (
    InterfaceInventory,
    LiveInventory,
    expected_main_table_ipv4_routes,
    inventory_matches_plan,
    recorded_link_ids_match_inventory,
)
from nslab.errors import NslabError
from nslab.planner import LinkPlan, NodeKind, NodePlan, RoutePlan, TopologyPlan
from nslab.snapshot import SnapshotValidation, validate_snapshot
from nslab.state import StateSnapshot

type InspectionStatus = Literal["absent", "deployed", "degraded", "stale"]
type NodeStatus = Literal["absent", "matching", "degraded"]
type DifferenceScope = Literal["deployment", "node", "interface", "link"]
type DifferenceSource = Literal["state", "live"]
type InspectionValue = None | bool | int | str | tuple[str | None, ...]


def _value_document(value: InspectionValue) -> object:
    if isinstance(value, tuple):
        return list(value)
    return value


@dataclass(frozen=True, slots=True)
class InterfaceView:
    name: str
    kind: str
    master: str | None
    mtu: int | None
    up: bool
    addresses: tuple[str, ...] = ()
    stp: bool | None = None
    vlan_filtering: bool | None = None
    ifindex: int | None = None
    link_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "addresses", tuple(self.addresses))

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "master": self.master,
            "mtu": self.mtu,
            "up": self.up,
            "addresses": list(self.addresses),
            "stp": self.stp,
            "vlan_filtering": self.vlan_filtering,
            "ifindex": self.ifindex,
            "link_id": self.link_id,
        }


@dataclass(frozen=True, slots=True)
class RouteView:
    dst: str
    via: str | None
    dev: str

    def to_dict(self) -> dict[str, object]:
        return {"dst": self.dst, "via": self.via, "dev": self.dev}


@dataclass(frozen=True, slots=True)
class SysctlView:
    name: str
    value: int | None

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "value": self.value}


@dataclass(frozen=True, slots=True)
class LinkView:
    index: int | None
    kind: str | None
    mtu: int | None
    endpoints: tuple[str, ...]
    endpoint_kinds: tuple[str | None, ...]
    endpoint_mtus: tuple[int | None, ...]
    present: bool
    link_ids: tuple[str | None, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoints", tuple(self.endpoints))
        object.__setattr__(self, "endpoint_kinds", tuple(self.endpoint_kinds))
        object.__setattr__(self, "endpoint_mtus", tuple(self.endpoint_mtus))
        object.__setattr__(self, "link_ids", tuple(self.link_ids))

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "kind": self.kind,
            "mtu": self.mtu,
            "endpoints": list(self.endpoints),
            "endpoint_kinds": list(self.endpoint_kinds),
            "endpoint_mtus": list(self.endpoint_mtus),
            "present": self.present,
            "link_ids": list(self.link_ids),
        }


@dataclass(frozen=True, slots=True)
class RootTemporaryView:
    name: str
    present: bool
    kind: str | None = None
    master: str | None = None
    mtu: int | None = None
    up: bool | None = None
    addresses: tuple[str, ...] = ()
    ifindex: int | None = None
    link_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "addresses", tuple(self.addresses))

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "present": self.present,
            "kind": self.kind,
            "master": self.master,
            "mtu": self.mtu,
            "up": self.up,
            "addresses": list(self.addresses),
            "ifindex": self.ifindex,
            "link_id": self.link_id,
        }


@dataclass(frozen=True, slots=True)
class NodeResourceView:
    name: str | None
    kind: NodeKind | None
    namespace: str | None
    present: bool
    interfaces: tuple[InterfaceView, ...]
    routes: tuple[RouteView, ...]
    sysctls: tuple[SysctlView, ...]
    links: tuple[LinkView, ...]
    root_temporaries: tuple[RootTemporaryView, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "interfaces", tuple(self.interfaces))
        object.__setattr__(self, "routes", tuple(self.routes))
        object.__setattr__(self, "sysctls", tuple(self.sysctls))
        object.__setattr__(self, "links", tuple(self.links))
        object.__setattr__(self, "root_temporaries", tuple(self.root_temporaries))

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "namespace": self.namespace,
            "present": self.present,
            "interfaces": [interface.to_dict() for interface in self.interfaces],
            "routes": [route.to_dict() for route in self.routes],
            "sysctls": [sysctl.to_dict() for sysctl in self.sysctls],
            "links": [link.to_dict() for link in self.links],
            "root_temporaries": [item.to_dict() for item in self.root_temporaries],
        }


@dataclass(frozen=True, slots=True)
class NodeSummary:
    name: str
    kind: NodeKind
    namespace: str
    status: NodeStatus
    desired: NodeResourceView
    state: NodeResourceView | None
    actual: NodeResourceView

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "namespace": self.namespace,
            "status": self.status,
            "desired": self.desired.to_dict(),
            "state": None if self.state is None else self.state.to_dict(),
            "actual": self.actual.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class InspectionDifference:
    scope: DifferenceScope
    source: DifferenceSource
    property: str
    desired: InspectionValue
    actual: InspectionValue
    node: str | None = None
    interface: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "source": self.source,
            "node": self.node,
            "interface": self.interface,
            "property": self.property,
            "desired": _value_document(self.desired),
            "actual": _value_document(self.actual),
        }


@dataclass(frozen=True, slots=True)
class InspectionReport:
    status: InspectionStatus
    nodes: tuple[NodeSummary, ...]
    differences: tuple[InspectionDifference, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "differences", tuple(self.differences))

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "nodes": [node.to_dict() for node in self.nodes],
            "differences": [difference.to_dict() for difference in self.differences],
        }


def _validate_snapshot(snapshot: StateSnapshot, plan: TopologyPlan) -> SnapshotValidation:
    validated = validate_snapshot(snapshot)
    if validated.plan != plan:
        raise NslabError(
            code="PLAN_STATE_MISMATCH",
            message=f"selected topology does not match stored deployment state: {plan.name}",
            details={
                "name": plan.name,
                "plan_fingerprint": plan.fingerprint,
                "snapshot_fingerprint": snapshot.fingerprint,
            },
        )
    return validated


def _address_key(address: IPv4Interface) -> tuple[int, int]:
    return int(address.ip), address.network.prefixlen


def _address_strings(addresses: Sequence[IPv4Interface]) -> tuple[str, ...]:
    return tuple(str(address) for address in sorted(set(addresses), key=_address_key))


def _route_key(route: RoutePlan) -> tuple[int, int, int, str]:
    gateway = -1 if route.via is None else int(route.via)
    return int(route.dst.network_address), route.dst.prefixlen, gateway, route.dev


def _route_view(route: RoutePlan) -> RouteView:
    return RouteView(
        dst=str(route.dst),
        via=None if route.via is None else str(route.via),
        dev=route.dev,
    )


def _route_string(route: RoutePlan) -> str:
    gateway = "-" if route.via is None else str(route.via)
    return f"{route.dst}|{gateway}|{route.dev}"


def _route_strings(routes: Sequence[RoutePlan]) -> tuple[str, ...]:
    return tuple(_route_string(route) for route in sorted(set(routes), key=_route_key))


def _ordered_actual_routes(
    desired: Sequence[RoutePlan],
    actual: Sequence[RoutePlan],
) -> tuple[RoutePlan, ...]:
    actual_set = set(actual)
    ordered = [route for route in desired if route in actual_set]
    ordered.extend(sorted(actual_set - set(ordered), key=_route_key))
    return tuple(ordered)


def _desired_interfaces(node: NodePlan, plan: TopologyPlan) -> tuple[InterfaceView, ...]:
    interfaces = [
        InterfaceView(
            name="lo",
            kind="loopback",
            master=None,
            mtu=None,
            up=True,
            addresses=("127.0.0.1/8",),
        )
    ]
    if node.kind == "bridge":
        assert node.bridge_name is not None
        interfaces.append(
            InterfaceView(
                name=node.bridge_name,
                kind="bridge",
                master=None,
                mtu=None,
                up=True,
                addresses=_address_strings(node.interfaces.get(node.bridge_name, ())),
                stp=node.stp,
                vlan_filtering=node.vlan_filtering,
            )
        )
    for link in plan.links:
        for endpoint in (link.left, link.right):
            if endpoint.namespace != node.namespace:
                continue
            interfaces.append(
                InterfaceView(
                    name=endpoint.interface,
                    kind="veth",
                    master=node.bridge_name if node.kind == "bridge" else None,
                    mtu=link.mtu,
                    up=True,
                    addresses=_address_strings(node.interfaces.get(endpoint.interface, ())),
                )
            )
    return tuple(interfaces)


def _interface_view(interface: InterfaceInventory) -> InterfaceView:
    return InterfaceView(
        name=interface.name,
        kind=interface.kind,
        master=interface.master,
        mtu=interface.mtu,
        up=interface.up,
        addresses=_address_strings(interface.addresses),
        stp=interface.stp,
        vlan_filtering=interface.vlan_filtering,
        ifindex=interface.ifindex,
        link_id=interface.link_id,
    )


def _planned_links_for_node(plan: TopologyPlan, node: str) -> tuple[LinkPlan, ...]:
    return tuple(link for link in plan.links if node in {link.left.node, link.right.node})


def _endpoint_labels(link: LinkPlan) -> tuple[str, str]:
    return (
        f"{link.left.node}:{link.left.interface}",
        f"{link.right.node}:{link.right.interface}",
    )


def _desired_link_view(link: LinkPlan) -> LinkView:
    return LinkView(
        index=link.index,
        kind=link.kind,
        mtu=link.mtu,
        endpoints=_endpoint_labels(link),
        endpoint_kinds=(link.kind, link.kind),
        endpoint_mtus=(link.mtu, link.mtu),
        present=True,
        link_ids=(None, None),
    )


def _ownership(snapshot: StateSnapshot, node: str, interface: str) -> Mapping[str, object]:
    value = snapshot.interfaces[f"{node}:{interface}"]
    return cast(Mapping[str, object], value)


def _state_link_view(snapshot: StateSnapshot, link: LinkPlan) -> LinkView:
    identities = tuple(
        cast(str | None, _ownership(snapshot, endpoint.node, endpoint.interface).get("link_id"))
        for endpoint in (link.left, link.right)
    )
    return LinkView(
        index=link.index,
        kind=link.kind,
        mtu=link.mtu,
        endpoints=_endpoint_labels(link),
        endpoint_kinds=(link.kind, link.kind),
        endpoint_mtus=(link.mtu, link.mtu),
        present=True,
        link_ids=identities,
    )


def _observed_endpoint(
    inventory: LiveInventory,
    namespace: str,
    interface: str,
) -> InterfaceInventory | None:
    observed_namespace = inventory.namespaces.get(namespace)
    if observed_namespace is None or not observed_namespace.exists:
        return None
    return observed_namespace.interfaces.get(interface)


def _observed_link_groups(
    inventory: LiveInventory,
) -> dict[str, set[tuple[str, str]]]:
    groups: dict[str, set[tuple[str, str]]] = {}
    for namespace, observed_namespace in inventory.namespaces.items():
        if not observed_namespace.exists:
            continue
        for name, interface in observed_namespace.interfaces.items():
            if interface.kind != "veth" or not interface.link_id:
                continue
            groups.setdefault(interface.link_id, set()).add((namespace, name))
    return groups


def _uniform_kind(values: tuple[str | None, ...]) -> str | None:
    if values and all(value == values[0] for value in values):
        return values[0]
    return None


def _uniform_mtu(values: tuple[int | None, ...]) -> int | None:
    if values and all(value == values[0] for value in values):
        return values[0]
    return None


def _actual_link_view(
    inventory: LiveInventory,
    link: LinkPlan,
    groups: Mapping[str, set[tuple[str, str]]],
) -> LinkView:
    endpoints = (link.left, link.right)
    observed = tuple(
        _observed_endpoint(inventory, endpoint.namespace, endpoint.interface)
        for endpoint in endpoints
    )
    identities = tuple(None if interface is None else interface.link_id for interface in observed)
    endpoint_kinds = tuple(None if interface is None else interface.kind for interface in observed)
    endpoint_mtus = tuple(None if interface is None else interface.mtu for interface in observed)
    expected_group = {(endpoint.namespace, endpoint.interface) for endpoint in endpoints}
    link_id = identities[0]
    present = (
        all(interface is not None and interface.kind == "veth" for interface in observed)
        and isinstance(link_id, str)
        and bool(link_id)
        and identities[1] == link_id
        and groups.get(link_id) == expected_group
    )
    return LinkView(
        index=link.index,
        kind=_uniform_kind(endpoint_kinds),
        mtu=_uniform_mtu(endpoint_mtus),
        endpoints=_endpoint_labels(link),
        endpoint_kinds=endpoint_kinds,
        endpoint_mtus=endpoint_mtus,
        present=present,
        link_ids=identities,
    )


def _root_temporary_view(
    name: str,
    interface: InterfaceInventory | None,
) -> RootTemporaryView:
    if interface is None:
        return RootTemporaryView(name=name, present=False)
    return RootTemporaryView(
        name=name,
        present=True,
        kind=interface.kind,
        master=interface.master,
        mtu=interface.mtu,
        up=interface.up,
        addresses=_address_strings(interface.addresses),
        ifindex=interface.ifindex,
        link_id=interface.link_id,
    )


def _node_temporaries(plan: TopologyPlan, node: str) -> tuple[str, ...]:
    return tuple(
        endpoint.temporary_name
        for link in plan.links
        for endpoint in (link.left, link.right)
        if endpoint.node == node
    )


def _desired_node_view(node: NodePlan, plan: TopologyPlan) -> NodeResourceView:
    return NodeResourceView(
        name=node.name,
        kind=node.kind,
        namespace=node.namespace,
        present=True,
        interfaces=_desired_interfaces(node, plan),
        routes=tuple(_route_view(route) for route in expected_main_table_ipv4_routes(node)),
        sysctls=tuple(
            SysctlView(name=name, value=value) for name, value in sorted(node.sysctls.items())
        ),
        links=tuple(_desired_link_view(link) for link in _planned_links_for_node(plan, node.name)),
        root_temporaries=tuple(
            _root_temporary_view(name, None) for name in _node_temporaries(plan, node.name)
        ),
    )


def _state_node_view(
    node: NodePlan,
    plan: TopologyPlan,
    snapshot: StateSnapshot,
) -> NodeResourceView:
    state_interfaces: list[InterfaceView] = []
    for desired in _desired_interfaces(node, plan):
        if desired.name == "lo":
            state_interfaces.append(desired)
            continue
        ownership = _ownership(snapshot, node.name, desired.name)
        state_interfaces.append(
            replace(
                desired,
                ifindex=cast(int | None, ownership.get("ifindex")),
                link_id=cast(str | None, ownership.get("link_id")),
            )
        )
    return NodeResourceView(
        name=node.name,
        kind=node.kind,
        namespace=node.namespace,
        present=True,
        interfaces=tuple(state_interfaces),
        routes=tuple(_route_view(route) for route in expected_main_table_ipv4_routes(node)),
        sysctls=tuple(
            SysctlView(name=name, value=value) for name, value in sorted(node.sysctls.items())
        ),
        links=tuple(
            _state_link_view(snapshot, link) for link in _planned_links_for_node(plan, node.name)
        ),
        root_temporaries=tuple(
            _root_temporary_view(name, None) for name in _node_temporaries(plan, node.name)
        ),
    )


def _actual_only_link_views(
    node: NodePlan,
    plan: TopologyPlan,
    inventory: LiveInventory,
    groups: Mapping[str, set[tuple[str, str]]],
) -> tuple[LinkView, ...]:
    planned_groups = {
        frozenset(
            {
                (link.left.namespace, link.left.interface),
                (link.right.namespace, link.right.interface),
            }
        )
        for link in plan.links
    }
    namespace_nodes = {planned.namespace: planned.name for planned in plan.nodes.values()}
    node_order = {name: index for index, name in enumerate(plan.nodes)}
    result: list[LinkView] = []
    for link_id, group in sorted(groups.items()):
        if frozenset(group) in planned_groups:
            continue
        entries = tuple(
            sorted(
                group,
                key=lambda entry: (
                    node_order.get(namespace_nodes.get(entry[0], entry[0]), len(node_order)),
                    f"{namespace_nodes.get(entry[0], entry[0])}:{entry[1]}",
                ),
            )
        )
        labels = tuple(
            f"{namespace_nodes.get(namespace, namespace)}:{interface}"
            for namespace, interface in entries
        )
        if not labels or node.name not in {label.partition(":")[0] for label in labels}:
            continue
        observed = tuple(
            inventory.namespaces[namespace].interfaces[interface]
            for namespace, interface in entries
        )
        endpoint_kinds = tuple(interface.kind for interface in observed)
        endpoint_mtus = tuple(interface.mtu for interface in observed)
        result.append(
            LinkView(
                index=None,
                kind=_uniform_kind(endpoint_kinds),
                mtu=_uniform_mtu(endpoint_mtus),
                endpoints=labels,
                endpoint_kinds=endpoint_kinds,
                endpoint_mtus=endpoint_mtus,
                present=len(labels) == 2,
                link_ids=tuple(link_id for _ in labels),
            )
        )
    return tuple(result)


def _actual_node_view(
    node: NodePlan,
    plan: TopologyPlan,
    inventory: LiveInventory,
    groups: Mapping[str, set[tuple[str, str]]],
) -> NodeResourceView:
    observed_namespace = inventory.namespaces.get(node.namespace)
    present = observed_namespace is not None and observed_namespace.exists
    expected_interfaces = _desired_interfaces(node, plan)
    actual_interfaces: list[InterfaceView] = []
    actual_routes: tuple[RoutePlan, ...] = ()
    actual_sysctls: Mapping[str, int] = {}
    if observed_namespace is not None:
        expected_names = {interface.name for interface in expected_interfaces}
        for desired in expected_interfaces:
            observed = observed_namespace.interfaces.get(desired.name)
            if observed is not None:
                actual_interfaces.append(_interface_view(observed))
        actual_interfaces.extend(
            _interface_view(observed_namespace.interfaces[name])
            for name in sorted(set(observed_namespace.interfaces) - expected_names)
        )
        actual_routes = _ordered_actual_routes(
            expected_main_table_ipv4_routes(node),
            observed_namespace.routes,
        )
        actual_sysctls = observed_namespace.sysctls

    planned_links = tuple(
        _actual_link_view(inventory, link, groups)
        for link in _planned_links_for_node(plan, node.name)
    )
    return NodeResourceView(
        name=None if observed_namespace is None else observed_namespace.node,
        kind=None if observed_namespace is None else observed_namespace.kind,
        namespace=None if observed_namespace is None else observed_namespace.namespace,
        present=present,
        interfaces=tuple(actual_interfaces),
        routes=tuple(_route_view(route) for route in actual_routes),
        sysctls=tuple(
            SysctlView(name=name, value=actual_sysctls.get(name)) for name in sorted(node.sysctls)
        ),
        links=(
            *planned_links,
            *_actual_only_link_views(node, plan, inventory, groups),
        ),
        root_temporaries=tuple(
            _root_temporary_view(name, inventory.root_interfaces.get(name))
            for name in _node_temporaries(plan, node.name)
        ),
    )


def _difference(
    *,
    scope: DifferenceScope,
    source: DifferenceSource,
    property: str,
    desired: InspectionValue,
    actual: InspectionValue,
    node: str | None = None,
    interface: str | None = None,
) -> InspectionDifference:
    return InspectionDifference(
        scope=scope,
        source=source,
        property=property,
        desired=desired,
        actual=actual,
        node=node,
        interface=interface,
    )


def _compare_interface(
    node: NodePlan,
    desired: InterfaceView,
    actual: InterfaceInventory,
) -> list[InspectionDifference]:
    differences: list[InspectionDifference] = []

    def compare(property: str, expected: InspectionValue, observed: InspectionValue) -> None:
        if expected != observed:
            differences.append(
                _difference(
                    scope="interface",
                    source="live",
                    node=node.name,
                    interface=desired.name,
                    property=property,
                    desired=expected,
                    actual=observed,
                )
            )

    compare("name", desired.name, actual.name)
    compare("kind", desired.kind, actual.kind)
    compare("master", desired.master, actual.master)
    if desired.mtu is not None:
        compare("mtu", desired.mtu, actual.mtu)
    compare("up", desired.up, actual.up)
    compare("addresses", desired.addresses, _address_strings(actual.addresses))
    compare("stp", desired.stp, actual.stp)
    compare("vlan_filtering", desired.vlan_filtering, actual.vlan_filtering)
    return differences


def _link_signature(
    inventory: LiveInventory,
    link: LinkPlan,
) -> tuple[str, str]:
    values: list[str] = []
    for endpoint in (link.left, link.right):
        observed = _observed_endpoint(inventory, endpoint.namespace, endpoint.interface)
        link_id = None if observed is None else observed.link_id
        values.append(f"{endpoint.node}:{endpoint.interface}#{link_id or '<missing>'}")
    return cast(tuple[str, str], tuple(values))


def _live_differences(
    plan: TopologyPlan,
    inventory: LiveInventory,
    groups: Mapping[str, set[tuple[str, str]]],
) -> tuple[list[InspectionDifference], set[str]]:
    differences: list[InspectionDifference] = []
    impacted_nodes: set[str] = set()
    expected_namespaces = {node.namespace for node in plan.nodes.values()}
    if set(inventory.namespaces) != expected_namespaces:
        differences.append(
            _difference(
                scope="deployment",
                source="live",
                property="inventory.namespaces",
                desired=tuple(sorted(expected_namespaces)),
                actual=tuple(sorted(inventory.namespaces)),
            )
        )

    for node in plan.nodes.values():
        observed = inventory.namespaces.get(node.namespace)
        if observed is None:
            differences.append(
                _difference(
                    scope="node",
                    source="live",
                    node=node.name,
                    property="exists",
                    desired=True,
                    actual=False,
                )
            )
            impacted_nodes.add(node.name)
            continue

        for property, desired, actual in (
            ("name", node.name, observed.node),
            ("kind", node.kind, observed.kind),
            ("namespace", node.namespace, observed.namespace),
        ):
            if desired != actual:
                differences.append(
                    _difference(
                        scope="node",
                        source="live",
                        node=node.name,
                        property=property,
                        desired=desired,
                        actual=actual,
                    )
                )
                impacted_nodes.add(node.name)
        if not observed.exists:
            differences.append(
                _difference(
                    scope="node",
                    source="live",
                    node=node.name,
                    property="exists",
                    desired=True,
                    actual=False,
                )
            )
            impacted_nodes.add(node.name)
            continue

        desired_interfaces = {
            interface.name: interface for interface in _desired_interfaces(node, plan)
        }
        missing = set(desired_interfaces) - set(observed.interfaces)
        unexpected = set(observed.interfaces) - set(desired_interfaces)
        for name in sorted(missing):
            differences.append(
                _difference(
                    scope="interface",
                    source="live",
                    node=node.name,
                    interface=name,
                    property="present",
                    desired=True,
                    actual=False,
                )
            )
            impacted_nodes.add(node.name)
        for name in sorted(unexpected):
            differences.append(
                _difference(
                    scope="interface",
                    source="live",
                    node=node.name,
                    interface=name,
                    property="present",
                    desired=False,
                    actual=True,
                )
            )
            impacted_nodes.add(node.name)
        for name in desired_interfaces.keys() & observed.interfaces.keys():
            interface_differences = _compare_interface(
                node,
                desired_interfaces[name],
                observed.interfaces[name],
            )
            if interface_differences:
                differences.extend(interface_differences)
                impacted_nodes.add(node.name)

        desired_routes = expected_main_table_ipv4_routes(node)
        if frozenset(desired_routes) != frozenset(observed.routes):
            differences.append(
                _difference(
                    scope="node",
                    source="live",
                    node=node.name,
                    property="routes",
                    desired=_route_strings(desired_routes),
                    actual=_route_strings(observed.routes),
                )
            )
            impacted_nodes.add(node.name)
        for name, value in node.sysctls.items():
            actual_sysctl = observed.sysctls.get(name)
            if actual_sysctl != value:
                differences.append(
                    _difference(
                        scope="node",
                        source="live",
                        node=node.name,
                        property=f"sysctl.{name}",
                        desired=value,
                        actual=actual_sysctl,
                    )
                )
                impacted_nodes.add(node.name)

    for link in plan.links:
        actual_view = _actual_link_view(inventory, link, groups)
        if actual_view.present:
            continue
        differences.append(
            _difference(
                scope="link",
                source="live",
                node=link.left.node,
                interface=link.left.interface,
                property="link",
                desired=_endpoint_labels(link),
                actual=_link_signature(inventory, link),
            )
        )
        impacted_nodes.update((link.left.node, link.right.node))

    temporary_owners = {
        endpoint.temporary_name: endpoint.node
        for link in plan.links
        for endpoint in (link.left, link.right)
    }
    for name in sorted(inventory.root_interfaces):
        owner = temporary_owners.get(name)
        differences.append(
            _difference(
                scope="interface",
                source="live",
                node=owner,
                interface=name,
                property="root_temporary.present",
                desired=False,
                actual=True,
            )
        )
        if owner is not None:
            impacted_nodes.add(owner)

    return differences, impacted_nodes


def _collapse_ids(values: tuple[str | None, str | None]) -> InspectionValue:
    if values[0] == values[1]:
        return values[0]
    return values


def _state_differences(
    plan: TopologyPlan,
    validated: SnapshotValidation | None,
    inventory: LiveInventory,
    live_absent: bool,
) -> tuple[list[InspectionDifference], set[str]]:
    if validated is None:
        if live_absent:
            return [], set()
        return [
            _difference(
                scope="deployment",
                source="state",
                property="snapshot.present",
                desired=True,
                actual=False,
            )
        ], set()

    snapshot = validated.snapshot
    differences: list[InspectionDifference] = []
    impacted_nodes: set[str] = set()
    if snapshot.status != "deployed":
        differences.append(
            _difference(
                scope="deployment",
                source="state",
                property="snapshot.status",
                desired="deployed",
                actual=snapshot.status,
            )
        )
        return differences, impacted_nodes

    for link in plan.links:
        recorded_values = cast(
            tuple[str | None, str | None],
            tuple(
                cast(
                    str | None,
                    _ownership(snapshot, endpoint.node, endpoint.interface).get("link_id"),
                )
                for endpoint in (link.left, link.right)
            ),
        )
        observed_values = cast(
            tuple[str | None, str | None],
            tuple(
                (
                    None
                    if (
                        observed := _observed_endpoint(
                            inventory,
                            endpoint.namespace,
                            endpoint.interface,
                        )
                    )
                    is None
                    else observed.link_id
                )
                for endpoint in (link.left, link.right)
            ),
        )
        if None in recorded_values:
            desired = _collapse_ids(observed_values)
            if desired is None:
                desired = "<non-empty>"
            differences.append(
                _difference(
                    scope="link",
                    source="state",
                    node=link.left.node,
                    interface=link.left.interface,
                    property="link_id",
                    desired=desired,
                    actual=_collapse_ids(recorded_values),
                )
            )
            impacted_nodes.update((link.left.node, link.right.node))
            continue
        if recorded_values != observed_values:
            differences.append(
                _difference(
                    scope="link",
                    source="live",
                    node=link.left.node,
                    interface=link.left.interface,
                    property="link_id",
                    desired=_collapse_ids(recorded_values),
                    actual=_collapse_ids(observed_values),
                )
            )
            impacted_nodes.update((link.left.node, link.right.node))
    return differences, impacted_nodes


def _inventory_is_absent(plan: TopologyPlan, inventory: LiveInventory) -> bool:
    if inventory.root_interfaces:
        return False
    expected_namespaces = {node.namespace for node in plan.nodes.values()}
    if set(inventory.namespaces) != expected_namespaces:
        return False
    for node in plan.nodes.values():
        observed = inventory.namespaces[node.namespace]
        if (
            observed.node != node.name
            or observed.kind != node.kind
            or observed.namespace != node.namespace
            or observed.exists
            or observed.interfaces
            or observed.routes
            or observed.sysctls
        ):
            return False
    return True


def _difference_key(
    difference: InspectionDifference,
) -> tuple[str, str, str, DifferenceSource, DifferenceScope]:
    return (
        difference.node or "",
        difference.interface or "",
        difference.property,
        difference.source,
        difference.scope,
    )


def inspect_topology(
    plan: TopologyPlan,
    snapshot: StateSnapshot | None,
    inventory: LiveInventory,
) -> InspectionReport:
    """Compare desired, persisted, and live topology state without mutation."""

    validated = None if snapshot is None else _validate_snapshot(snapshot, plan)
    live_absent = _inventory_is_absent(plan, inventory)
    live_matches = inventory_matches_plan(plan, inventory)
    recorded_matches = (
        validated is not None
        and validated.link_ids_complete
        and recorded_link_ids_match_inventory(
            plan,
            inventory,
            validated.snapshot.interfaces,
        )
    )

    if live_absent:
        status: InspectionStatus = "absent" if validated is None else "stale"
    elif (
        validated is not None
        and validated.snapshot.status == "deployed"
        and live_matches
        and recorded_matches
    ):
        status = "deployed"
    else:
        status = "degraded"

    groups = _observed_link_groups(inventory)
    live_differences, impacted_nodes = _live_differences(plan, inventory, groups)
    state_differences, state_impacted_nodes = _state_differences(
        plan,
        validated,
        inventory,
        live_absent,
    )
    impacted_nodes.update(state_impacted_nodes)
    differences = [] if status == "absent" else [*live_differences, *state_differences]
    if status in {"degraded", "stale"} and not differences:
        differences.append(
            _difference(
                scope="deployment",
                source="live",
                property="inventory.match",
                desired=True,
                actual=False,
            )
        )

    summaries: list[NodeSummary] = []
    for node in plan.nodes.values():
        actual = _actual_node_view(node, plan, inventory, groups)
        if not actual.present:
            node_status: NodeStatus = "absent"
        elif node.name in impacted_nodes:
            node_status = "degraded"
        else:
            node_status = "matching"
        summaries.append(
            NodeSummary(
                name=node.name,
                kind=node.kind,
                namespace=node.namespace,
                status=node_status,
                desired=_desired_node_view(node, plan),
                state=(
                    None if validated is None else _state_node_view(node, plan, validated.snapshot)
                ),
                actual=actual,
            )
        )

    return InspectionReport(
        status=status,
        nodes=tuple(summaries),
        differences=tuple(sorted(differences, key=_difference_key)),
    )
