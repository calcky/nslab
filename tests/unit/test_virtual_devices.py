from __future__ import annotations

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
from nslab.manifest import (
    BridgeNode,
    DummyDeviceConfig,
    GeneveDeviceConfig,
    IpvlanDeviceConfig,
    LinuxNode,
    MacvlanDeviceConfig,
    Manifest,
)
from nslab.planner import (
    DummyDevicePlan,
    GeneveDevicePlan,
    IpvlanDevicePlan,
    MacvlanDevicePlan,
    compile_plan,
    geneve_device_mtu,
    node_interface_master,
)
from nslab.state import StateStore


def _virtual_document() -> dict[str, Any]:
    return {
        "version": 1,
        "name": "virtual-devices",
        "topology": {
            "nodes": {
                "h1": {
                    "kind": "linux",
                    "interfaces": {
                        "eth0": {"addresses": ["192.0.2.1/24"]},
                        "eth1": {"addresses": ["198.51.100.1/24"]},
                    },
                    "devices": {
                        "dummy0": {
                            "type": "dummy",
                            "mtu": 1400,
                            "addresses": ["203.0.113.1/32"],
                        },
                        "mac0": {
                            "type": "macvlan",
                            "link": "eth0",
                            "mode": "bridge",
                            "addresses": ["192.0.2.10/24"],
                        },
                        "ip0": {
                            "type": "ipvlan",
                            "link": "eth1",
                            "mode": "l3",
                            "addresses": ["198.51.100.10/24"],
                        },
                    },
                },
                "h2": {
                    "kind": "linux",
                    "interfaces": {"eth0": {"addresses": ["192.0.2.2/24"]}},
                },
                "h3": {
                    "kind": "linux",
                    "interfaces": {"eth0": {"addresses": ["198.51.100.2/24"]}},
                },
            },
            "links": [
                {"endpoints": ["h1:eth0", "h2:eth0"]},
                {"endpoints": ["h1:eth1", "h3:eth0"]},
            ],
        },
    }


def _geneve_document(*, ipv6: bool = False) -> dict[str, Any]:
    if ipv6:
        underlay1, underlay2, prefix = "2001:db8:1::1", "2001:db8:1::2", "/64"
    else:
        underlay1, underlay2, prefix = "192.0.2.1", "192.0.2.2", "/30"
    return {
        "version": 1,
        "name": "geneve",
        "topology": {
            "nodes": {
                "h1": {
                    "kind": "linux",
                    "interfaces": {"eth0": {"addresses": ["10.0.0.1/24"]}},
                },
                "vtep1": {
                    "kind": "bridge",
                    "interfaces": {"underlay0": {"addresses": [underlay1 + prefix]}},
                    "devices": {
                        "geneve100": {
                            "type": "geneve",
                            "vni": 100,
                            "link": "underlay0",
                            "remote": underlay2,
                        }
                    },
                    "bridge": {"name": "br0", "stp": False, "vlan_filtering": False},
                },
                "vtep2": {
                    "kind": "bridge",
                    "interfaces": {"underlay0": {"addresses": [underlay2 + prefix]}},
                    "devices": {
                        "geneve100": {
                            "type": "geneve",
                            "vni": 100,
                            "link": "underlay0",
                            "remote": underlay1,
                        }
                    },
                    "bridge": {"name": "br0", "stp": False, "vlan_filtering": False},
                },
                "h2": {
                    "kind": "linux",
                    "interfaces": {"eth0": {"addresses": ["10.0.0.2/24"]}},
                },
            },
            "links": [
                {"endpoints": ["h1:eth0", "vtep1:access0"], "mtu": 1450},
                {"endpoints": ["vtep1:underlay0", "vtep2:underlay0"]},
                {"endpoints": ["vtep2:access0", "h2:eth0"], "mtu": 1450},
            ],
        },
    }


def test_virtual_device_defaults_and_plan() -> None:
    manifest = Manifest.model_validate(_virtual_document())
    node = manifest.topology.nodes["h1"]
    assert isinstance(node, LinuxNode)
    assert isinstance(node.devices["dummy0"], DummyDeviceConfig)
    assert isinstance(node.devices["mac0"], MacvlanDeviceConfig)
    assert isinstance(node.devices["ip0"], IpvlanDeviceConfig)

    plan = compile_plan(manifest)
    assert plan.nodes["h1"].devices["dummy0"] == DummyDevicePlan(
        name="dummy0", mtu=1400, addresses=(next(iter(node.devices["dummy0"].addresses)),)
    )
    mac = plan.nodes["h1"].devices["mac0"]
    ip = plan.nodes["h1"].devices["ip0"]
    assert isinstance(mac, MacvlanDevicePlan)
    assert isinstance(ip, IpvlanDevicePlan)
    assert mac.mode == "bridge"
    assert ip.mode == "l3"
    assert mac.link == "eth0"
    assert ip.link == "eth1"


