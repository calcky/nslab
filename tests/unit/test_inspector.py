from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from ipaddress import IPv4Interface

import pytest

from nslab.backend.base import InterfaceInventory, LiveInventory
from nslab.backend.fake import FakeNetworkBackend
from nslab.errors import NslabError
from nslab.inspector import InspectionReport, inspect_topology
from nslab.manifest import Manifest, normalized_manifest
from nslab.planner import NetemPlan, TopologyPlan, compile_plan
from nslab.state import StateSnapshot


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "version": 1,
            "name": "inspect-lab",
            "topology": {
                "nodes": {
                    "h1": {
                        "kind": "linux",
                        "interfaces": {"eth0": {"addresses": ["10.10.0.1/24"]}},
                        "routes": [
                            {
                                "dst": "default",
                                "via": "10.10.0.254",
                                "dev": "eth0",
                            }
                        ],
                        "sysctls": {"net.ipv4.ip_forward": 1},
                    },
                    "sw1": {
                        "kind": "bridge",
                        "bridge": {
                            "name": "br0",
                            "stp": True,
                            "vlan_filtering": False,
                            "priority": 4096,
                            "ports": {
                                "swp1": {"path_cost": 10, "priority": 16},
                                "swp2": {"path_cost": 100},
                            },
                        },
                        "interfaces": {"br0": {"addresses": ["192.0.2.1/24"]}},
                    },
                    "h2": {
                        "kind": "linux",
                        "interfaces": {"eth0": {"addresses": ["10.10.0.2/24"]}},
                    },
                },
                "links": [
                    {"endpoints": ["h1:eth0", "sw1:swp1"], "mtu": 1500},
                    {"endpoints": ["h2:eth0", "sw1:swp2"], "mtu": 1400},
                ],
            },
        }
    )


@pytest.fixture
def plan(manifest: Manifest) -> TopologyPlan:
    return compile_plan(manifest)


@pytest.fixture
def deployed_inventory(plan: TopologyPlan) -> LiveInventory:
    backend = FakeNetworkBackend()
    _create_topology(backend, plan)
    return backend.inventory(plan)


def _create_topology(backend: FakeNetworkBackend, plan: TopologyPlan) -> None:
    for node in plan.nodes.values():
        backend.create_namespace(node)
    for node in plan.nodes.values():
        if node.kind == "bridge":
            backend.create_bridge(node)
    for link in plan.links:
        backend.create_veth(link)
    for node in plan.nodes.values():
        backend.configure_node(node, plan)


def _absent_inventory(plan: TopologyPlan) -> LiveInventory:
    return FakeNetworkBackend().inventory(plan)


def _snapshot(
    plan: TopologyPlan,
    manifest: Manifest,
    inventory: LiveInventory,
    *,
    status: str = "deployed",
    include_link_ids: bool = True,
) -> StateSnapshot:
    interfaces: dict[str, object] = {}
    for node in plan.nodes.values():
        if node.kind == "bridge":
            assert node.bridge_name is not None
            observed = inventory.namespaces[node.namespace].interfaces.get(node.bridge_name)
            interfaces[f"{node.name}:{node.bridge_name}"] = {
                "name": node.bridge_name,
                "kind": "bridge",
                "namespace": node.namespace,
                "ifindex": None if observed is None else observed.ifindex,
            }
    for link in plan.links:
        for endpoint in (link.left, link.right):
            observed = inventory.namespaces[endpoint.namespace].interfaces.get(endpoint.interface)
            record: dict[str, object] = {
                "name": endpoint.interface,
                "kind": "veth",
                "namespace": endpoint.namespace,
                "ifindex": None if observed is None else observed.ifindex,
                "temporary_name": endpoint.temporary_name,
            }
            if include_link_ids:
                record["link_id"] = (
                    None if status != "deployed" or observed is None else observed.link_id
                )
            interfaces[f"{endpoint.node}:{endpoint.interface}"] = record
    return StateSnapshot(
        schema=1,
        name=plan.name,
        fingerprint=plan.fingerprint,
        manifest=normalized_manifest(manifest),
        namespaces={name: node.namespace for name, node in plan.nodes.items()},
        interfaces=interfaces,
        created_at="2026-09-01T10:00:00+00:00",
        status=status,  # type: ignore[arg-type]
    )


