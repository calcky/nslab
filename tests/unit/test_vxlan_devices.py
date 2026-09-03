from __future__ import annotations

import copy
import json
from dataclasses import replace
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call

import pytest
from pydantic import ValidationError

from nslab.backend.base import inventory_matches_plan
from nslab.backend.fake import FakeNetworkBackend
from nslab.backend.pyroute2 import Pyroute2Backend
from nslab.graph import render_graph
from nslab.inspector import inspect_topology
from nslab.lifecycle import LifecycleService
from nslab.manifest import BridgeNode, Manifest, VxlanDeviceConfig, load_manifest
from nslab.planner import (
    VxlanDevicePlan,
    compile_plan,
    node_interface_master,
    vxlan_device_mtu,
)
from nslab.state import StateStore

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "vxlan" / "nslab.yaml"


def _document(*, ipv6: bool = False) -> dict[str, Any]:
    local1 = "2001:db8:12::1" if ipv6 else "192.0.2.1"
    local2 = "2001:db8:12::2" if ipv6 else "192.0.2.2"
    prefix1 = f"{local1}/64" if ipv6 else f"{local1}/30"
    prefix2 = f"{local2}/64" if ipv6 else f"{local2}/30"
    return {
        "version": 1,
        "name": "vxlan-test",
        "topology": {
            "nodes": {
                "h1": {
                    "kind": "linux",
                    "interfaces": {"eth0": {"addresses": ["10.70.0.1/24"]}},
                },
                "vtep1": {
                    "kind": "bridge",
                    "interfaces": {"underlay0": {"addresses": [prefix1]}},
                    "devices": {
                        "vxlan100": {
                            "type": "vxlan",
                            "vni": 100,
                            "link": "underlay0",
                            "local": local1,
                            "remote": local2,
                        }
                    },
                    "bridge": {
                        "name": "br0",
                        "stp": False,
                        "vlan_filtering": False,
                    },
                },
                "vtep2": {
                    "kind": "bridge",
                    "interfaces": {"underlay0": {"addresses": [prefix2]}},
                    "devices": {
                        "vxlan100": {
                            "type": "vxlan",
                            "vni": 100,
                            "link": "underlay0",
                            "local": local2,
                            "remote": local1,
                        }
                    },
                    "bridge": {
                        "name": "br0",
                        "stp": False,
                        "vlan_filtering": False,
                    },
                },
                "h2": {
                    "kind": "linux",
                    "interfaces": {"eth0": {"addresses": ["10.70.0.2/24"]}},
                },
            },
            "links": [
                {"endpoints": ["h1:eth0", "vtep1:access0"], "mtu": 1450},
                {"endpoints": ["vtep1:underlay0", "vtep2:underlay0"], "mtu": 1500},
                {"endpoints": ["vtep2:access0", "h2:eth0"], "mtu": 1450},
            ],
        },
    }


def _manifest(*, ipv6: bool = False) -> Manifest:
    return Manifest.model_validate(_document(ipv6=ipv6))


def _standalone_document() -> dict[str, Any]:
    return {
        "version": 1,
        "name": "vxlan-routed-test",
        "topology": {
            "nodes": {
                "r1": {
                    "kind": "linux",
                    "interfaces": {
                        "underlay0": {"addresses": ["192.0.2.1/30"]},
                    },
                    "devices": {
                        "vxlan100": {
                            "type": "vxlan",
                            "vni": 100,
                            "link": "underlay0",
                            "local": "192.0.2.1",
                            "remote": "192.0.2.2",
                            "addresses": ["10.255.100.1/30"],
                        }
                    },
                    "routes": [
                        {
                            "dst": "10.80.2.0/24",
                            "via": "10.255.100.2",
                            "dev": "vxlan100",
                        }
                    ],
                },
                "r2": {
                    "kind": "linux",
                    "interfaces": {
                        "underlay0": {"addresses": ["192.0.2.2/30"]},
                    },
                    "devices": {
                        "vxlan100": {
                            "type": "vxlan",
                            "vni": 100,
                            "link": "underlay0",
                            "local": "192.0.2.2",
                            "remote": "192.0.2.1",
                            "addresses": ["10.255.100.2/30"],
                        }
                    },
                    "routes": [
                        {
                            "dst": "10.80.1.0/24",
                            "via": "10.255.100.1",
                            "dev": "vxlan100",
                        }
                    ],
                },
            },
            "links": [{"endpoints": ["r1:underlay0", "r2:underlay0"]}],
        },
    }


