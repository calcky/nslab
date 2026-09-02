from __future__ import annotations

import json
import socket
from collections.abc import Callable
from dataclasses import replace
from ipaddress import IPv4Network
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call

import pytest
from pydantic import ValidationError

from nslab.backend.base import inventory_matches_plan
from nslab.backend.fake import FakeNetworkBackend
from nslab.backend.pyroute2 import Pyroute2Backend
from nslab.inspector import inspect_topology
from nslab.lifecycle import LifecycleService
from nslab.manifest import LinuxNode, Manifest, load_manifest, normalized_manifest
from nslab.planner import PolicyRulePlan, compile_plan, node_route_tables
from nslab.state import StateStore

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "policy-routing" / "nslab.yaml"


def _document() -> dict[str, Any]:
    return {
        "version": 1,
        "name": "policy-routing",
        "topology": {
            "nodes": {
                "h1": {
                    "kind": "linux",
                    "interfaces": {"eth0": {"addresses": ["192.0.2.2/24"]}},
                },
                "r1": {
                    "kind": "linux",
                    "interfaces": {
                        "eth0": {"addresses": ["192.0.2.1/24"]},
                        "eth1": {"addresses": ["10.0.0.1/30"]},
                    },
                    "routes": [
                        {
                            "dst": "203.0.113.2/32",
                            "via": "10.0.0.2",
                            "dev": "eth1",
                            "table": 100,
                        },
                        {
                            "dst": "203.0.113.2/32",
                            "via": "10.0.0.2",
                            "dev": "eth1",
                            "table": 200,
                        },
                    ],
                    "rules": [
                        {
                            "priority": 100,
                            "family": "ipv4",
                            "action": "lookup",
                            "table": 100,
                            "from": "192.0.2.0/24",
                            "to": "203.0.113.0/24",
                            "not": True,
                            "tos": 16,
                            "fwmark": 1,
                            "fwmask": 255,
                            "iif": "eth0",
                            "oif": "eth1",
                            "uid_range": {"start": 0, "end": 0},
                            "protocol": 99,
                            "ip_protocol": 6,
                            "source_port": {"start": 1000, "end": 2000},
                            "destination_port": {"start": 80, "end": 443},
                            "tunnel_id": 123,
                            "suppress_prefix_length": 24,
                            "suppress_interface_group": 7,
                            "realms": {"source": 1, "destination": 2},
                        },
                        {
                            "priority": 200,
                            "family": "ipv4",
                            "action": "goto",
                            "goto": 300,
                            "fwmark": 2,
                        },
                        {
                            "priority": 300,
                            "family": "ipv4",
                            "table": 200,
                        },
                        {
                            "priority": 400,
                            "family": "ipv4",
                            "action": "blackhole",
                            "from": "198.51.100.0/24",
                        },
                        {
                            "priority": 100,
                            "family": "ipv6",
                            "action": "prohibit",
                            "to": "2001:db8::/32",
                        },
                    ],
                },
                "h2": {
                    "kind": "linux",
                    "interfaces": {"eth0": {"addresses": ["10.0.0.2/30", "203.0.113.2/32"]}},
                },
            },
            "links": [
                {"endpoints": ["h1:eth0", "r1:eth0"]},
                {"endpoints": ["r1:eth1", "h2:eth0"]},
            ],
        },
    }


def _manifest() -> Manifest:
    return Manifest.model_validate(_document())


def _r1(document: dict[str, Any]) -> dict[str, Any]:
    return document["topology"]["nodes"]["r1"]


def test_policy_routing_example_manifest_compiles() -> None:
    plan = compile_plan(load_manifest(_EXAMPLE))

    assert plan.name == "policy-routing"
    assert tuple((rule.priority, rule.table) for rule in plan.nodes["r1"].rules) == (
        (90, 200),
        (100, 100),
    )


def _rule_message(
    *,
    priority: int,
    action: int = 1,
    table: int = 0,
    family: int = socket.AF_INET,
    src_len: int = 0,
    dst_len: int = 0,
    tos: int = 0,
    flags: int = 0,
    attrs: tuple[tuple[str, object], ...] = (),
    protocol: int = 0,
) -> dict[str, object]:
    attribute_names = {name for name, _ in attrs}
    default_attrs = (
        ("FRA_TABLE", table),
        ("FRA_SUPPRESS_PREFIXLEN", 4_294_967_295),
        ("FRA_PROTOCOL", protocol),
        ("FRA_PRIORITY", priority),
    )
    return {
        "family": family,
        "src_len": src_len,
        "dst_len": dst_len,
        "tos": tos,
        "table": table,
        "action": action,
        "flags": flags,
        "attrs": [
            *(item for item in default_attrs if item[0] not in attribute_names),
            *attrs,
        ],
    }