def test_geneve_ipv4_and_ipv6_compile_and_derive_mtu() -> None:
    for ipv6, remote_type in ((False, IPv4Address), (True, IPv6Address)):
        manifest = Manifest.model_validate(_geneve_document(ipv6=ipv6))
        node = manifest.topology.nodes["vtep1"]
        assert isinstance(node, BridgeNode)
        config = node.devices["geneve100"]
        assert isinstance(config, GeneveDeviceConfig)
        assert isinstance(config.remote, remote_type)
        plan = compile_plan(manifest)
        device = plan.nodes["vtep1"].devices["geneve100"]
        assert isinstance(device, GeneveDevicePlan)
        assert geneve_device_mtu(plan.nodes["vtep1"], plan, device) == (1450 if not ipv6 else 1430)
        assert node_interface_master(plan.nodes["vtep1"], "underlay0") is None
        assert node_interface_master(plan.nodes["vtep1"], "geneve100") == "br0"


@pytest.mark.parametrize(
    ("device_type", "mode"),
    [("macvlan", "invalid"), ("ipvlan", "invalid")],
)
def test_virtual_device_rejects_invalid_mode(device_type: str, mode: str) -> None:
    document = _virtual_document()
    document["topology"]["nodes"]["h1"]["devices"]["mac0" if device_type == "macvlan" else "ip0"][
        "mode"
    ] = mode
    with pytest.raises(ValidationError, match="Input should be"):
        Manifest.model_validate(document)


@pytest.mark.parametrize(
    ("device_name", "message"),
    [
        ("mac0", "parent interface is not linked"),
        ("ip0", "parent interface is not linked"),
    ],
)
def test_virtual_device_rejects_unlinked_parent(device_name: str, message: str) -> None:
    document = _virtual_document()
    document["topology"]["nodes"]["h1"]["devices"][device_name]["link"] = "missing0"
    with pytest.raises(ValidationError, match=message):
        Manifest.model_validate(document)


def test_geneve_rejects_invalid_remote_and_mtu() -> None:
    document = _geneve_document()
    document["topology"]["nodes"]["vtep1"]["devices"]["geneve100"]["remote"] = "239.1.1.1"
    with pytest.raises(ValidationError, match="must be a unicast"):
        Manifest.model_validate(document)

    document = _geneve_document()
    document["topology"]["nodes"]["vtep1"]["devices"]["geneve100"]["mtu"] = 1451
    with pytest.raises(ValidationError, match="exceeds encapsulation limit"):
        Manifest.model_validate(document)


def test_fake_lifecycle_inventory_snapshot_and_graph(tmp_path: Path) -> None:
    manifest = Manifest.model_validate(_virtual_document())
    plan = compile_plan(manifest)
    backend = FakeNetworkBackend()
    service = LifecycleService(backend, StateStore(tmp_path / "state"))

    assert service.deploy(plan, manifest).status == "deployed"
    inventory = backend.inventory(plan)
    assert inventory_matches_plan(plan, inventory)
    h1 = inventory.namespaces[plan.nodes["h1"].namespace].interfaces
    assert h1["dummy0"].kind == "dummy"
    assert h1["mac0"].macvlan_mode == "bridge"
    assert h1["ip0"].ipvlan_mode == "l3"
    snapshot = StateStore(tmp_path / "state").load(plan.name)
    assert snapshot is not None
    assert snapshot.interfaces["h1:dummy0"]["kind"] == "dummy"
    assert snapshot.interfaces["h1:mac0"]["mode"] == "bridge"
    assert snapshot.interfaces["h1:ip0"]["mode"] == "l3"
    assert "dummy0: dummy" in render_graph(plan, "tree")
    document = json.loads(render_graph(plan, "json"))
    h1_document = next(node for node in document["nodes"] if node["name"] == "h1")
    assert {device["type"] for device in h1_document["devices"]} == {
        "dummy",
        "macvlan",
        "ipvlan",
    }
    assert service.destroy(plan, plan.name).status == "absent"


def test_pyroute2_configures_virtual_devices() -> None:
    plan = compile_plan(Manifest.model_validate(_virtual_document()))
    node = plan.nodes["h1"]
    handle = Mock()
    indexes = {"eth0": 10, "eth1": 11, "dummy0": 12, "mac0": 13, "ip0": 14}
    handle.link_lookup.side_effect = lambda *, ifname: [indexes[ifname]]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    backend.configure_node(node, plan)

    assert call.link("add", ifname="dummy0", kind="dummy") in handle.mock_calls
    assert (
        call.link("add", ifname="mac0", kind="macvlan", link=10, macvlan_mode=4)
        in handle.mock_calls
    )
    assert (
        call.link("add", ifname="ip0", kind="ipvlan", link=11, ipvlan_mode=1) in handle.mock_calls
    )
    assert call.addr("add", index=12, address="203.0.113.1", prefixlen=32) in handle.mock_calls