def _replace_interface(
    inventory: LiveInventory,
    namespace: str,
    interface: str,
    **changes: object,
) -> LiveInventory:
    observed_namespace = inventory.namespaces[namespace]
    interfaces = dict(observed_namespace.interfaces)
    interfaces[interface] = replace(interfaces[interface], **changes)
    namespaces = dict(inventory.namespaces)
    namespaces[namespace] = replace(observed_namespace, interfaces=interfaces)
    return LiveInventory(
        namespaces=namespaces,
        root_interfaces=inventory.root_interfaces,
    )


def _snapshot_document(snapshot: StateSnapshot) -> dict[str, object]:
    return snapshot.to_dict()


def test_absent_without_snapshot_has_no_differences_and_preserves_plan_order(
    plan: TopologyPlan,
) -> None:
    report = inspect_topology(plan, None, _absent_inventory(plan))

    assert report.status == "absent"
    assert tuple(node.name for node in report.nodes) == ("h1", "sw1", "h2")
    assert tuple(node.status for node in report.nodes) == ("absent", "absent", "absent")
    assert report.differences == ()


def test_deployed_requires_compatible_stable_snapshot_and_exact_live_match(
    plan: TopologyPlan,
    manifest: Manifest,
    deployed_inventory: LiveInventory,
) -> None:
    snapshot = _snapshot(plan, manifest, deployed_inventory)

    report = inspect_topology(plan, snapshot, deployed_inventory)

    assert report.status == "deployed"
    assert tuple(node.status for node in report.nodes) == (
        "matching",
        "matching",
        "matching",
    )
    assert report.differences == ()


def test_present_snapshot_and_proven_absence_is_stale_with_nonempty_differences(
    plan: TopologyPlan,
    manifest: Manifest,
    deployed_inventory: LiveInventory,
) -> None:
    snapshot = _snapshot(plan, manifest, deployed_inventory)

    report = inspect_topology(plan, snapshot, _absent_inventory(plan))

    assert report.status == "stale"
    assert tuple(node.status for node in report.nodes) == ("absent", "absent", "absent")
    assert report.differences
    assert {
        (difference.node, difference.interface, difference.property)
        for difference in report.differences
    } >= {("h1", None, "exists"), ("sw1", None, "exists"), ("h2", None, "exists")}


def test_live_resources_without_snapshot_are_degraded_even_when_nodes_match(
    plan: TopologyPlan,
    deployed_inventory: LiveInventory,
) -> None:
    report = inspect_topology(plan, None, deployed_inventory)

    assert report.status == "degraded"
    assert tuple(node.status for node in report.nodes) == (
        "matching",
        "matching",
        "matching",
    )
    assert [difference.to_dict() for difference in report.differences] == [
        {
            "scope": "deployment",
            "source": "state",
            "node": None,
            "interface": None,
            "property": "snapshot.present",
            "desired": True,
            "actual": False,
        }
    ]


@pytest.mark.parametrize("status", ["deploying", "destroying"])
def test_transitional_snapshot_is_degraded_without_requiring_link_ids(
    plan: TopologyPlan,
    manifest: Manifest,
    deployed_inventory: LiveInventory,
    status: str,
) -> None:
    snapshot = _snapshot(plan, manifest, deployed_inventory, status=status)

    report = inspect_topology(plan, snapshot, deployed_inventory)

    assert report.status == "degraded"
    assert tuple(node.status for node in report.nodes) == (
        "matching",
        "matching",
        "matching",
    )
    assert any(
        difference.property == "snapshot.status"
        and difference.desired == "deployed"
        and difference.actual == status
        for difference in report.differences
    )


def test_partial_live_absence_is_degraded_not_stale(
    plan: TopologyPlan,
    manifest: Manifest,
    deployed_inventory: LiveInventory,
) -> None:
    snapshot = _snapshot(plan, manifest, deployed_inventory)
    h2 = plan.nodes["h2"]
    namespaces = dict(deployed_inventory.namespaces)
    namespaces[h2.namespace] = replace(
        namespaces[h2.namespace],
        exists=False,
        interfaces={},
        routes=(),
        sysctls={},
    )
    partial = LiveInventory(
        namespaces=namespaces,
        root_interfaces=deployed_inventory.root_interfaces,
    )

    report = inspect_topology(plan, snapshot, partial)

    assert report.status == "degraded"
    assert tuple(node.status for node in report.nodes) == (
        "matching",
        "degraded",
        "absent",
    )