def test_manifest_and_plan_preserve_complete_policy_rule_configuration() -> None:
    manifest = _manifest()
    r1_manifest = manifest.topology.nodes["r1"]

    assert isinstance(r1_manifest, LinuxNode)
    assert r1_manifest.rules[0].ip_version == 4
    assert r1_manifest.rules[-1].ip_version == 6

    plan = compile_plan(manifest)
    r1 = plan.nodes["r1"]
    complex_rule = r1.rules[0]

    assert complex_rule == PolicyRulePlan(
        priority=100,
        family=4,
        action="lookup",
        table=100,
        source=IPv4Network("192.0.2.0/24"),
        destination=IPv4Network("203.0.113.0/24"),
        invert=True,
        tos=16,
        fwmark=1,
        fwmask=255,
        iif="eth0",
        oif="eth1",
        uid_range=(0, 0),
        protocol=99,
        ip_protocol=6,
        source_port=(1000, 2000),
        destination_port=(80, 443),
        tunnel_id=123,
        suppress_prefix_length=24,
        suppress_interface_group=7,
        realms=(1, 2),
    )
    assert tuple(route.table for route in r1.routes) == (100, 200)
    assert node_route_tables(r1) == (254, 100, 200)
    assert r1.rules[1].fwmask == 4_294_967_295

    normalized_rule = normalized_manifest(manifest)["topology"]["nodes"]["r1"]["rules"][0]
    assert normalized_rule["from"] == "192.0.2.0/24"
    assert normalized_rule["to"] == "203.0.113.0/24"
    assert normalized_rule["not"] is True
    assert "source" not in normalized_rule
    assert "destination" not in normalized_rule
    assert "invert" not in normalized_rule


