from __future__ import annotations

import json
from dataclasses import replace
from ipaddress import IPv4Address, IPv4Interface
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
from nslab.manifest import GreDeviceConfig, IpipDeviceConfig, LinuxNode, Manifest
from nslab.planner import (
    GreDevicePlan,
    IpipDevicePlan,
    compile_plan,
    gre_device_mtu,
    ipip_device_mtu,
)
from nslab.state import StateStore


def _tunnel_document() -> dict[str, Any]:
    return {
        "version": 1,
        "name": "ip-tunnels",
        "topology": {
            "nodes": {
                "r1": {
                    "kind": "linux",
                    "interfaces": {"underlay0": {"addresses": ["192.0.2.1/30"]}},
                    "devices": {
                        "gre1": {
                            "type": "gre",
                            "link": "underlay0",
                            "local": "192.0.2.1",
                            "remote": "192.0.2.2",
                            "key": 100,
                            "addresses": ["10.10.0.1/30"],
                        },
                        "ipip0": {
                            "type": "ipip",
                            "link": "underlay0",
                            "local": "192.0.2.1",
                            "remote": "192.0.2.2",
                            "addresses": ["10.20.0.1/30"],
                        },
                    },
                },
                "r2": {
                    "kind": "linux",
                    "interfaces": {"underlay0": {"addresses": ["192.0.2.2/30"]}},
                    "devices": {
                        "gre1": {
                            "type": "gre",
                            "link": "underlay0",
                            "local": "192.0.2.2",
                            "remote": "192.0.2.1",
                            "key": 100,
                            "addresses": ["10.10.0.2/30"],
                        },
                        "ipip0": {
                            "type": "ipip",
                            "link": "underlay0",
                            "local": "192.0.2.2",
                            "remote": "192.0.2.1",
                            "addresses": ["10.20.0.2/30"],
                        },
                    },
                },
            },
            "links": [{"endpoints": ["r1:underlay0", "r2:underlay0"]}],
        },
    }


def test_tunnel_manifest_compiles_and_derives_mtu() -> None:
    manifest = Manifest.model_validate(_tunnel_document())
    manifest_node = manifest.topology.nodes["r1"]
    assert isinstance(manifest_node, LinuxNode)
    assert isinstance(manifest_node.devices["gre1"], GreDeviceConfig)
    assert isinstance(manifest_node.devices["ipip0"], IpipDeviceConfig)

    plan = compile_plan(manifest)
    node = plan.nodes["r1"]
    gre = node.devices["gre1"]
    ipip = node.devices["ipip0"]
    assert gre == GreDevicePlan(
        name="gre1",
        link="underlay0",
        local=IPv4Address("192.0.2.1"),
        remote=IPv4Address("192.0.2.2"),
        key=100,
        ttl=64,
        addresses=(IPv4Interface("10.10.0.1/30"),),
    )
    assert ipip == IpipDevicePlan(
        name="ipip0",
        link="underlay0",
        local=IPv4Address("192.0.2.1"),
        remote=IPv4Address("192.0.2.2"),
        ttl=64,
        addresses=(IPv4Interface("10.20.0.1/30"),),
    )
    assert gre_device_mtu(node, plan, gre) == 1472
    assert ipip_device_mtu(node, plan, ipip) == 1480


def test_unkeyed_gre_uses_smaller_encapsulation_overhead() -> None:
    document = _tunnel_document()
    document["topology"]["nodes"]["r1"]["devices"]["gre1"].pop("key")
    plan = compile_plan(Manifest.model_validate(document))
    node = plan.nodes["r1"]
    gre = node.devices["gre1"]
    assert isinstance(gre, GreDevicePlan)
    assert gre.key is None
    assert gre_device_mtu(node, plan, gre) == 1476


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda node: node["devices"]["gre1"].update(link="missing0"),
            "GRE underlay interface is not linked",
        ),
        (
            lambda node: node["devices"]["ipip0"].update(link="missing0"),
            "IPIP underlay interface is not linked",
        ),
        (
            lambda node: node["devices"]["gre1"].update(local="192.0.2.9"),
            "GRE local address is not configured",
        ),
        (
            lambda node: node["devices"]["ipip0"].update(local="192.0.2.9"),
            "IPIP local address is not configured",
        ),
        (
            lambda node: node["devices"]["gre1"].update(remote="192.0.2.1"),
            "GRE local and remote addresses must be different",
        ),
        (
            lambda node: node["devices"]["ipip0"].update(remote="239.1.1.1"),
            "IPIP endpoints must be unicast",
        ),
        (
            lambda node: node["devices"]["gre1"].update(mtu=1473),
            "GRE MTU exceeds encapsulation limit",
        ),
        (
            lambda node: node["devices"]["ipip0"].update(mtu=1481),
            "IPIP MTU exceeds encapsulation limit",
        ),
    ],
)
def test_tunnel_manifest_rejects_invalid_references(
    mutation: Any,
    message: str,
) -> None:
    document = _tunnel_document()
    mutation(document["topology"]["nodes"]["r1"])
    with pytest.raises(ValidationError, match=message):
        Manifest.model_validate(document)