def test_report_contains_complete_json_safe_three_way_resources(
    plan: TopologyPlan,
    manifest: Manifest,
    deployed_inventory: LiveInventory,
) -> None:
    report = inspect_topology(
        plan,
        _snapshot(plan, manifest, deployed_inventory),
        deployed_inventory,
    )
    h1 = report.nodes[0]

    assert (h1.desired.name, h1.desired.kind, h1.desired.namespace) == (
        "h1",
        "linux",
        plan.nodes["h1"].namespace,
    )
    assert h1.desired.present is True
    assert h1.state is not None
    assert (h1.state.name, h1.state.kind, h1.state.namespace) == (
        "h1",
        "linux",
        plan.nodes["h1"].namespace,
    )
    assert h1.actual.present is True
    assert (h1.actual.name, h1.actual.kind, h1.actual.namespace) == (
        "h1",
        "linux",
        plan.nodes["h1"].namespace,
    )
    assert tuple(interface.name for interface in h1.desired.interfaces) == ("lo", "eth0")
    assert h1.desired.interfaces[1].addresses == ("10.10.0.1/24",)
    assert h1.actual.interfaces[1].ifindex is not None
    assert h1.actual.interfaces[1].link_id is not None
    assert h1.desired.routes[-1].dst == "0.0.0.0/0"
    assert tuple(sysctl.name for sysctl in h1.desired.sysctls) == ("net.ipv4.ip_forward",)
    assert tuple(link.index for link in h1.desired.links) == (0,)
    assert h1.desired.links[0].endpoints == ("h1:eth0", "sw1:swp1")
    assert h1.desired.links[0].kind == "veth"
    assert h1.desired.links[0].mtu == 1500
    assert h1.desired.links[0].endpoint_kinds == ("veth", "veth")
    assert h1.desired.links[0].endpoint_mtus == (1500, 1500)
    assert h1.actual.links[0].present is True
    assert h1.actual.links[0].kind == "veth"
    assert h1.actual.links[0].mtu == 1500
    assert h1.actual.links[0].endpoint_kinds == ("veth", "veth")
    assert h1.actual.links[0].endpoint_mtus == (1500, 1500)
    assert h1.actual.links[0].link_ids[0] == h1.actual.links[0].link_ids[1]
    assert tuple(item.name for item in h1.actual.root_temporaries) == (
        plan.links[0].left.temporary_name,
    )
    assert h1.actual.root_temporaries[0].present is False

    assert tuple(link.index for link in report.nodes[1].desired.links) == (0, 1)
    desired_bridge = report.nodes[1].desired.interfaces[1]
    desired_swp1 = report.nodes[1].desired.interfaces[2]
    assert desired_bridge.bridge_priority == 4096
    assert desired_swp1.path_cost == 10
    assert desired_swp1.port_priority == 16
    assert tuple(link.index for link in report.nodes[1].state.links) == (0, 1)  # type: ignore[union-attr]
    assert tuple(link.index for link in report.nodes[1].actual.links) == (0, 1)
    assert tuple(link.index for link in report.nodes[2].desired.links) == (1,)

    document = report.to_dict()
    assert tuple(document) == ("status", "nodes", "differences")
    assert document["nodes"][0]["name"] == "h1"  # type: ignore[index]
    assert "\x1b" not in json.dumps(document, sort_keys=True)
    with pytest.raises(FrozenInstanceError):
        report.status = "absent"  # type: ignore[misc]


def test_ifindex_changes_are_visible_but_never_drift(
    plan: TopologyPlan,
    manifest: Manifest,
    deployed_inventory: LiveInventory,
) -> None:
    snapshot = _snapshot(plan, manifest, deployed_inventory)
    changed = _replace_interface(
        deployed_inventory,
        plan.nodes["h1"].namespace,
        "eth0",
        ifindex=9999,
    )

    report = inspect_topology(plan, snapshot, changed)

    assert report.status == "deployed"
    assert report.differences == ()
    h1 = report.nodes[0]
    assert h1.state is not None
    assert h1.state.interfaces[1].ifindex != h1.actual.interfaces[1].ifindex


