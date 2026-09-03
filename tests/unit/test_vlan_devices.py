from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import replace
from ipaddress import IPv4Interface
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call

import pytest
from pydantic import ValidationError

from nslab.backend.base import expected_main_table_routes, inventory_matches_plan
from nslab.backend.fake import FakeNetworkBackend
from nslab.backend.pyroute2 import Pyroute2Backend
from nslab.graph import render_graph
from nslab.inspector import inspect_topology
from nslab.lifecycle import LifecycleService
from nslab.manifest import (
    LinuxNode,
    Manifest,
    VlanDeviceConfig,
    load_manifest,
    normalized_manifest,
)
from nslab.planner import (
    BridgeVlanPlan,
    NodePlan,
    VlanDevicePlan,
    compile_plan,
    node_interface_addresses,
)
from nslab.routing import render_frr_config
from nslab.state import StateStore

_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def _document() -> dict[str, Any]:
    return {
        "version": 1,
        "name": "vlan-device",
        "topology": {
            "nodes": {
                "h1": {
                    "kind": "linux",
                    "interfaces": {"eth0": {}},
                    "devices": {
                        "vlan10": {
                            "type": "vlan",
                            "link": "eth0",
                            "id": 10,
                            "addresses": ["192.0.2.1/24"],
                        }
                    },
                    "routes": [
                        {
                            "dst": "default",
                            "via": "192.0.2.254",
                            "dev": "vlan10",
                        }
                    ],
                },
                "h2": {
                    "kind": "linux",
                    "interfaces": {"eth0": {}},
                    "devices": {
                        "vlan10": {
                            "type": "vlan",
                            "link": "eth0",
                            "id": 10,
                            "addresses": ["192.0.2.2/24"],
                        }
                    },
                },
            },
            "links": [{"endpoints": ["h1:eth0", "h2:eth0"]}],
        },
    }


def _manifest() -> Manifest:
    return Manifest.model_validate(_document())


def test_vlan_example_manifest_combines_subinterfaces_and_router_on_a_stick() -> None:
    manifest = load_manifest(_EXAMPLES / "vlan" / "nslab.yaml")

    plan = compile_plan(manifest)

    assert plan.name == "vlan"
    assert tuple(plan.nodes) == ("h1", "h2", "h10", "sw1", "r1", "h20")
    assert len(plan.links) == 5
    assert tuple(
        (link.left.node, link.left.interface, link.right.node, link.right.interface)
        for link in plan.links
    ) == (
        ("h1", "eth0", "sw1", "trunk1"),
        ("h2", "eth0", "sw1", "trunk2"),
        ("h10", "eth0", "sw1", "access10"),
        ("sw1", "router", "r1", "eth0"),
        ("sw1", "access20", "h20", "eth0"),
    )
    assert plan.nodes["h1"].devices["vlan10"] == VlanDevicePlan(
        name="vlan10",
        link="eth0",
        vlan_id=10,
        addresses=(IPv4Interface("192.168.10.3/24"),),
    )
    assert plan.nodes["h2"].devices["vlan10"] == VlanDevicePlan(
        name="vlan10",
        link="eth0",
        vlan_id=10,
        addresses=(IPv4Interface("192.168.10.4/24"),),
    )
    assert plan.nodes["r1"].devices["vlan10"] == VlanDevicePlan(
        name="vlan10",
        link="eth0",
        vlan_id=10,
        addresses=(IPv4Interface("192.168.10.1/24"),),
    )
    assert plan.nodes["r1"].devices["vlan20"] == VlanDevicePlan(
        name="vlan20",
        link="eth0",
        vlan_id=20,
        addresses=(IPv4Interface("192.168.20.1/24"),),
    )
    assert plan.nodes["r1"].sysctls == {"net.ipv4.ip_forward": 1}
    assert plan.nodes["sw1"].vlan_filtering is True
    assert plan.nodes["sw1"].bridge_ports["trunk1"].vlans == (
        BridgeVlanPlan(vid=10, pvid=False, untagged=False),
    )
    assert plan.nodes["sw1"].bridge_ports["trunk2"].vlans == (
        BridgeVlanPlan(vid=10, pvid=False, untagged=False),
    )
    assert plan.nodes["h1"].routes[0].dev == "vlan10"
    assert str(plan.nodes["h1"].routes[0].via) == "192.168.10.1"
    assert plan.nodes["h2"].routes[0].dev == "vlan10"
    assert str(plan.nodes["h2"].routes[0].via) == "192.168.10.1"


