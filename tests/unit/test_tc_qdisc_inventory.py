from __future__ import annotations

from dataclasses import replace
from ipaddress import IPv4Interface
from unittest.mock import Mock

import pytest

from nslab.backend.base import ExecResult, _qdisc_matches
from nslab.backend.pyroute2 import Pyroute2Backend, _tc_qdisc_arguments
from nslab.errors import NslabError
from nslab.planner import FqCodelPlan, HtbPlan, QdiscPlan, SimpleQdiscPlan

KINDS = ("pfifo", "bfifo", "pfifo_fast", "prio", "sfq", "fq", "codel", "red", "pie", "fq_pie")


def messages(kind: str, *, htb: bool) -> tuple[list[dict], list[dict]]:
    leaf = {
        "index": 10,
        "handle": 0x100000 if htb else 0x10000,
        "parent": 0x10001 if htb else 0xFFFFFFFF,
        "attrs": [("TCA_KIND", kind), ("TCA_OPTIONS", b"opaque options")],
    }
    if not htb:
        return [leaf], []
    root = {
        "index": 10,
        "handle": 0x10000,
        "parent": 0xFFFFFFFF,
        "attrs": [
            ("TCA_KIND", "htb"),
            (
                "TCA_OPTIONS",
                {
                    "attrs": [("TCA_HTB_INIT", {"defcls": 1, "rate2quantum": 10})],
                },
            ),
        ],
    }
    classes = [
        {
            "index": 10,
            "handle": 0x10001,
            "parent": 0xFFFFFFFF,
            "attrs": [
                ("TCA_KIND", "htb"),
                (
                    "TCA_OPTIONS",
                    {
                        "attrs": [("TCA_HTB_PARMS", {"rate": 12500000, "ceil": 12500000})],
                    },
                ),
            ],
        }
    ]
    return [root, leaf], classes


def inventory(kind: str, options: dict, *, htb: bool = False, tc_rows: list | None = None):
    qdiscs, classes = messages(kind, htb=htb)
    item = {"kind": kind, "handle": "10:" if htb else "1:", "options": options}
    item.update({"parent": "1:1"} if htb else {"root": True})
    links = [
        {
            "index": 10,
            "flags": 1,
            "attrs": [
                ("IFLA_IFNAME", "eth0"),
                ("IFLA_MTU", 1500),
                ("IFLA_LINKINFO", {"attrs": [("IFLA_INFO_KIND", "veth")]}),
            ],
        }
    ]
    interfaces, _ = Pyroute2Backend._inventory_interfaces(
        links,
        [],
        (),
        qdiscs,
        classes,
        tc_qdiscs={"eth0": [item] if tc_rows is None else tc_rows},
    )
    return interfaces["eth0"].qdisc


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("htb", [False, True])
def test_inventory_decodes_new_root_and_htb_leaf(kind: str, htb: bool) -> None:
    observed = inventory(kind, {"limit": 1000}, htb=htb)
    leaf = observed.leaf if htb else observed
    assert isinstance(leaf, SimpleQdiscPlan)
    assert leaf.kind == kind
    assert leaf.options["limit"] == 1000
    if htb:
        assert observed.rate == "100mbit"


def test_inventory_retains_actual_values_not_declared_values() -> None:
    desired = SimpleQdiscPlan("pfifo", {"limit": 1000})
    observed = inventory("pfifo", {"limit": 2000})
    assert observed.options["limit"] == 2000
    assert not _qdisc_matches(desired, observed)
    assert not _qdisc_matches(HtbPlan("100mbit", desired), HtbPlan("100mbit", observed))


def test_matches_declared_subset_without_discarding_kernel_defaults() -> None:
    actual = inventory("fq", {"limit": 1000, "quantum": 1514, "flow_limit": 100})
    assert actual.options["flow_limit"] == 100
    assert _qdisc_matches(SimpleQdiscPlan("fq", {"limit": 1000}), actual)
    assert not _qdisc_matches(SimpleQdiscPlan("pfifo", {"limit": 1000}), actual)
    assert not _qdisc_matches(HtbPlan("90mbit", actual), HtbPlan("100mbit", actual))