def test_interface_differences_are_complete_and_deterministically_sorted(
    plan: TopologyPlan,
    manifest: Manifest,
    deployed_inventory: LiveInventory,
) -> None:
    snapshot = _snapshot(plan, manifest, deployed_inventory)
    changed = _replace_interface(
        deployed_inventory,
        plan.nodes["h1"].namespace,
        "eth0",
        mtu=1300,
        up=False,
        addresses=(IPv4Interface("198.51.100.1/24"),),
        netem=NetemPlan(delay_ms=50, jitter_ms=0, loss_percent=0),
    )

    report = inspect_topology(plan, snapshot, changed)

    assert report.status == "degraded"
    keys = [
        (
            difference.node or "",
            difference.interface or "",
            difference.property,
            difference.source,
        )
        for difference in report.differences
    ]
    assert keys == sorted(keys)
    h1_differences = {
        difference.property: (difference.desired, difference.actual)
        for difference in report.differences
        if difference.node == "h1" and difference.interface == "eth0"
    }
    assert h1_differences == {
        "addresses": (("10.10.0.1/24",), ("198.51.100.1/24",)),
        "mtu": (1500, 1300),
        "netem": (None, "delay 50ms"),
        "up": (True, False),
    }


def test_inspection_reports_explicit_stp_tuning_drift(
    plan: TopologyPlan,
    manifest: Manifest,
    deployed_inventory: LiveInventory,
) -> None:
    snapshot = _snapshot(plan, manifest, deployed_inventory)
    sw1 = plan.nodes["sw1"]
    changed = _replace_interface(
        deployed_inventory,
        sw1.namespace,
        "br0",
        bridge_priority=8192,
    )
    changed = _replace_interface(
        changed,
        sw1.namespace,
        "swp1",
        path_cost=20,
        port_priority=32,
    )

    report = inspect_topology(plan, snapshot, changed)

    assert report.status == "degraded"
    assert {
        (difference.interface, difference.property, difference.desired, difference.actual)
        for difference in report.differences
        if difference.node == "sw1"
    } >= {
        ("br0", "bridge_priority", 4096, 8192),
        ("swp1", "path_cost", 10, 20),
        ("swp1", "port_priority", 16, 32),
    }


def test_actual_node_view_reports_observed_identity_drift(
    plan: TopologyPlan,
    manifest: Manifest,
    deployed_inventory: LiveInventory,
) -> None:
    snapshot = _snapshot(plan, manifest, deployed_inventory)
    h1 = plan.nodes["h1"]
    namespaces = dict(deployed_inventory.namespaces)
    namespaces[h1.namespace] = replace(
        namespaces[h1.namespace],
        node="wrong-node",
        kind="bridge",
        namespace="wrong-namespace",
    )
    changed = LiveInventory(
        namespaces=namespaces,
        root_interfaces=deployed_inventory.root_interfaces,
    )

    report = inspect_topology(plan, snapshot, changed)

    summary = report.nodes[0]
    assert summary.status == "degraded"
    assert (summary.desired.name, summary.desired.kind, summary.desired.namespace) == (
        h1.name,
        h1.kind,
        h1.namespace,
    )
    assert summary.state is not None
    assert (summary.state.name, summary.state.kind, summary.state.namespace) == (
        h1.name,
        h1.kind,
        h1.namespace,
    )
    assert (summary.actual.name, summary.actual.kind, summary.actual.namespace) == (
        "wrong-node",
        "bridge",
        "wrong-namespace",
    )
    assert {
        difference.property
        for difference in report.differences
        if difference.node == h1.name and difference.scope == "node"
    } >= {"name", "kind", "namespace"}


def test_actual_link_view_exposes_inconsistent_endpoint_kind_and_mtu(
    plan: TopologyPlan,
    manifest: Manifest,
    deployed_inventory: LiveInventory,
) -> None:
    snapshot = _snapshot(plan, manifest, deployed_inventory)
    link = plan.links[0]
    changed = _replace_interface(
        deployed_inventory,
        link.right.namespace,
        link.right.interface,
        kind="bridge",
        mtu=1300,
    )

    report = inspect_topology(plan, snapshot, changed)

    actual = report.nodes[0].actual.links[0]
    assert actual.kind is None
    assert actual.mtu is None
    assert actual.endpoint_kinds == ("veth", "bridge")
    assert actual.endpoint_mtus == (1500, 1300)