@pytest.mark.parametrize(
    ("device", "field", "value"),
    [
        ("gre1", "ttl", 0),
        ("gre1", "ttl", 256),
        ("ipip0", "ttl", 0),
        ("ipip0", "ttl", 256),
        ("gre1", "key", 0),
        ("gre1", "key", 4_294_967_296),
    ],
)
def test_tunnel_manifest_rejects_out_of_range_options(
    device: str,
    field: str,
    value: int,
) -> None:
    document = _tunnel_document()
    document["topology"]["nodes"]["r1"]["devices"][device][field] = value
    with pytest.raises(ValidationError):
        Manifest.model_validate(document)


def test_ipip_rejects_ipv6_inner_address() -> None:
    document = _tunnel_document()
    document["topology"]["nodes"]["r1"]["devices"]["ipip0"]["addresses"] = ["2001:db8::1/64"]
    with pytest.raises(ValidationError, match="IPIP device addresses must use IPv4"):
        Manifest.model_validate(document)


def test_tunnel_devices_are_linux_only() -> None:
    document = _tunnel_document()
    node = document["topology"]["nodes"]["r1"]
    node["kind"] = "bridge"
    node["bridge"] = {"name": "br0", "stp": False, "vlan_filtering": False}
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        Manifest.model_validate(document)


@pytest.mark.parametrize(
    ("device", "reserved_name", "message"),
    [
        ("gre1", "gre0", "reserved by the kernel"),
        ("gre1", "gretap0", "reserved by the kernel"),
        ("gre1", "erspan0", "reserved by the kernel"),
        ("ipip0", "tunl0", "reserved by the kernel"),
    ],
)
def test_tunnel_manifest_rejects_kernel_fallback_names(
    device: str,
    reserved_name: str,
    message: str,
) -> None:
    document = _tunnel_document()
    devices = document["topology"]["nodes"]["r1"]["devices"]
    devices[reserved_name] = devices.pop(device)
    with pytest.raises(ValidationError, match=message):
        Manifest.model_validate(document)


def test_fake_lifecycle_tracks_tunnel_identity_inventory_and_drift(tmp_path: Path) -> None:
    manifest = Manifest.model_validate(_tunnel_document())
    plan = compile_plan(manifest)
    backend = FakeNetworkBackend()
    store = StateStore(tmp_path / "state")
    service = LifecycleService(backend, store)

    assert service.deploy(plan, manifest).status == "deployed"
    inventory = backend.inventory(plan)
    assert inventory_matches_plan(plan, inventory)
    namespace = inventory.namespaces[plan.nodes["r1"].namespace]
    gre = namespace.interfaces["gre1"]
    ipip = namespace.interfaces["ipip0"]
    assert gre.mtu == 1472
    assert gre.gre_link == "underlay0"
    assert gre.gre_local == IPv4Address("192.0.2.1")
    assert gre.gre_remote == IPv4Address("192.0.2.2")
    assert gre.gre_key == 100
    assert gre.gre_ttl == 64
    assert ipip.mtu == 1480
    assert ipip.ipip_link == "underlay0"
    assert ipip.ipip_ttl == 64

    snapshot = store.load(plan.name)
    assert snapshot is not None
    assert snapshot.interfaces["r1:gre1"] == {
        "name": "gre1",
        "kind": "gre",
        "namespace": plan.nodes["r1"].namespace,
        "ifindex": gre.ifindex,
        "link": "underlay0",
        "local": "192.0.2.1",
        "remote": "192.0.2.2",
        "key": 100,
        "ttl": 64,
    }

    drifted_gre = replace(gre, gre_ttl=63)
    drifted_inventory = replace(
        inventory,
        namespaces={
            **inventory.namespaces,
            plan.nodes["r1"].namespace: replace(
                namespace,
                interfaces={**namespace.interfaces, "gre1": drifted_gre},
            ),
        },
    )
    assert not inventory_matches_plan(plan, drifted_inventory)
    report = inspect_topology(plan, snapshot, drifted_inventory)
    assert report.status == "degraded"
    assert any(
        difference.interface == "gre1" and difference.property == "gre_ttl"
        for difference in report.differences
    )
    assert service.destroy(plan, plan.name).status == "absent"