def _standalone_manifest() -> Manifest:
    return Manifest.model_validate(_standalone_document())


def _vtep(document: dict[str, Any], name: str = "vtep1") -> dict[str, Any]:
    return document["topology"]["nodes"][name]


def test_vxlan_example_compiles_with_static_unicast_defaults() -> None:
    manifest = load_manifest(_EXAMPLE)
    plan = compile_plan(manifest)
    node = manifest.topology.nodes["vtep1"]

    assert isinstance(node, BridgeNode)
    config = node.devices["vxlan100"]
    assert isinstance(config, VxlanDeviceConfig)
    assert config.local == IPv4Address("192.0.2.1")
    assert config.remote == IPv4Address("192.0.2.2")
    assert config.dst_port == 4789
    assert config.learning is True

    device = plan.nodes["vtep1"].devices["vxlan100"]
    assert device == VxlanDevicePlan(
        name="vxlan100",
        vni=100,
        link="underlay0",
        local=IPv4Address("192.0.2.1"),
        remote=IPv4Address("192.0.2.2"),
        dst_port=4789,
        learning=True,
        mtu=None,
    )
    assert vxlan_device_mtu(plan.nodes["vtep1"], plan, device) == 1450


def test_combined_vxlan_example_contains_the_routed_overlay() -> None:
    manifest = load_manifest(_EXAMPLE)
    plan = compile_plan(manifest)
    mermaid = render_graph(plan, "mermaid")

    assert manifest.name == "vxlan"
    assert plan.name == "vxlan"
    assert tuple(plan.nodes) == (
        "h1",
        "vtep1",
        "vtep2",
        "h2",
        "h3",
        "r1",
        "r2",
        "h4",
        "underlay",
    )
    for node_name, local, remote, address in (
        ("r1", "192.0.2.3", "192.0.2.4", "10.255.200.1/30"),
        ("r2", "192.0.2.4", "192.0.2.3", "10.255.200.2/30"),
    ):
        node = plan.nodes[node_name]
        device = node.devices["vxlan200"]
        assert isinstance(device, VxlanDevicePlan)
        assert device.local == IPv4Address(local)
        assert device.remote == IPv4Address(remote)
        assert device.addresses[0].with_prefixlen == address
        assert node_interface_master(node, "vxlan200") is None
        assert node.sysctls["net.ipv4.ip_forward"] == 1

    assert mermaid.startswith('%%{init: {"flowchart": {"curve": "step"}}}%%\nflowchart TB\n')
    assert 'n8 -- "p1 ↔ underlay0" --- n1' in mermaid
    assert 'n1 -- "access0 ↔ eth0" --- n0' in mermaid
    assert 'n5["r1\\nlinux\\nlan0: 10.80.1.254/24' in mermaid
    assert "vxlan100: vxlan 100 -> 192.0.2.2" in mermaid
    assert "vxlan200: vxlan 200 -> 192.0.2.4" in mermaid
    assert "classDef" not in mermaid


def test_ipv6_vxlan_uses_larger_encapsulation_overhead() -> None:
    plan = compile_plan(_manifest(ipv6=True))
    device = plan.nodes["vtep1"].devices["vxlan100"]

    assert isinstance(device, VxlanDevicePlan)
    assert device.local == IPv6Address("2001:db8:12::1")
    assert vxlan_device_mtu(plan.nodes["vtep1"], plan, device) == 1430


