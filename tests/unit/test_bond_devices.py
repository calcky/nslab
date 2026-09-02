from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import replace
from ipaddress import IPv4Interface, IPv4Network
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
from nslab.manifest import (
    BondDeviceConfig,
    LinuxNode,
    Manifest,
    load_manifest,
    manifest_fingerprint,
)
from nslab.planner import BondDevicePlan, compile_plan, node_interface_master
from nslab.state import StateStore

_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def _bond_config(mode: str, address: str) -> dict[str, object]:
    config: dict[str, object] = {
        "type": "bond",
        "mode": mode,
        "interfaces": ["eth0", "eth1"],
        "addresses": [address],
        "miimon_ms": 100,
    }
    if mode == "active-backup":
        config["primary"] = "eth0"
    else:
        config.update(
            lacp_rate="fast",
            xmit_hash_policy="layer3+4",
            min_links=1,
        )
    return config


def _document(mode: str = "active-backup") -> dict[str, Any]:
    return {
        "version": 1,
        "name": "bond-test",
        "topology": {
            "nodes": {
                "h1": {
                    "kind": "linux",
                    "interfaces": {"eth0": {}, "eth1": {}},
                    "devices": {
                        "bond0": _bond_config(mode, "10.60.0.1/24"),
                    },
                },
                "h2": {
                    "kind": "linux",
                    "interfaces": {"eth0": {}, "eth1": {}},
                    "devices": {
                        "bond0": _bond_config(mode, "10.60.0.2/24"),
                    },
                },
            },
            "links": [
                {"endpoints": ["h1:eth0", "h2:eth0"]},
                {"endpoints": ["h1:eth1", "h2:eth1"]},
            ],
        },
    }


def _manifest(mode: str = "active-backup") -> Manifest:
    return Manifest.model_validate(_document(mode))


def _h1(document: dict[str, Any]) -> dict[str, Any]:
    return document["topology"]["nodes"]["h1"]


@pytest.mark.parametrize(
    ("example", "mode"),
    [
        ("bond-active-backup", "active-backup"),
        ("bond-8023ad", "802.3ad"),
    ],
)
def test_bond_examples_compile(example: str, mode: str) -> None:
    plan = compile_plan(load_manifest(_EXAMPLES / example / "nslab.yaml"))

    assert plan.name == example
    for node in plan.nodes.values():
        bond = node.devices["bond0"]
        assert isinstance(bond, BondDevicePlan)
        assert bond.mode == mode
        assert bond.interfaces == ("eth0", "eth1")


def test_manifest_and_plan_preserve_active_backup_configuration() -> None:
    manifest = _manifest()
    node = manifest.topology.nodes["h1"]

    assert isinstance(node, LinuxNode)
    config = node.devices["bond0"]
    assert isinstance(config, BondDeviceConfig)
    assert config.mode == "active-backup"
    assert config.interfaces == ("eth0", "eth1")
    assert config.addresses == (IPv4Interface("10.60.0.1/24"),)
    assert config.miimon_ms == 100
    assert config.primary == "eth0"

    bond = compile_plan(manifest).nodes["h1"].devices["bond0"]
    assert bond == BondDevicePlan(
        name="bond0",
        mode="active-backup",
        interfaces=("eth0", "eth1"),
        addresses=(IPv4Interface("10.60.0.1/24"),),
        miimon_ms=100,
        primary="eth0",
    )


def test_manifest_and_plan_apply_lacp_defaults() -> None:
    document = _document("802.3ad")
    config = _h1(document)["devices"]["bond0"]
    config.pop("lacp_rate")
    config.pop("xmit_hash_policy")
    config.pop("min_links")

    bond = compile_plan(Manifest.model_validate(document)).nodes["h1"].devices["bond0"]

    assert isinstance(bond, BondDevicePlan)
    assert bond.lacp_rate == "slow"
    assert bond.xmit_hash_policy == "layer2"
    assert bond.min_links == 0