@pytest.mark.parametrize("ipv6", [False, True])
def test_pyroute2_configures_geneve_without_unsupported_link_attribute(ipv6: bool) -> None:
    plan = compile_plan(Manifest.model_validate(_geneve_document(ipv6=ipv6)))
    node = plan.nodes["vtep1"]
    handle = Mock()
    indexes = {"underlay0": 10, "br0": 11, "access0": 12, "geneve100": 13}
    handle.link_lookup.side_effect = lambda *, ifname: [indexes[ifname]]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    backend.configure_node(node, plan)

    expected = {
        "ifname": "geneve100",
        "kind": "geneve",
        "geneve_id": 100,
        "geneve_port": 6081,
        "geneve_remote6" if ipv6 else "geneve_remote": ("2001:db8:1::2" if ipv6 else "192.0.2.2"),
    }
    assert call.link("add", **expected) in handle.mock_calls
    assert all(
        not (item[0] == "link" and "geneve_link" in item[2])
        for item in handle.mock_calls
        if len(item) >= 3 and item[0] == "link"
    )


def test_geneve_inventory_without_underlay_attribute_matches_plan(tmp_path: Path) -> None:
    manifest = Manifest.model_validate(_geneve_document())
    plan = compile_plan(manifest)
    backend = FakeNetworkBackend()
    store = StateStore(tmp_path / "state")
    service = LifecycleService(backend, store)
    service.deploy(plan, manifest)
    inventory = backend.inventory(plan)
    namespace = inventory.namespaces[plan.nodes["vtep1"].namespace]
    geneve = namespace.interfaces["geneve100"]
    assert geneve.geneve_link == "underlay0"
    # A real kernel dump omits the fixed underlay field.  The matcher accepts
    # that representation while retaining the declarative link in the plan.
    stripped = replace(geneve, geneve_link=None)
    stripped_inventory = replace(
        inventory,
        namespaces={
            **inventory.namespaces,
            plan.nodes["vtep1"].namespace: replace(
                namespace,
                interfaces={**namespace.interfaces, "geneve100": stripped},
            ),
        },
    )
    assert inventory_matches_plan(plan, stripped_inventory)
    snapshot = store.load(plan.name)
    assert snapshot is not None
    report = inspect_topology(plan, snapshot, stripped_inventory)
    assert report.status == "deployed"
    assert not any(difference.property == "geneve_link" for difference in report.differences)
    service.destroy(plan, plan.name)


def _link_message(
    index: int,
    name: str,
    kind: str,
    *,
    parent_index: int | None = None,
    info_data: list[tuple[str, object]] | None = None,
) -> dict[str, object]:
    link_info: list[tuple[str, object]] = [("IFLA_INFO_KIND", kind)]
    if info_data is not None:
        link_info.append(("IFLA_INFO_DATA", {"attrs": info_data}))
    attributes: list[tuple[str, object]] = [
        ("IFLA_IFNAME", name),
        ("IFLA_MTU", 1450 if kind == "geneve" else 1500),
        ("IFLA_LINKINFO", {"attrs": link_info}),
    ]
    if parent_index is not None:
        attributes.append(("IFLA_LINK", parent_index))
    return {"index": index, "flags": 1, "attrs": attributes}


def test_pyroute2_inventory_decodes_geneve_macvlan_and_ipvlan() -> None:
    messages = (
        _link_message(10, "underlay0", "veth"),
        _link_message(
            11,
            "geneve100",
            "geneve",
            info_data=[
                ("IFLA_GENEVE_ID", 100),
                ("IFLA_GENEVE_REMOTE", "192.0.2.2"),
                ("IFLA_GENEVE_PORT", 6081),
            ],
        ),
        _link_message(
            12,
            "mac0",
            "macvlan",
            parent_index=10,
            info_data=[("IFLA_MACVLAN_MODE", 4)],
        ),
        _link_message(
            13,
            "ip0",
            "ipvlan",
            parent_index=10,
            info_data=[("IFLA_IPVLAN_MODE", 1)],
        ),
    )
    interfaces, _ = Pyroute2Backend._inventory_interfaces(messages, ())
    assert interfaces["geneve100"].geneve_vni == 100
    assert interfaces["geneve100"].geneve_link is None
    assert interfaces["geneve100"].geneve_remote == IPv4Address("192.0.2.2")
    assert interfaces["geneve100"].geneve_dst_port == 6081
    assert interfaces["mac0"].parent == "underlay0"
    assert interfaces["mac0"].macvlan_mode == "bridge"
    assert interfaces["ip0"].parent == "underlay0"
    assert interfaces["ip0"].ipvlan_mode == "l3"