def test_explicit_vxlan_options_are_preserved() -> None:
    document = _document()
    config = _vtep(document)["devices"]["vxlan100"]
    config.update(dst_port=8472, learning=False, mtu=1400)

    device = compile_plan(Manifest.model_validate(document)).nodes["vtep1"].devices["vxlan100"]

    assert isinstance(device, VxlanDevicePlan)
    assert device.dst_port == 8472
    assert device.learning is False
    assert device.mtu == 1400


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda node: node["devices"]["vxlan100"].update(vni=0), "greater than or equal"),
        (lambda node: node["devices"]["vxlan100"].update(vni=16_777_216), "less than or equal"),
        (lambda node: node["devices"]["vxlan100"].update(dst_port=0), "greater than or equal"),
        (lambda node: node["devices"]["vxlan100"].update(learning=1), "valid boolean"),
        (
            lambda node: node["devices"]["vxlan100"].update(remote="2001:db8::2"),
            "same address family",
        ),
        (
            lambda node: node["devices"]["vxlan100"].update(remote="192.0.2.1"),
            "must be different",
        ),
        (
            lambda node: node["devices"]["vxlan100"].update(remote="239.1.1.1"),
            "must be unicast",
        ),
        (
            lambda node: node["devices"]["vxlan100"].update(local="0.0.0.0"),
            "must be unicast",
        ),
    ],
)
def test_manifest_rejects_invalid_vxlan_values(mutation: Any, message: str) -> None:
    document = _document()
    mutation(_vtep(document))

    with pytest.raises(ValidationError, match=message):
        Manifest.model_validate(document)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda document: _vtep(document)["devices"]["vxlan100"].update(link="missing0"),
            "underlay interface is not linked",
        ),
        (
            lambda document: _vtep(document)["devices"]["vxlan100"].update(local="192.0.2.9"),
            "local address is not configured",
        ),
        (
            lambda document: _vtep(document)["devices"].update(
                vxlan200={
                    **copy.deepcopy(_vtep(document)["devices"]["vxlan100"]),
                    "remote": "192.0.2.3",
                }
            ),
            "duplicate VXLAN VNI",
        ),
        (
            lambda document: _vtep(document)["bridge"].update(
                stp=True, ports={"underlay0": {"path_cost": 10}}
            ),
            "underlay interface cannot be a bridge port",
        ),
        (
            lambda document: _vtep(document)["devices"]["vxlan100"].update(mtu=1451),
            "exceeds encapsulation limit",
        ),
    ],
)
def test_manifest_rejects_invalid_vxlan_references(mutation: Any, message: str) -> None:
    document = _document()
    mutation(document)

    with pytest.raises(ValidationError, match=message):
        Manifest.model_validate(document)


def test_bridge_port_settings_may_reference_vxlan_device() -> None:
    document = _document()
    node = _vtep(document)
    node["bridge"].update(
        stp=True,
        vlan_filtering=True,
        ports={
            "vxlan100": {
                "path_cost": 50,
                "vlans": [{"vid": 10}],
            }
        },
    )

    plan = compile_plan(Manifest.model_validate(document))

    assert plan.nodes["vtep1"].bridge_ports["vxlan100"].path_cost == 50


def test_underlay_stays_outside_bridge_and_vxlan_joins_it() -> None:
    plan = compile_plan(_manifest())
    node = plan.nodes["vtep1"]

    assert node_interface_master(node, "underlay0") is None
    assert node_interface_master(node, "access0") == "br0"
    assert node_interface_master(node, "vxlan100") == "br0"


def test_linux_vxlan_stays_standalone_and_accepts_addresses_and_routes() -> None:
    manifest = _standalone_manifest()
    plan = compile_plan(manifest)
    node = manifest.topology.nodes["r1"]
    assert node.kind == "linux"

    config = node.devices["vxlan100"]
    assert isinstance(config, VxlanDeviceConfig)
    assert config.addresses[0].with_prefixlen == "10.255.100.1/30"

    planned = plan.nodes["r1"]
    device = planned.devices["vxlan100"]
    assert isinstance(device, VxlanDevicePlan)
    assert device.addresses[0].with_prefixlen == "10.255.100.1/30"
    assert node_interface_master(planned, "vxlan100") is None
    assert planned.routes[0].dev == "vxlan100"


def test_bridge_vxlan_rejects_interface_addresses() -> None:
    document = _document()
    _vtep(document)["devices"]["vxlan100"]["addresses"] = ["10.70.0.254/24"]

    with pytest.raises(ValidationError, match="bridge VXLAN device cannot declare addresses"):
        Manifest.model_validate(document)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda document: document["topology"]["nodes"]["r1"]["devices"]["vxlan100"].update(
                link="missing0"
            ),
            "underlay interface is not linked",
        ),
        (
            lambda document: document["topology"]["nodes"]["r1"]["devices"]["vxlan100"].update(
                local="192.0.2.9"
            ),
            "local address is not configured",
        ),
    ],
)
def test_linux_vxlan_rejects_invalid_underlay_references(mutation: Any, message: str) -> None:
    document = _standalone_document()
    mutation(document)

    with pytest.raises(ValidationError, match=message):
        Manifest.model_validate(document)