def _link_message(
    index: int,
    name: str,
    kind: str,
    *,
    parent_index: int | None = None,
    vlan_id: int | None = None,
) -> dict[str, object]:
    link_info: list[tuple[str, object]] = [("IFLA_INFO_KIND", kind)]
    if vlan_id is not None:
        link_info.append(("IFLA_INFO_DATA", {"attrs": [("IFLA_VLAN_ID", vlan_id)]}))
    attributes: list[tuple[str, object]] = [
        ("IFLA_IFNAME", name),
        ("IFLA_MTU", 1500),
        ("IFLA_LINKINFO", {"attrs": link_info}),
    ]
    if parent_index is not None:
        attributes.append(("IFLA_LINK", parent_index))
    return {"index": index, "flags": 1, "attrs": attributes}


def test_manifest_and_plan_preserve_vlan_device_configuration() -> None:
    manifest = _manifest()
    h1 = manifest.topology.nodes["h1"]

    assert isinstance(h1, LinuxNode)
    assert isinstance(h1.devices["vlan10"], VlanDeviceConfig)
    assert h1.devices["vlan10"].id == 10
    assert h1.devices["vlan10"].addresses == (IPv4Interface("192.0.2.1/24"),)

    plan = compile_plan(manifest)
    device = plan.nodes["h1"].devices["vlan10"]
    assert isinstance(device, VlanDevicePlan)
    assert device == VlanDevicePlan(
        name="vlan10",
        link="eth0",
        vlan_id=10,
        addresses=(IPv4Interface("192.0.2.1/24"),),
    )
    assert node_interface_addresses(plan.nodes["h1"]) == {
        "eth0": (),
        "vlan10": (IPv4Interface("192.0.2.1/24"),),
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda node: node["devices"]["vlan10"].update({"id": 0}), "greater than or equal"),
        (
            lambda node: node["devices"]["vlan10"].update({"type": "vxlan"}),
            "Field required",
        ),
        (lambda node: node["devices"]["vlan10"].update({"link": "lo"}), "cannot be 'lo'"),
        (
            lambda node: node["devices"].update(
                {
                    "vlan20": {
                        "type": "vlan",
                        "link": "eth0",
                        "id": 10,
                    }
                }
            ),
            "duplicate VLAN ID",
        ),
        (
            lambda node: node["devices"].update(
                {
                    "vlan20": {
                        "type": "vlan",
                        "link": "vlan10",
                        "id": 20,
                    }
                }
            ),
            "parent must be a linked interface",
        ),
        (lambda node: node["devices"]["vlan10"].update({"link": "eth9"}), "not linked"),
        (
            lambda node: node["routes"][0].update({"dst": "192.0.2.0/24", "via": None}),
            "conflicts with connected network",
        ),
    ],
)
def test_manifest_rejects_invalid_vlan_devices(
    mutation: Callable[[dict[str, Any]], None], message: str
) -> None:
    document = copy.deepcopy(_document())
    node = document["topology"]["nodes"]["h1"]
    mutation(node)

    with pytest.raises(ValidationError, match=message):
        Manifest.model_validate(document)


def test_manifest_rejects_device_name_collisions() -> None:
    document = copy.deepcopy(_document())
    node = document["topology"]["nodes"]["h1"]
    node["devices"] = {"eth0": {"type": "vlan", "link": "eth0", "id": 10}}

    with pytest.raises(ValidationError, match="device name conflicts"):
        Manifest.model_validate(document)


def test_empty_devices_do_not_change_legacy_normalized_manifest() -> None:
    document = _document()
    nodes = document["topology"]["nodes"]
    nodes["h1"].pop("devices")
    nodes["h1"].pop("routes")
    nodes["h2"].pop("devices")

    normalized = normalized_manifest(Manifest.model_validate(document))

    normalized_nodes = normalized["topology"]["nodes"]  # type: ignore[index]
    assert "devices" not in normalized_nodes["h1"]  # type: ignore[index]
    assert "devices" not in normalized_nodes["h2"]  # type: ignore[index]