def test_lacp_omitted_and_explicit_defaults_have_the_same_fingerprint() -> None:
    omitted = _document("802.3ad")
    explicit = copy.deepcopy(omitted)
    for document in (omitted, explicit):
        bond = _h1(document)["devices"]["bond0"]
        bond.pop("lacp_rate")
        bond.pop("xmit_hash_policy")
        bond.pop("min_links")
    explicit_bond = _h1(explicit)["devices"]["bond0"]
    explicit_bond.update(lacp_rate="slow", xmit_hash_policy="layer2", min_links=0)

    assert manifest_fingerprint(Manifest.model_validate(omitted)) == manifest_fingerprint(
        Manifest.model_validate(explicit)
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda node: node["devices"]["bond0"].update(interfaces=["eth0"]),
            "at least 2 items",
        ),
        (
            lambda node: node["devices"]["bond0"].update(interfaces=["eth0", "eth0"]),
            "must be unique",
        ),
        (
            lambda node: node["devices"]["bond0"].update(primary="missing0"),
            "must be one of its member interfaces",
        ),
        (
            lambda node: node["devices"]["bond0"].update(lacp_rate="fast"),
            "require 802.3ad mode",
        ),
        (
            lambda node: node["devices"]["bond0"].update(miimon_ms=-1),
            "greater than or equal",
        ),
        (
            lambda node: node["interfaces"]["eth0"].update(addresses=["192.0.2.1/24"]),
            "cannot declare addresses",
        ),
        (
            lambda node: node["devices"].update(
                bond1={
                    "type": "bond",
                    "mode": "active-backup",
                    "interfaces": ["eth0", "eth1"],
                }
            ),
            "more than one bond",
        ),
        (
            lambda node: node["devices"].update(
                blue={"type": "vrf", "table": 1001, "interfaces": ["eth0"]}
            ),
            "cannot belong directly to a VRF",
        ),
    ],
)
def test_manifest_rejects_invalid_active_backup_configuration(
    mutation: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    document = _document()
    mutation(_h1(document))

    with pytest.raises(ValidationError, match=message):
        Manifest.model_validate(document)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda bond: bond.update(primary="eth0"),
            "only valid in active-backup mode",
        ),
        (
            lambda bond: bond.update(min_links=3),
            "cannot exceed the number of member interfaces",
        ),
        (
            lambda bond: bond.update(lacp_rate="quick"),
            "Input should be 'slow' or 'fast'",
        ),
        (
            lambda bond: bond.update(xmit_hash_policy="round-robin"),
            "Input should be 'layer2', 'layer2\\+3' or 'layer3\\+4'",
        ),
    ],
)
def test_manifest_rejects_invalid_lacp_configuration(
    mutation: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    document = _document("802.3ad")
    mutation(_h1(document)["devices"]["bond0"])

    with pytest.raises(ValidationError, match=message):
        Manifest.model_validate(document)


def test_manifest_rejects_unlinked_bond_member() -> None:
    document = _document()
    _h1(document)["devices"]["bond0"]["interfaces"] = ["eth0", "eth2"]

    with pytest.raises(ValidationError, match="bond member interface is not linked"):
        Manifest.model_validate(document)


def test_manifest_rejects_different_member_link_mtus() -> None:
    document = _document()
    document["topology"]["links"][1]["mtu"] = 9000

    with pytest.raises(ValidationError, match="must use the same MTU"):
        Manifest.model_validate(document)


def test_routes_and_policy_rules_may_reference_a_bond() -> None:
    document = _document()
    node = _h1(document)
    node["routes"] = [{"dst": "203.0.113.0/24", "via": "10.60.0.2", "dev": "bond0"}]
    node["rules"] = [{"priority": 100, "iif": "bond0", "table": 254}]

    plan = compile_plan(Manifest.model_validate(document))

    assert plan.nodes["h1"].routes[0].dev == "bond0"
    assert plan.nodes["h1"].rules[0].iif == "bond0"


def test_fake_backend_snapshot_inventory_and_drift_include_bond_state(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    plan = compile_plan(manifest)
    backend = FakeNetworkBackend()
    store = StateStore(tmp_path)
    service = LifecycleService(backend, store)

    result = service.deploy(plan, manifest)

    assert result.status == "deployed"
    inventory = backend.inventory(plan)
    assert inventory_matches_plan(plan, inventory)
    namespace = inventory.namespaces[plan.nodes["h1"].namespace]
    bond = namespace.interfaces["bond0"]
    assert bond.bond_mode == "active-backup"
    assert bond.bond_primary == "eth0"
    assert bond.bond_miimon_ms == 100
    assert namespace.interfaces["eth0"].master == "bond0"
    assert namespace.interfaces["eth1"].master == "bond0"
    assert node_interface_master(plan.nodes["h1"], "eth0") == "bond0"
    assert tuple((route.dst, route.dev) for route in expected_routes(plan.nodes["h1"])) == (
        (IPv4Network("127.0.0.0/8"), "lo"),
        (IPv4Network("10.60.0.0/24"), "bond0"),
    )

    snapshot = store.load(plan.name)
    assert snapshot is not None
    assert snapshot.interfaces["h1:bond0"] == {
        "name": "bond0",
        "kind": "bond",
        "namespace": plan.nodes["h1"].namespace,
        "bond_mode": "active-backup",
        "interfaces": ("eth0", "eth1"),
        "ifindex": bond.ifindex,
    }

    state = backend.namespaces[plan.nodes["h1"].namespace]
    state.interfaces["bond0"] = replace(bond, bond_primary="eth1")
    drifted = backend.inventory(plan)
    assert not inventory_matches_plan(plan, drifted)
    report = inspect_topology(plan, snapshot, drifted)
    assert report.status == "degraded"
    assert any(
        difference.node == "h1"
        and difference.interface == "bond0"
        and difference.property == "bond_primary"
        and difference.desired == "eth0"
        and difference.actual == "eth1"
        for difference in report.differences
    )


def test_pyroute2_configure_node_creates_active_backup_and_enslaves_members() -> None:
    plan = compile_plan(_manifest())
    node = plan.nodes["h1"]
    handle = Mock()
    indexes = {"eth0": 10, "eth1": 11, "bond0": 12}
    handle.link_lookup.side_effect = lambda *, ifname: [indexes[ifname]]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    backend.configure_node(node, plan)

    assert handle.mock_calls == [
        call.link_lookup(ifname="eth0"),
        call.link_lookup(ifname="eth1"),
        call.link(
            "add",
            ifname="bond0",
            kind="bond",
            bond_mode=1,
            bond_miimon=100,
        ),
        call.link_lookup(ifname="bond0"),
        call.link("set", index=12, mtu=1500),
        call.link("set", index=10, state="down"),
        call.link("set", index=10, master=12),
        call.link("set", index=10, state="up"),
        call.link("set", index=11, state="down"),
        call.link("set", index=11, master=12),
        call.link("set", index=11, state="up"),
        call.link("set", index=12, kind="bond", bond_primary=10),
        call.link("set", index=10, state="up"),
        call.link("set", index=11, state="up"),
        call.addr("add", index=12, address="10.60.0.1", prefixlen=24),
        call.link("set", index=12, state="up"),
        call.close(),
    ]


def test_pyroute2_configure_node_sets_lacp_options() -> None:
    plan = compile_plan(_manifest("802.3ad"))
    node = plan.nodes["h1"]
    handle = Mock()
    indexes = {"eth0": 10, "eth1": 11, "bond0": 12}
    handle.link_lookup.side_effect = lambda *, ifname: [indexes[ifname]]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    backend.configure_node(node, plan)

    assert (
        call.link(
            "add",
            ifname="bond0",
            kind="bond",
            bond_mode=4,
            bond_miimon=100,
            bond_ad_lacp_rate=1,
            bond_xmit_hash_policy=1,
            bond_min_links=1,
        )
        in handle.mock_calls
    )
    assert call.link("set", index=10, master=12) in handle.mock_calls
    assert call.link("set", index=11, master=12) in handle.mock_calls


def _link_message(
    index: int,
    name: str,
    kind: str,
    *,
    master: int | None = None,
    bond_data: list[tuple[str, object]] | None = None,
) -> dict[str, object]:
    link_info: list[tuple[str, object]] = [("IFLA_INFO_KIND", kind)]
    if bond_data is not None:
        link_info.append(("IFLA_INFO_DATA", {"attrs": bond_data}))
    attributes: list[tuple[str, object]] = [
        ("IFLA_IFNAME", name),
        ("IFLA_MTU", 1500),
        ("IFLA_LINKINFO", {"attrs": link_info}),
    ]
    if master is not None:
        attributes.append(("IFLA_MASTER", master))
    return {"index": index, "flags": 1, "attrs": attributes}


def test_pyroute2_inventory_decodes_active_backup_properties() -> None:
    interfaces, _ = Pyroute2Backend._inventory_interfaces(
        (
            _link_message(10, "eth0", "veth", master=12),
            _link_message(11, "eth1", "veth", master=12),
            _link_message(
                12,
                "bond0",
                "bond",
                bond_data=[
                    ("IFLA_BOND_MODE", 1),
                    ("IFLA_BOND_MIIMON", 100),
                    ("IFLA_BOND_PRIMARY", 10),
                    ("IFLA_BOND_XMIT_HASH_POLICY", 0),
                ],
            ),
        ),
        (),
    )

    bond = interfaces["bond0"]
    assert bond.bond_mode == "active-backup"
    assert bond.bond_miimon_ms == 100
    assert bond.bond_primary == "eth0"
    assert bond.bond_lacp_rate is None
    assert bond.bond_xmit_hash_policy is None
    assert interfaces["eth0"].master == "bond0"
    assert interfaces["eth1"].master == "bond0"


def test_pyroute2_inventory_decodes_lacp_properties() -> None:
    interfaces, _ = Pyroute2Backend._inventory_interfaces(
        (
            _link_message(10, "eth0", "veth", master=12),
            _link_message(11, "eth1", "veth", master=12),
            _link_message(
                12,
                "bond0",
                "bond",
                bond_data=[
                    ("IFLA_BOND_MODE", 4),
                    ("IFLA_BOND_MIIMON", 100),
                    ("IFLA_BOND_AD_LACP_RATE", 1),
                    ("IFLA_BOND_XMIT_HASH_POLICY", 1),
                    ("IFLA_BOND_MIN_LINKS", 1),
                ],
            ),
        ),
        (),
    )

    bond = interfaces["bond0"]
    assert bond.bond_mode == "802.3ad"
    assert bond.bond_miimon_ms == 100
    assert bond.bond_primary is None
    assert bond.bond_lacp_rate == "fast"
    assert bond.bond_xmit_hash_policy == "layer3+4"
    assert bond.bond_min_links == 1


@pytest.mark.parametrize("mode", ["active-backup", "802.3ad"])
def test_graph_formats_show_bond_mode_members_and_options(mode: str) -> None:
    plan = compile_plan(_manifest(mode))

    tree = render_graph(plan, "tree")
    detailed = render_graph(plan, "tree", detail=True)
    mermaid = render_graph(plan, "mermaid")
    graph_json = json.loads(render_graph(plan, "json"))

    summary = f"bond0: bond {mode} · members eth0, eth1"
    assert summary in tree
    assert summary in mermaid
    assert "miimon 100ms" in detailed
    h1 = next(node for node in graph_json["nodes"] if node["name"] == "h1")
    bond = h1["devices"][0]
    assert bond["type"] == "bond"
    assert bond["mode"] == mode
    assert bond["interfaces"] == ["eth0", "eth1"]