def test_pyroute2_configures_keyed_gre_and_ipip() -> None:
    plan = compile_plan(Manifest.model_validate(_tunnel_document()))
    node = plan.nodes["r1"]
    handle = Mock()
    indexes = {"underlay0": 10, "gre1": 11, "ipip0": 12}
    handle.link_lookup.side_effect = lambda *, ifname: [indexes[ifname]]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    backend.configure_node(node, plan)

    assert (
        call.link(
            "add",
            ifname="gre1",
            kind="gre",
            gre_link=10,
            gre_local="192.0.2.1",
            gre_remote="192.0.2.2",
            gre_ttl=64,
            gre_ikey=100,
            gre_okey=100,
            gre_iflags=0x2000,
            gre_oflags=0x2000,
        )
        in handle.mock_calls
    )
    assert (
        call.link(
            "add",
            ifname="ipip0",
            kind="ipip",
            ipip_link=10,
            ipip_local="192.0.2.1",
            ipip_remote="192.0.2.2",
            ipip_ttl=64,
        )
        in handle.mock_calls
    )
    assert call.link("set", index=11, mtu=1472, state="up") in handle.mock_calls
    assert call.link("set", index=12, mtu=1480, state="up") in handle.mock_calls
    assert call.addr("add", index=11, address="10.10.0.1", prefixlen=30) in handle.mock_calls
    assert call.addr("add", index=12, address="10.20.0.1", prefixlen=30) in handle.mock_calls


def _link_message(
    index: int,
    name: str,
    kind: str,
    *,
    mtu: int = 1500,
    info_data: list[tuple[str, object]] | None = None,
) -> dict[str, object]:
    link_info: list[tuple[str, object]] = [("IFLA_INFO_KIND", kind)]
    if info_data is not None:
        link_info.append(("IFLA_INFO_DATA", {"attrs": info_data}))
    return {
        "index": index,
        "flags": 1,
        "attrs": [
            ("IFLA_IFNAME", name),
            ("IFLA_MTU", mtu),
            ("IFLA_LINKINFO", {"attrs": link_info}),
        ],
    }


def test_pyroute2_inventory_decodes_gre_and_ipip() -> None:
    messages = (
        _link_message(10, "underlay0", "veth"),
        _link_message(
            11,
            "gre1",
            "gre",
            mtu=1472,
            info_data=[
                ("IFLA_GRE_LINK", 10),
                ("IFLA_GRE_IFLAGS", 0x2000),
                ("IFLA_GRE_OFLAGS", 0x2000),
                ("IFLA_GRE_IKEY", 100),
                ("IFLA_GRE_OKEY", 100),
                ("IFLA_GRE_LOCAL", "192.0.2.1"),
                ("IFLA_GRE_REMOTE", "192.0.2.2"),
                ("IFLA_GRE_TTL", 64),
            ],
        ),
        _link_message(
            12,
            "ipip0",
            "ipip",
            mtu=1480,
            info_data=[
                ("IFLA_IPIP_LINK", 10),
                ("IFLA_IPIP_LOCAL", "192.0.2.1"),
                ("IFLA_IPIP_REMOTE", "192.0.2.2"),
                ("IFLA_IPIP_TTL", 64),
            ],
        ),
    )

    interfaces, _ = Pyroute2Backend._inventory_interfaces(messages, ())
    gre = interfaces["gre1"]
    assert gre.gre_link == "underlay0"
    assert gre.gre_local == IPv4Address("192.0.2.1")
    assert gre.gre_remote == IPv4Address("192.0.2.2")
    assert gre.gre_key == 100
    assert gre.gre_ttl == 64
    ipip = interfaces["ipip0"]
    assert ipip.ipip_link == "underlay0"
    assert ipip.ipip_local == IPv4Address("192.0.2.1")
    assert ipip.ipip_remote == IPv4Address("192.0.2.2")
    assert ipip.ipip_ttl == 64


def test_pyroute2_inventory_ignores_kernel_tunnel_fallbacks() -> None:
    messages = (
        _link_message(1, "lo", "loopback", mtu=65_536),
        _link_message(2, "gre0", "gre"),
        _link_message(3, "gretap0", "gretap"),
        _link_message(4, "erspan0", "erspan"),
        _link_message(5, "tunl0", "ipip"),
        _link_message(6, "gre1", "gre"),
        _link_message(7, "ipip0", "ipip"),
    )

    interfaces, _ = Pyroute2Backend._inventory_interfaces(messages, ())
    assert set(interfaces) == {"lo", "gre1", "ipip0"}


def test_graph_formats_show_tunnel_summary_details_and_json() -> None:
    plan = compile_plan(Manifest.model_validate(_tunnel_document()))

    assert "gre1: gre -> 192.0.2.2" in render_graph(plan, "tree")
    assert "ipip0: ipip -> 192.0.2.2" in render_graph(plan, "tree")
    detail = render_graph(plan, "tree", detail=True)
    assert "local 192.0.2.1 via underlay0 · ttl 64 · key 100" in detail
    document = json.loads(render_graph(plan, "json"))
    r1 = next(node for node in document["nodes"] if node["name"] == "r1")
    assert r1["devices"] == [
        {
            "addresses": ["10.10.0.1/30"],
            "key": 100,
            "link": "underlay0",
            "local": "192.0.2.1",
            "mtu": None,
            "name": "gre1",
            "remote": "192.0.2.2",
            "ttl": 64,
            "type": "gre",
        },
        {
            "addresses": ["10.20.0.1/30"],
            "link": "underlay0",
            "local": "192.0.2.1",
            "mtu": None,
            "name": "ipip0",
            "remote": "192.0.2.2",
            "ttl": 64,
            "type": "ipip",
        },
    ]