def test_fake_backend_preserves_standalone_vxlan_state(tmp_path: Path) -> None:
    manifest = _standalone_manifest()
    plan = compile_plan(manifest)
    backend = FakeNetworkBackend()
    service = LifecycleService(backend, StateStore(tmp_path / "state"))

    result = service.deploy(plan, manifest)

    assert result.status == "deployed"
    assert inventory_matches_plan(plan, backend.inventory(plan))
    interface = (
        backend.inventory(plan).namespaces[plan.nodes["r1"].namespace].interfaces["vxlan100"]
    )
    assert interface.master is None
    assert interface.addresses[0].with_prefixlen == "10.255.100.1/30"

    service.destroy(plan, plan.name)


def test_pyroute2_configure_node_creates_standalone_vxlan_with_address() -> None:
    plan = compile_plan(_standalone_manifest())
    node = plan.nodes["r1"]
    handle = Mock()
    indexes = {"underlay0": 10, "vxlan100": 11}
    handle.link_lookup.side_effect = lambda *, ifname: [indexes[ifname]]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    backend.configure_node(node, plan)

    assert call.link("set", index=11, mtu=1450) in handle.mock_calls
    assert call.link("set", index=11, state="up") in handle.mock_calls
    assert call.link("set", index=11, master=999) not in handle.mock_calls
    assert call.addr("add", index=11, address="10.255.100.1", prefixlen=30) in handle.mock_calls


def test_fake_backend_snapshot_inventory_and_drift_include_vxlan_state(tmp_path: Path) -> None:
    manifest = _manifest()
    plan = compile_plan(manifest)
    backend = FakeNetworkBackend()
    store = StateStore(tmp_path)
    service = LifecycleService(backend, store)

    result = service.deploy(plan, manifest)

    assert result.status == "deployed"
    inventory = backend.inventory(plan)
    assert inventory_matches_plan(plan, inventory)
    namespace = inventory.namespaces[plan.nodes["vtep1"].namespace]
    underlay = namespace.interfaces["underlay0"]
    vxlan = namespace.interfaces["vxlan100"]
    assert underlay.master is None
    assert vxlan.master == "br0"
    assert vxlan.mtu == 1450
    assert vxlan.vxlan_vni == 100
    assert vxlan.vxlan_link == "underlay0"
    assert vxlan.vxlan_local == IPv4Address("192.0.2.1")
    assert vxlan.vxlan_remote == IPv4Address("192.0.2.2")
    assert vxlan.vxlan_dst_port == 4789
    assert vxlan.vxlan_learning is True

    snapshot = store.load(plan.name)
    assert snapshot is not None
    assert snapshot.interfaces["vtep1:vxlan100"] == {
        "name": "vxlan100",
        "kind": "vxlan",
        "namespace": plan.nodes["vtep1"].namespace,
        "vni": 100,
        "link": "underlay0",
        "local": "192.0.2.1",
        "remote": "192.0.2.2",
        "dst_port": 4789,
        "ifindex": vxlan.ifindex,
    }

    state = backend.namespaces[plan.nodes["vtep1"].namespace]
    state.interfaces["vxlan100"] = replace(vxlan, vxlan_learning=False)
    drifted = backend.inventory(plan)
    assert not inventory_matches_plan(plan, drifted)
    report = inspect_topology(plan, snapshot, drifted)
    assert report.status == "degraded"
    assert any(
        difference.node == "vtep1"
        and difference.interface == "vxlan100"
        and difference.property == "vxlan_learning"
        and difference.desired is True
        and difference.actual is False
        for difference in report.differences
    )


def test_pyroute2_configure_node_creates_vxlan_and_bridge_membership() -> None:
    plan = compile_plan(_manifest())
    node = plan.nodes["vtep1"]
    handle = Mock()
    indexes = {"underlay0": 10, "br0": 11, "access0": 12, "vxlan100": 13}
    handle.link_lookup.side_effect = lambda *, ifname: [indexes[ifname]]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    backend.configure_node(node, plan)

    assert (
        call.link(
            "add",
            ifname="vxlan100",
            kind="vxlan",
            vxlan_id=100,
            vxlan_link=10,
            vxlan_local="192.0.2.1",
            vxlan_group="192.0.2.2",
            vxlan_port=4789,
            vxlan_learning=1,
        )
        in handle.mock_calls
    )
    assert call.link("set", index=13, mtu=1450) in handle.mock_calls
    assert call.link("set", index=12, master=11) in handle.mock_calls
    assert call.link("set", index=13, master=11) in handle.mock_calls
    assert call.link("set", index=10, master=11) not in handle.mock_calls
    assert call.addr("add", index=10, address="192.0.2.1", prefixlen=30) in handle.mock_calls
    assert call.link("set", index=13, state="up") in handle.mock_calls