def test_live_link_identity_change_is_degraded_against_recorded_snapshot(
    plan: TopologyPlan,
    manifest: Manifest,
    deployed_inventory: LiveInventory,
) -> None:
    snapshot = _snapshot(plan, manifest, deployed_inventory)
    link = plan.links[0]
    changed = deployed_inventory
    for endpoint in (link.left, link.right):
        changed = _replace_interface(
            changed,
            endpoint.namespace,
            endpoint.interface,
            link_id="replacement-link-id",
        )

    report = inspect_topology(plan, snapshot, changed)

    assert report.status == "degraded"
    difference = next(
        difference
        for difference in report.differences
        if difference.scope == "link" and difference.property == "link_id"
    )
    assert difference.source == "live"
    assert difference.node == link.left.node
    assert difference.interface == link.left.interface
    assert difference.desired != difference.actual
    assert tuple(node.status for node in report.nodes) == (
        "degraded",
        "degraded",
        "matching",
    )


def test_legacy_snapshot_without_link_ids_is_degraded_not_invalid(
    plan: TopologyPlan,
    manifest: Manifest,
    deployed_inventory: LiveInventory,
) -> None:
    snapshot = _snapshot(
        plan,
        manifest,
        deployed_inventory,
        include_link_ids=False,
    )

    report = inspect_topology(plan, snapshot, deployed_inventory)

    assert report.status == "degraded"
    assert any(
        difference.scope == "link"
        and difference.source == "state"
        and difference.property == "link_id"
        and difference.actual is None
        for difference in report.differences
    )
    assert tuple(node.status for node in report.nodes) == (
        "degraded",
        "degraded",
        "degraded",
    )


def test_deployed_snapshot_cannot_mix_complete_and_legacy_link_identity(
    plan: TopologyPlan,
    manifest: Manifest,
    deployed_inventory: LiveInventory,
) -> None:
    snapshot = _snapshot(plan, manifest, deployed_inventory)
    document = _snapshot_document(snapshot)
    interfaces = document["interfaces"]
    assert isinstance(interfaces, dict)
    second = plan.links[1]
    for endpoint in (second.left, second.right):
        record = interfaces[f"{endpoint.node}:{endpoint.interface}"]
        assert isinstance(record, dict)
        record.pop("link_id")

    with pytest.raises(NslabError) as caught:
        inspect_topology(plan, StateSnapshot.from_dict(document), deployed_inventory)

    assert caught.value.code == "STATE_INVALID"


@pytest.mark.parametrize(
    "invalidity",
    ["empty", "partial_missing", "mismatch", "reused"],
)
def test_explicit_invalid_deployed_snapshot_link_ids_are_state_invalid(
    plan: TopologyPlan,
    manifest: Manifest,
    deployed_inventory: LiveInventory,
    invalidity: str,
) -> None:
    snapshot = _snapshot(plan, manifest, deployed_inventory)
    document = _snapshot_document(snapshot)
    interfaces = document["interfaces"]
    assert isinstance(interfaces, dict)
    first, second = plan.links
    first_left = interfaces[f"{first.left.node}:{first.left.interface}"]
    first_right = interfaces[f"{first.right.node}:{first.right.interface}"]
    second_left = interfaces[f"{second.left.node}:{second.left.interface}"]
    second_right = interfaces[f"{second.right.node}:{second.right.interface}"]
    assert all(
        isinstance(value, dict) for value in (first_left, first_right, second_left, second_right)
    )
    if invalidity == "empty":
        first_left["link_id"] = ""  # type: ignore[index]
    elif invalidity == "partial_missing":
        first_left.pop("link_id")  # type: ignore[union-attr]
    elif invalidity == "mismatch":
        first_right["link_id"] = "different"  # type: ignore[index]
    else:
        first_id = first_left["link_id"]  # type: ignore[index]
        second_left["link_id"] = first_id  # type: ignore[index]
        second_right["link_id"] = first_id  # type: ignore[index]
    invalid = StateSnapshot.from_dict(document)

    with pytest.raises(NslabError) as caught:
        inspect_topology(plan, invalid, deployed_inventory)

    assert caught.value.code == "STATE_INVALID"