def test_policy_rules_allow_uint32_priorities_local_table_and_unresolved_goto() -> None:
    document = _document()
    _r1(document)["rules"] = [
        {"priority": 1000, "family": "ipv4", "l3mdev": True},
        {
            "priority": 32766,
            "family": "ipv4",
            "table": 255,
            "tos": 0,
            "ip_protocol": 0,
            "tunnel_id": 0,
        },
        {
            "priority": 4_294_967_294,
            "family": "ipv4",
            "action": "goto",
            "goto": 4_294_967_295,
        },
    ]

    rules = compile_plan(Manifest.model_validate(document)).nodes["r1"].rules

    assert rules[0].l3mdev is True
    assert rules[1].table == 255
    assert rules[1].tos is None
    assert rules[1].ip_protocol is None
    assert rules[1].tunnel_id is None
    assert rules[2].goto == 4_294_967_295


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda node: node["rules"][0].pop("table"), "requires table"),
        (lambda node: node["rules"][0].update(fwmark=None), "fwmask requires fwmark"),
        (lambda node: node["rules"][0].pop("ip_protocol"), "require a nonzero ip_protocol"),
        (
            lambda node: node["rules"][0].update(ip_protocol=0),
            "require a nonzero ip_protocol",
        ),
        (lambda node: node["rules"][0].update(family="ipv6"), "does not match IPv6"),
        (lambda node: node["rules"][0].update(iif="missing0"), "not available"),
        (
            lambda node: node["rules"][1].update(priority=100),
            "duplicate policy rule priority",
        ),
        (lambda node: node["rules"][1].update(goto=150), "greater priority"),
        (lambda node: node["rules"][3].update(table=100), "cannot declare table"),
        (
            lambda node: node["rules"][0].update(suppress_prefix_length=33),
            "must be at most 32",
        ),
        (
            lambda node: node["rules"][0].update(suppress_interface_group=4_294_967_295),
            "less than or equal",
        ),
    ],
)
def test_manifest_rejects_invalid_policy_rule_configuration(
    mutation: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    document = _document()
    mutation(_r1(document))

    with pytest.raises(ValidationError, match=message):
        Manifest.model_validate(document)


def test_pyroute2_configure_node_maps_every_policy_rule_field() -> None:
    node = compile_plan(_manifest()).nodes["r1"]
    handle = Mock()
    handle.link_lookup.side_effect = lambda *, ifname: [{"eth0": 10, "eth1": 11}[ifname]]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    backend.configure_node(node, compile_plan(_manifest()))

    assert (
        call.rule(
            "add",
            family=socket.AF_INET,
            priority=100,
            action="to_tbl",
            table=100,
            src="192.0.2.0",
            src_len=24,
            dst="203.0.113.0",
            dst_len=24,
            flags=2,
            tos=16,
            fwmark=1,
            fwmask=255,
            iifname="eth0",
            oifname="eth1",
            ip_proto=6,
            tun_id=123,
            suppress_prefixlen=24,
            suppress_ifgroup=7,
            uid_range="0:0",
            protocol=99,
            sport_range="1000:2000",
            dport_range="80:443",
            flow=65538,
        )
        in handle.mock_calls
    )
    target_call = call.rule("add", family=socket.AF_INET, priority=300, action="to_tbl", table=200)
    goto_call = call.rule(
        "add",
        family=socket.AF_INET,
        priority=200,
        action="goto",
        goto=300,
        fwmark=2,
        fwmask=4_294_967_295,
    )
    assert handle.mock_calls.index(target_call) < handle.mock_calls.index(goto_call)


def test_inventory_decodes_complete_rule_and_ignores_kernel_rules() -> None:
    complete = _rule_message(
        priority=100,
        table=100,
        src_len=24,
        dst_len=24,
        tos=16,
        flags=2,
        protocol=99,
        attrs=(
            ("FRA_SRC", "192.0.2.0"),
            ("FRA_DST", "203.0.113.0"),
            ("FRA_FWMARK", 1),
            ("FRA_FWMASK", 255),
            ("FRA_IIFNAME", "eth0"),
            ("FRA_OIFNAME", "eth1"),
            ("FRA_UID_RANGE", "0:0"),
            ("FRA_IP_PROTO", 6),
            ("FRA_SPORT_RANGE", "1000:2000"),
            ("FRA_DPORT_RANGE", "80:443"),
            ("FRA_TUN_ID", 123),
            ("FRA_SUPPRESS_PREFIXLEN", 24),
            ("FRA_SUPPRESS_IFGROUP", 7),
            ("FRA_FLOW", 65538),
        ),
    )
    local = _rule_message(priority=0, table=255, protocol=2)
    main = _rule_message(priority=32766, table=254, protocol=2)
    default = _rule_message(priority=32767, table=253, protocol=2)
    l3mdev = _rule_message(
        priority=1000,
        table=0,
        protocol=2,
        attrs=(("FRA_L3MDEV", 1),),
    )

    rules = Pyroute2Backend._inventory_rules(
        (local, complete, l3mdev, main, default),
        "policy-namespace",
        ignore_l3mdev_kernel_rule=True,
    )

    assert rules == (compile_plan(_manifest()).nodes["r1"].rules[0],)


def test_inventory_preserves_user_rules_at_kernel_priorities() -> None:
    custom_main = _rule_message(
        priority=32766,
        table=254,
        src_len=24,
        protocol=2,
        attrs=(("FRA_SRC", "192.0.2.0"),),
    )
    user_l3mdev = _rule_message(
        priority=1000,
        table=0,
        attrs=(("FRA_L3MDEV", 1),),
    )

    rules = Pyroute2Backend._inventory_rules(
        (custom_main, user_l3mdev),
        "policy-namespace",
    )

    assert tuple(rule.priority for rule in rules) == (32766, 1000)
    assert rules[0].source == IPv4Network("192.0.2.0/24")
    assert rules[1].l3mdev is True


def test_inventory_only_filters_the_exact_vrf_kernel_rule() -> None:
    manual_l3mdev = _rule_message(
        priority=1000,
        table=0,
        protocol=2,
        attrs=(("FRA_L3MDEV", 1), ("FRA_FWMARK", 7)),
    )

    rules = Pyroute2Backend._inventory_rules(
        (manual_l3mdev,),
        "policy-namespace",
        ignore_l3mdev_kernel_rule=True,
    )

    assert len(rules) == 1
    assert rules[0].fwmark == 7


@pytest.mark.parametrize(
    ("action", "number", "extra", "expected"),
    [
        ("goto", 2, (("FRA_GOTO", 300),), {"goto": 300}),
        ("nop", 3, (), {}),
        ("blackhole", 6, (), {}),
        ("unreachable", 7, (), {}),
        ("prohibit", 8, (), {}),
    ],
)
def test_inventory_decodes_policy_rule_actions(
    action: str,
    number: int,
    extra: tuple[tuple[str, object], ...],
    expected: dict[str, int],
) -> None:
    message = _rule_message(priority=200, action=number, attrs=extra)

    rule = Pyroute2Backend._inventory_rules((message,), "policy-namespace")[0]

    assert rule.action == action
    assert rule.table is None
    assert rule.goto == expected.get("goto")


def test_fake_backend_and_inspector_include_policy_rule_drift(tmp_path: Path) -> None:
    manifest = _manifest()
    plan = compile_plan(manifest)
    backend = FakeNetworkBackend()
    store = StateStore(tmp_path)
    service = LifecycleService(backend, store)

    service.deploy(plan, manifest)
    inventory = backend.inventory(plan)
    assert inventory_matches_plan(plan, inventory)

    snapshot = store.load(plan.name)
    assert snapshot is not None
    report = inspect_topology(plan, snapshot, inventory)
    r1 = next(node for node in report.nodes if node.name == "r1")
    assert r1.desired.rules[0].to_dict()["fwmark"] == 1
    assert r1.desired.rules[0].to_dict()["source_port"] == {
        "start": 1000,
        "end": 2000,
    }
    assert r1.actual.rules == r1.desired.rules
    assert json.loads(json.dumps(report.to_dict()))["nodes"][1]["desired"]["rules"]

    state = backend.namespaces[plan.nodes["r1"].namespace]
    state.rules = (replace(state.rules[0], fwmark=99), *state.rules[1:])
    drifted = backend.inventory(plan)
    assert not inventory_matches_plan(plan, drifted)
    degraded = inspect_topology(plan, snapshot, drifted)
    assert degraded.status == "degraded"
    assert any(
        difference.node == "r1" and difference.property == "rules"
        for difference in degraded.differences
    )
