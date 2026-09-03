from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import replace
from ipaddress import IPv4Network
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call

import pytest
from pydantic import ValidationError

from nslab.backend.base import expected_routes, inventory_matches_plan
from nslab.backend.fake import FakeNetworkBackend
from nslab.backend.pyroute2 import Pyroute2Backend
from nslab.graph import render_graph
from nslab.inspector import inspect_topology
from nslab.lifecycle import LifecycleService
from nslab.manifest import LinuxNode, Manifest, VrfDeviceConfig, load_manifest
from nslab.planner import (
    VlanDevicePlan,
    VrfDevicePlan,
    compile_plan,
    node_interface_master,
    node_interface_route_table,
    node_route_tables,
)
from nslab.state import StateStore

_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def _document() -> dict[str, Any]:
    host = {
        "kind": "linux",
        "interfaces": {
            "eth0": {
                "addresses": ["10.0.0.2/24", "192.0.2.2/32"],
            }
        },
    }
    return {
        "version": 1,
        "name": "vrf",
        "topology": {
            "nodes": {
                "h1": copy.deepcopy(host),
                "r1": {
                    "kind": "linux",
                    "interfaces": {
                        "blue0": {"addresses": ["10.0.0.1/24"]},
                        "red0": {"addresses": ["10.0.0.1/24"]},
                    },
                    "devices": {
                        "blue": {
                            "type": "vrf",
                            "table": 1001,
                            "interfaces": ["blue0"],
                        },
                        "red": {
                            "type": "vrf",
                            "table": 1002,
                            "interfaces": ["red0"],
                        },
                    },
                    "routes": [
                        {
                            "dst": "192.0.2.2/32",
                            "via": "10.0.0.2",
                            "dev": "blue0",
                        },
                        {
                            "dst": "192.0.2.2/32",
                            "via": "10.0.0.2",
                            "dev": "red0",
                        },
                    ],
                },
                "h2": copy.deepcopy(host),
            },
            "links": [
                {"endpoints": ["h1:eth0", "r1:blue0"]},
                {"endpoints": ["r1:red0", "h2:eth0"]},
            ],
        },
    }


def _manifest() -> Manifest:
    return Manifest.model_validate(_document())


def test_vrf_example_manifest_compiles() -> None:
    plan = compile_plan(load_manifest(_EXAMPLES / "vrf" / "nslab.yaml"))

    assert plan.name == "vrf"
    assert isinstance(plan.nodes["r1"].devices["blue"], VrfDevicePlan)


def _r1(document: dict[str, Any]) -> dict[str, Any]:
    return document["topology"]["nodes"]["r1"]


def _link_message(
    index: int,
    name: str,
    kind: str,
    *,
    master: int | None = None,
    vrf_table: int | None = None,
) -> dict[str, object]:
    link_info: list[tuple[str, object]] = [("IFLA_INFO_KIND", kind)]
    if vrf_table is not None:
        link_info.append(("IFLA_INFO_DATA", {"attrs": [("IFLA_VRF_TABLE", vrf_table)]}))
    attributes: list[tuple[str, object]] = [
        ("IFLA_IFNAME", name),
        ("IFLA_MTU", 1500),
        ("IFLA_LINKINFO", {"attrs": link_info}),
    ]
    if master is not None:
        attributes.append(("IFLA_MASTER", master))
    return {"index": index, "flags": 1, "attrs": attributes}