def test_fake_backend_inventory_routes_snapshot_and_drift_include_vlan_device(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    plan = compile_plan(manifest)
    backend = FakeNetworkBackend()
    service = LifecycleService(backend, StateStore(tmp_path))

    result = service.deploy(plan, manifest)

    assert result.status == "deployed"
    inventory = backend.inventory(plan)
    assert inventory_matches_plan(plan, inventory)
    observed = inventory.namespaces[plan.nodes["h1"].namespace].interfaces["vlan10"]
    assert observed.parent == "eth0"
    assert observed.vlan_id == 10
    assert observed.addresses == (IPv4Interface("192.0.2.1/24"),)
    assert _route_tuples(plan.nodes["h1"]) == (
        ("127.0.0.0/8", None, "lo"),
        ("192.0.2.0/24", None, "vlan10"),
        ("0.0.0.0/0", "192.0.2.254", "vlan10"),
    )

    snapshot = StateStore(tmp_path).load(plan.name)
    assert snapshot is not None
    assert snapshot.interfaces["h1:vlan10"] == {
        "name": "vlan10",
        "kind": "vlan",
        "namespace": plan.nodes["h1"].namespace,
        "parent": "eth0",
        "vlan_id": 10,
        "ifindex": observed.ifindex,
    }

    namespace = backend.namespaces[plan.nodes["h1"].namespace]
    namespace.interfaces["vlan10"] = replace(observed, vlan_id=20)
    drifted_inventory = backend.inventory(plan)
    assert not inventory_matches_plan(plan, drifted_inventory)
    report = inspect_topology(plan, snapshot, drifted_inventory)
    assert report.status == "degraded"
    assert any(
        difference.node == "h1"
        and difference.interface == "vlan10"
        and difference.property == "vlan_id"
        and difference.desired == 10
        and difference.actual == 20
        for difference in report.differences
    )


def _route_tuples(node: NodePlan) -> tuple[tuple[str, str | None, str], ...]:
    return tuple(
        (str(route.dst), None if route.via is None else str(route.via), route.dev)
        for route in expected_main_table_routes(node)
    )


def test_pyroute2_configure_node_creates_vlan_before_assigning_its_address() -> None:
    plan = compile_plan(_manifest())
    node = plan.nodes["h1"]
    handle = Mock()
    handle.link_lookup.side_effect = lambda *, ifname: {
        "eth0": [10],
        "vlan10": [11],
    }[ifname]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    backend.configure_node(node, plan)

    assert handle.mock_calls == [
        call.link_lookup(ifname="eth0"),
        call.link(
            "add",
            ifname="vlan10",
            kind="vlan",
            link=10,
            vlan_id=10,
        ),
        call.link_lookup(ifname="vlan10"),
        call.link("set", index=10, state="up"),
        call.addr(
            "add",
            index=11,
            address="192.0.2.1",
            prefixlen=24,
        ),
        call.link("set", index=11, state="up"),
        call.route(
            "add",
            dst="0.0.0.0/0",
            oif=11,
            gateway="192.0.2.254",
        ),
        call.close(),
    ]


def test_pyroute2_inventory_decodes_vlan_parent_and_id() -> None:
    interfaces, _ = Pyroute2Backend._inventory_interfaces(
        (
            _link_message(10, "eth0", "veth"),
            _link_message(11, "vlan10", "vlan", parent_index=10, vlan_id=10),
        ),
        (
            {
                "index": 11,
                "prefixlen": 24,
                "attrs": [("IFA_LOCAL", "192.0.2.1")],
            },
        ),
    )

    observed = interfaces["vlan10"]
    assert observed.kind == "vlan"
    assert observed.parent == "eth0"
    assert observed.vlan_id == 10
    assert observed.addresses == (IPv4Interface("192.0.2.1/24"),)


def test_graph_formats_show_vlan_device_relationship() -> None:
    plan = compile_plan(_manifest())

    tree = render_graph(plan, "tree")
    mermaid = render_graph(plan, "mermaid")
    graph_json = json.loads(render_graph(plan, "json"))

    assert "vlan10: vlan 10 on eth0 · 192.0.2.1/24" in tree
    assert "vlan10: vlan 10 on eth0" in mermaid
    assert graph_json["nodes"][0]["devices"] == [
        {
            "addresses": ["192.0.2.1/24"],
            "id": 10,
            "link": "eth0",
            "name": "vlan10",
            "type": "vlan",
        }
    ]


def test_frr_uses_vlan_device_connected_networks() -> None:
    document = copy.deepcopy(_document())
    node = document["topology"]["nodes"]["h1"]
    node["sysctls"] = {"net.ipv4.ip_forward": 1}
    node["routing"] = {"ospf": {"router_id": "1.1.1.1", "passive_interfaces": ["vlan10"]}}
    plan = compile_plan(Manifest.model_validate(document))

    config = render_frr_config(plan.nodes["h1"], plan)

    assert " network 192.0.2.0/24 area 0.0.0.0" in config
    assert " passive-interface vlan10" in config


def test_bgp_neighbor_may_be_directly_connected_through_vlan_device() -> None:
    document = copy.deepcopy(_document())
    node = document["topology"]["nodes"]["h1"]
    node["sysctls"] = {"net.ipv4.ip_forward": 1}
    node["routing"] = {
        "bgp": {
            "local_as": 65001,
            "router_id": "1.1.1.1",
            "neighbors": [{"address": "192.0.2.2", "remote_as": 65002}],
        }
    }

    manifest = Manifest.model_validate(document)

    assert isinstance(manifest.topology.nodes["h1"], LinuxNode)