def test_codel_time_quantization_and_missing_boolean_flags() -> None:
    actual = inventory("codel", {"limit": 1000, "target": 4999, "interval": 99999})
    assert actual.options["target_ms"] == 5
    assert actual.options["interval_ms"] == 100
    assert actual.options["ecn"] is False
    assert _qdisc_matches(SimpleQdiscPlan("codel", {"target_ms": 5, "ecn": False}), actual)
    assert not _qdisc_matches(SimpleQdiscPlan("codel", {"target_ms": 6}), actual)


@pytest.mark.parametrize("kind", ["pie", "fq_pie"])
def test_explicit_false_flags_are_sent_and_round_trip(kind: str) -> None:
    desired = SimpleQdiscPlan(kind, {"ecn": False, "bytemode": False})
    assert _tc_qdisc_arguments(desired) == [kind, "noecn", "nobytemode"]
    assert _qdisc_matches(desired, inventory(kind, {"target": 5000, "tupdate": 15000}))
    assert not _qdisc_matches(desired, inventory(kind, {"ecn": True}))


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"kind": "sfq", "handle": "1:", "root": True, "options": {}}],
        [{"kind": "pfifo", "handle": "2:", "root": True, "options": {}}],
        [{"kind": "pfifo", "handle": "1:", "parent": "2:1", "options": {}}],
    ],
)
def test_missing_or_wrong_tc_identity_is_rejected(rows: list) -> None:
    with pytest.raises(NslabError) as caught:
        inventory("pfifo", {}, tc_rows=rows)
    assert caught.value.code == "INVENTORY_UNSUPPORTED"


def test_duplicate_tc_identity_is_rejected() -> None:
    item = {"kind": "pfifo", "root": True, "handle": "1:", "options": {"limit": 1000}}
    with pytest.raises(NslabError):
        inventory("pfifo", {}, tc_rows=[item, item])


@pytest.mark.parametrize(
    "options",
    [
        None,
        "limit 1000",
        {"target": "bad"},
        {"limit": True},
        {"limit": -1},
        {"ecn": "false"},
        {"target": 4900},
        {"target": 0},
        {"target": 5000.1},
        {"probability": float("nan")},
        {"probability": float("inf")},
        {"probability": True},
    ],
)
def test_malformed_options_fail_closed(options: object) -> None:
    with pytest.raises(NslabError):
        inventory("codel", options)


@pytest.mark.parametrize("output", ["not json", "{}", "[1]", "[null]"])
def test_invalid_tc_json_is_rejected(output: str) -> None:
    backend = Pyroute2Backend()
    backend.execute = Mock(return_value=ExecResult((), 0, output, ""))
    with pytest.raises(NslabError):
        backend._read_tc_qdiscs("owned-namespace", "eth0")
    backend.execute.assert_called_once_with(
        "owned-namespace",
        ("tc", "-j", "-d", "qdisc", "show", "dev", "eth0"),
    )


def test_tc_failure_is_not_an_empty_inventory() -> None:
    backend = Pyroute2Backend()
    backend.execute = Mock(return_value=ExecResult((), 2, "", "invalid option -j"))
    with pytest.raises(NslabError):
        backend._read_tc_qdiscs("owned-namespace", "eth0")


def test_red_compares_derived_options_not_input_echo() -> None:
    from pyroute2.netlink.rtnl.tcmsg.common import red_eval_idle_damping

    desired = SimpleQdiscPlan(
        "red",
        {
            "limit": 1514000,
            "min": 125000,
            "max": 375000,
            "avpkt": 1500,
            "burst": 139,
            "probability": 0.02,
            "ecn": False,
        },
    )
    actual = inventory(
        "red",
        {
            "limit": 1514000,
            "min": 125000,
            "max": 375000,
            "ewma": 6,
            "Scell_log": red_eval_idle_damping(6, 1500, 1250000)[0],
            "probability": 0.02,
            "ecn": False,
            "harddrop": False,
            "adaptive": False,
            "nodrop": False,
        },
    )
    assert "avpkt" not in actual.options
    assert "burst" not in actual.options
    assert _qdisc_matches(desired, actual)
    for key, value in [
        ("ewma", 7),
        ("Scell_log", -1),
        ("probability", 0.1),
        ("ecn", True),
        ("harddrop", True),
        ("adaptive", True),
        ("nodrop", True),
        ("limit", 1000),
    ]:
        assert not _qdisc_matches(desired, replace(actual, options={**actual.options, key: value}))
    assert not _qdisc_matches(replace(desired, options={**desired.options, "burst": 200}), actual)
    assert not _qdisc_matches(replace(desired, options={**desired.options, "avpkt": 500}), actual)
    rounded = replace(actual, options={**actual.options, "probability": 0.0123457})
    assert _qdisc_matches(
        replace(desired, options={**desired.options, "probability": 0.01234567}), rounded
    )