def test_manifest_and_plan_preserve_vrf_tables_members_and_overlapping_routes() -> None:
    manifest = _manifest()
    node = manifest.topology.nodes["r1"]

    assert isinstance(node, LinuxNode)
    assert isinstance(node.devices["blue"], VrfDeviceConfig)
    assert node.devices["blue"].table == 1001
    assert node.devices["blue"].interfaces == ("blue0",)

    plan = compile_plan(manifest)
    r1 = plan.nodes["r1"]
    assert r1.devices == {
        "blue": VrfDevicePlan("blue", 1001, ("blue0",)),
        "red": VrfDevicePlan("red", 1002, ("red0",)),
    }
    assert node_interface_master(r1, "blue0") == "blue"
    assert node_interface_master(r1, "red0") == "red"
    assert node_interface_route_table(r1, "blue0") == 1001
    assert node_interface_route_table(r1, "red0") == 1002
    assert node_route_tables(r1) == (254, 1001, 1002)
    assert tuple((str(route.dst), route.dev, route.table) for route in r1.routes) == (
        ("192.0.2.2/32", "blue0", 1001),
        ("192.0.2.2/32", "red0", 1002),
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda node: node["devices"]["blue"].update(table=0), "greater than or equal"),
        (lambda node: node["devices"]["blue"].update(table=254), "reserved table"),
        (lambda node: node["devices"]["red"].update(table=1001), "duplicate VRF table"),
        (lambda node: node["devices"]["blue"].update(interfaces=[]), "at least 1 item"),
        (
            lambda node: node["devices"]["blue"].update(interfaces=["blue0", "blue0"]),
            "must be unique",
        ),
        (
            lambda node: node["devices"]["red"].update(interfaces=["blue0"]),
            "more than one VRF",
        ),
        (
            lambda node: node["devices"]["blue"].update(interfaces=["blue"]),
            "must be a linked interface or VLAN device",
        ),
        (
            lambda node: node["devices"]["blue"].update(interfaces=["missing0"]),
            "not linked or a VLAN device",
        ),
        (
            lambda node: node["routes"][1].update(dev="blue0"),
            "duplicate route destination",
        ),
        (
            lambda node: node.update(
                sysctls={"net.ipv4.ip_forward": 1},
                routing={"ospf": {"router_id": "1.1.1.1"}},
            ),
            "dynamic routing with VRF devices is not supported",
        ),
    ],
)
def test_manifest_rejects_invalid_vrf_configuration(
    mutation: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    document = _document()
    mutation(_r1(document))

    with pytest.raises(ValidationError, match=message):
        Manifest.model_validate(document)


def test_vrf_may_own_a_vlan_device() -> None:
    document = _document()
    node = _r1(document)
    node["devices"]["blue"]["interfaces"] = ["vlan10"]
    node["devices"]["vlan10"] = {
        "type": "vlan",
        "link": "blue0",
        "id": 10,
        "addresses": ["198.51.100.1/24"],
    }
    node["routes"][0]["dev"] = "vlan10"

    plan = compile_plan(Manifest.model_validate(document))
    r1 = plan.nodes["r1"]

    assert isinstance(r1.devices["vlan10"], VlanDevicePlan)
    assert node_interface_master(r1, "vlan10") == "blue"
    assert r1.routes[0].table == 1001


def test_fake_backend_snapshot_inventory_and_drift_include_vrf_state(tmp_path: Path) -> None:
    manifest = _manifest()
    plan = compile_plan(manifest)
    backend = FakeNetworkBackend()
    store = StateStore(tmp_path)
    service = LifecycleService(backend, store)

    result = service.deploy(plan, manifest)

    assert result.status == "deployed"
    inventory = backend.inventory(plan)
    assert inventory_matches_plan(plan, inventory)
    namespace = inventory.namespaces[plan.nodes["r1"].namespace]
    assert namespace.interfaces["blue"].vrf_table == 1001
    assert namespace.interfaces["red"].vrf_table == 1002
    assert namespace.interfaces["blue0"].master == "blue"
    assert namespace.interfaces["red0"].master == "red"
    assert tuple(
        (str(route.dst), route.dev, route.table) for route in expected_routes(plan.nodes["r1"])
    ) == (
        ("127.0.0.0/8", "lo", 254),
        ("10.0.0.0/24", "blue0", 1001),
        ("10.0.0.0/24", "red0", 1002),
        ("192.0.2.2/32", "blue0", 1001),
        ("192.0.2.2/32", "red0", 1002),
    )

    snapshot = store.load(plan.name)
    assert snapshot is not None
    assert snapshot.interfaces["r1:blue"] == {
        "name": "blue",
        "kind": "vrf",
        "namespace": plan.nodes["r1"].namespace,
        "vrf_table": 1001,
        "ifindex": namespace.interfaces["blue"].ifindex,
    }

    state = backend.namespaces[plan.nodes["r1"].namespace]
    blue = state.interfaces["blue"]
    state.interfaces["blue"] = replace(blue, vrf_table=2001)
    drifted = backend.inventory(plan)
    assert not inventory_matches_plan(plan, drifted)
    report = inspect_topology(plan, snapshot, drifted)
    assert report.status == "degraded"
    assert any(
        difference.node == "r1"
        and difference.interface == "blue"
        and difference.property == "vrf_table"
        and difference.desired == 1001
        and difference.actual == 2001
        for difference in report.differences
    )


def test_pyroute2_configure_node_creates_vrfs_before_enslaving_and_addressing() -> None:
    plan = compile_plan(_manifest())
    node = plan.nodes["r1"]
    handle = Mock()
    indexes = {"blue0": 10, "red0": 11, "blue": 12, "red": 13}
    handle.link_lookup.side_effect = lambda *, ifname: [indexes[ifname]]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    backend.configure_node(node, plan)

    assert handle.mock_calls == [
        call.link_lookup(ifname="blue0"),
        call.link_lookup(ifname="red0"),
        call.link("add", ifname="blue", kind="vrf", vrf_table=1001),
        call.link_lookup(ifname="blue"),
        call.link("add", ifname="red", kind="vrf", vrf_table=1002),
        call.link_lookup(ifname="red"),
        call.link("set", index=12, state="up"),
        call.link_lookup(ifname="blue0"),
        call.link("set", index=10, state="down"),
        call.link("set", index=10, master=12),
        call.link("set", index=13, state="up"),
        call.link_lookup(ifname="red0"),
        call.link("set", index=11, state="down"),
        call.link("set", index=11, master=13),
        call.addr("add", index=10, address="10.0.0.1", prefixlen=24),
        call.link("set", index=10, state="up"),
        call.addr("add", index=11, address="10.0.0.1", prefixlen=24),
        call.link("set", index=11, state="up"),
        call.route(
            "add",
            dst="192.0.2.2/32",
            oif=10,
            gateway="10.0.0.2",
            table=1001,
        ),
        call.route(
            "add",
            dst="192.0.2.2/32",
            oif=11,
            gateway="10.0.0.2",
            table=1002,
        ),
        call.close(),
    ]


def test_pyroute2_inventory_decodes_vrf_table_membership_and_connected_routes() -> None:
    interfaces, _ = Pyroute2Backend._inventory_interfaces(
        (
            _link_message(10, "blue0", "veth", master=12),
            _link_message(11, "red0", "veth", master=13),
            _link_message(12, "blue", "vrf", vrf_table=1001),
            _link_message(13, "red", "vrf", vrf_table=1002),
        ),
        (
            {"index": 10, "prefixlen": 24, "attrs": [("IFA_LOCAL", "10.0.0.1")]},
            {"index": 11, "prefixlen": 24, "attrs": [("IFA_LOCAL", "10.0.0.1")]},
        ),
    )

    assert interfaces["blue"].kind == "vrf"
    assert interfaces["blue"].vrf_table == 1001
    assert interfaces["blue0"].master == "blue"
    assert interfaces["red0"].master == "red"
    routes = Pyroute2Backend._inventory_connected_routes(interfaces)
    assert len(routes) == 2
    assert {(route.dst, route.dev, route.table) for route in routes} == {
        (IPv4Network("10.0.0.0/24"), "blue0", 1001),
        (IPv4Network("10.0.0.0/24"), "red0", 1002),
    }


def test_graph_formats_show_vrf_tables_and_members() -> None:
    plan = compile_plan(_manifest())

    tree = render_graph(plan, "tree")
    mermaid = render_graph(plan, "mermaid")
    graph_json = json.loads(render_graph(plan, "json"))

    assert "blue: vrf table 1001 · members blue0" in tree
    assert "red: vrf table 1002 · members red0" in mermaid
    r1 = next(node for node in graph_json["nodes"] if node["name"] == "r1")
    assert r1["devices"] == [
        {"interfaces": ["blue0"], "name": "blue", "table": 1001, "type": "vrf"},
        {"interfaces": ["red0"], "name": "red", "table": 1002, "type": "vrf"},
    ]