@pytest.mark.parametrize(
    "invalidity",
    ["partial_missing", "mixed_value", "pair_missing", "complete_pair"],
)
def test_mixed_transitional_snapshot_link_id_states_are_invalid(
    plan: TopologyPlan,
    manifest: Manifest,
    deployed_inventory: LiveInventory,
    invalidity: str,
) -> None:
    snapshot = _snapshot(
        plan,
        manifest,
        deployed_inventory,
        status="deploying",
    )
    document = _snapshot_document(snapshot)
    interfaces = document["interfaces"]
    assert isinstance(interfaces, dict)
    link = plan.links[0]
    left = interfaces[f"{link.left.node}:{link.left.interface}"]
    right = interfaces[f"{link.right.node}:{link.right.interface}"]
    assert isinstance(left, dict)
    assert isinstance(right, dict)
    if invalidity == "partial_missing":
        left.pop("link_id")
    elif invalidity == "mixed_value":
        left["link_id"] = "partial-link-id"
    elif invalidity == "pair_missing":
        left.pop("link_id")
        right.pop("link_id")
    else:
        left["link_id"] = "complete-link-id"
        right["link_id"] = "complete-link-id"
    invalid = StateSnapshot.from_dict(document)

    with pytest.raises(NslabError) as caught:
        inspect_topology(plan, invalid, deployed_inventory)

    assert caught.value.code == "STATE_INVALID"


def test_self_inconsistent_snapshot_is_state_invalid(
    plan: TopologyPlan,
    manifest: Manifest,
    deployed_inventory: LiveInventory,
) -> None:
    snapshot = _snapshot(plan, manifest, deployed_inventory)
    inconsistent = replace(snapshot, fingerprint="f" * 64)

    with pytest.raises(NslabError) as caught:
        inspect_topology(plan, inconsistent, deployed_inventory)

    assert caught.value.code == "STATE_INVALID"
    assert caught.value.details["name"] == plan.name


def test_self_consistent_but_incompatible_snapshot_is_plan_state_mismatch(
    plan: TopologyPlan,
    deployed_inventory: LiveInventory,
) -> None:
    other_manifest = Manifest.model_validate(
        {
            "version": 1,
            "name": plan.name,
            "topology": {
                "nodes": {"other": {"kind": "linux"}},
                "links": [],
            },
        }
    )
    other_plan = compile_plan(other_manifest)
    other_inventory = _absent_inventory(other_plan)
    snapshot = _snapshot(other_plan, other_manifest, other_inventory)

    with pytest.raises(NslabError) as caught:
        inspect_topology(plan, snapshot, deployed_inventory)

    assert caught.value.code == "PLAN_STATE_MISMATCH"
    assert caught.value.details == {
        "name": plan.name,
        "plan_fingerprint": plan.fingerprint,
        "snapshot_fingerprint": other_plan.fingerprint,
    }


def test_exact_temporary_root_artifact_is_degraded_and_reported_on_owning_node(
    plan: TopologyPlan,
    manifest: Manifest,
    deployed_inventory: LiveInventory,
) -> None:
    snapshot = _snapshot(plan, manifest, deployed_inventory)
    endpoint = plan.links[0].left
    leftover = InterfaceInventory(
        name=endpoint.temporary_name,
        kind="veth",
        ifindex=800,
        master=None,
        mtu=plan.links[0].mtu,
        up=False,
        link_id="leftover-link-id",
    )
    inventory = replace(
        deployed_inventory,
        root_interfaces={endpoint.temporary_name: leftover},
    )

    report = inspect_topology(plan, snapshot, inventory)

    assert report.status == "degraded"
    h1 = report.nodes[0]
    assert h1.status == "degraded"
    root = h1.actual.root_temporaries[0]
    assert root.name == endpoint.temporary_name
    assert root.present is True
    assert root.ifindex == 800
    assert any(
        difference.node == endpoint.node
        and difference.interface == endpoint.temporary_name
        and difference.property == "root_temporary.present"
        for difference in report.differences
    )


def test_report_and_nested_models_are_frozen(
    plan: TopologyPlan,
) -> None:
    report: InspectionReport = inspect_topology(plan, None, _absent_inventory(plan))

    with pytest.raises(FrozenInstanceError):
        report.nodes[0].status = "matching"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        report.nodes[0].actual.present = True  # type: ignore[misc]