def test_pyroute2_configure_node_uses_ipv6_vxlan_attributes() -> None:
    plan = compile_plan(_manifest(ipv6=True))
    node = plan.nodes["vtep1"]
    handle = Mock()
    indexes = {"underlay0": 10, "br0": 11, "access0": 12, "vxlan100": 13}
    handle.link_lookup.side_effect = lambda *, ifname: [indexes[ifname]]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    backend.configure_node(node, plan)

    assert (
        call.link(
            "add",
            ifname="vxlan100",
            kind="vxlan",
            vxlan_id=100,
            vxlan_link=10,
            vxlan_local6="2001:db8:12::1",
            vxlan_group6="2001:db8:12::2",
            vxlan_port=4789,
            vxlan_learning=1,
        )
        in handle.mock_calls
    )
    assert call.link("set", index=13, mtu=1430) in handle.mock_calls


def _link_message(
    index: int,
    name: str,
    kind: str,
    *,
    master: int | None = None,
    vxlan_data: list[tuple[str, object]] | None = None,
) -> dict[str, object]:
    link_info: list[tuple[str, object]] = [("IFLA_INFO_KIND", kind)]
    if vxlan_data is not None:
        link_info.append(("IFLA_INFO_DATA", {"attrs": vxlan_data}))
    attributes: list[tuple[str, object]] = [
        ("IFLA_IFNAME", name),
        ("IFLA_MTU", 1450 if kind == "vxlan" else 1500),
        ("IFLA_LINKINFO", {"attrs": link_info}),
    ]
    if master is not None:
        attributes.append(("IFLA_MASTER", master))
    return {"index": index, "flags": 1, "attrs": attributes}


def test_pyroute2_inventory_decodes_vxlan_properties() -> None:
    interfaces, _ = Pyroute2Backend._inventory_interfaces(
        (
            _link_message(10, "underlay0", "veth"),
            _link_message(11, "br0", "bridge"),
            _link_message(
                13,
                "vxlan100",
                "vxlan",
                master=11,
                vxlan_data=[
                    ("IFLA_VXLAN_ID", 100),
                    ("IFLA_VXLAN_LINK", 10),
                    ("IFLA_VXLAN_LOCAL", "192.0.2.1"),
                    ("IFLA_VXLAN_GROUP", "192.0.2.2"),
                    ("IFLA_VXLAN_PORT", 4789),
                    ("IFLA_VXLAN_LEARNING", 1),
                ],
            ),
        ),
        (),
    )

    vxlan = interfaces["vxlan100"]
    assert vxlan.vxlan_vni == 100
    assert vxlan.vxlan_link == "underlay0"
    assert vxlan.vxlan_local == IPv4Address("192.0.2.1")
    assert vxlan.vxlan_remote == IPv4Address("192.0.2.2")
    assert vxlan.vxlan_dst_port == 4789
    assert vxlan.vxlan_learning is True


def test_graph_formats_show_vxlan_summary_and_details() -> None:
    plan = compile_plan(_manifest())

    tree = render_graph(plan, "tree")
    detailed = render_graph(plan, "tree", detail=True)
    mermaid = render_graph(plan, "mermaid")
    graph_json = json.loads(render_graph(plan, "json"))

    summary = "vxlan100: vxlan 100 -> 192.0.2.2"
    assert summary in tree
    assert mermaid.startswith('%%{init: {"flowchart": {"curve": "step"}}}%%\nflowchart TB\n')
    assert 'n1["vtep1\\nbridge · br0\\nunderlay0: 192.0.2.1/30' in mermaid
    assert 'n1 -- "access0 ↔ eth0" --- n0' in mermaid
    assert "classDef" not in mermaid
    assert "local 192.0.2.1 via underlay0" in detailed
    assert "udp 4789" in detailed
    assert "learning on" in detailed
    vtep1 = next(node for node in graph_json["nodes"] if node["name"] == "vtep1")
    vxlan = vtep1["devices"][0]
    assert vxlan == {
        "dst_port": 4789,
        "learning": True,
        "link": "underlay0",
        "local": "192.0.2.1",
        "mtu": None,
        "name": "vxlan100",
        "remote": "192.0.2.2",
        "type": "vxlan",
        "vni": 100,
    }