@pytest.mark.parametrize(
    "patch", [{"kind": "sfq"}, {"handle": "11:"}, {"parent": "1:2"}, {"parent": None, "root": True}]
)
def test_htb_tc_leaf_identity_drift_is_rejected(patch: dict) -> None:
    record = {"kind": "pfifo", "handle": "10:", "parent": "1:1", "options": {"limit": 1000}}
    with pytest.raises(NslabError):
        inventory("pfifo", {}, htb=True, tc_rows=[{**record, **patch}])


def test_codel_false_ecn_is_explicit_but_red_has_no_noecn_option() -> None:
    assert _tc_qdisc_arguments(SimpleQdiscPlan("codel", {"ecn": False})) == ["codel", "noecn"]
    assert _tc_qdisc_arguments(SimpleQdiscPlan("red", {"ecn": False})) == ["red"]


@pytest.mark.parametrize(
    "qdisc",
    [
        SimpleQdiscPlan("pfifo", {"limit": 1000}),
        HtbPlan("100mbit", SimpleQdiscPlan("pfifo", {"limit": 1000})),
        FqCodelPlan(5, 100, 10240, True),
        HtbPlan("100mbit", FqCodelPlan(5, 100, 10240, True)),
        None,
    ],
)
@pytest.mark.parametrize("inspect_qdiscs,exists", [(True, True), (False, True), (True, False)])
def test_namespace_inventory_reads_tc_only_for_declared_simple_qdiscs(
    qdisc: QdiscPlan | None,
    inspect_qdiscs: bool,
    exists: bool,
) -> None:
    from nslab.planner import EndpointPlan, LinkPlan, NodePlan, TopologyPlan

    node = NodePlan("h1", "linux", "owned-ns", {"eth0": (IPv4Interface("10.0.0.1/24"),)}, (), {})
    link = LinkPlan(
        0,
        "veth",
        EndpointPlan("h1", "eth0", "owned-ns", "tmp1"),
        EndpointPlan("h2", "eth0", "owned-peer", "tmp2"),
        1500,
        qdisc=qdisc,
    )
    plan = TopologyPlan("test", "test", {"h1": node}, (link,))
    handle = Mock()
    handle.get_links.return_value = (
        [{"index": 10, "attrs": [("IFLA_IFNAME", "eth0")]}] if exists else []
    )
    for method in ("get_addr", "get_qdiscs", "get_classes", "get_routes"):
        getattr(handle, method).return_value = []
    backend = Pyroute2Backend()
    backend._read_tc_qdiscs = Mock(return_value=[])
    backend._inventory_interfaces = Mock(return_value=({}, {}))
    backend._read_sysctls = Mock(return_value={})
    backend._inventory_namespace(node, plan, handle, inspect_qdiscs=inspect_qdiscs)
    needs_tc = (
        inspect_qdiscs
        and exists
        and (
            isinstance(qdisc, SimpleQdiscPlan)
            or isinstance(qdisc, HtbPlan)
            and isinstance(qdisc.leaf, SimpleQdiscPlan)
        )
    )
    if needs_tc:
        backend._read_tc_qdiscs.assert_called_once_with("owned-ns", "eth0")
    else:
        backend._read_tc_qdiscs.assert_not_called()
    assert backend._inventory_interfaces.call_args.kwargs["tc_qdiscs"] == (
        {"eth0": []} if needs_tc else {}
    )
