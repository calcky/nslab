from __future__ import annotations

from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv4Network,
    IPv6Address,
    IPv6Interface,
    IPv6Network,
)
from pathlib import Path

import pytest

from nslab.graph import render_graph
from nslab.manifest import load_manifest
from nslab.planner import BridgeVlanPlan, NetemPlan, RoutePlan, compile_plan

_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
_DOCS = Path(__file__).resolve().parents[2] / "docs"
_DOCS_EXAMPLES = _DOCS / "examples"


@pytest.mark.parametrize(
    "manifest_path",
    sorted(_EXAMPLES.glob("*/nslab.yaml")),
    ids=lambda path: path.parent.name,
)
def test_every_example_manifest_compiles(manifest_path: Path) -> None:
    plan = compile_plan(load_manifest(manifest_path))

    assert plan.name == manifest_path.parent.name
    assert plan.nodes


@pytest.mark.parametrize(
    "manifest_path",
    sorted(_EXAMPLES.glob("*/nslab.yaml")),
    ids=lambda path: path.parent.name,
)
def test_every_example_has_a_usage_readme(manifest_path: Path) -> None:
    readme = manifest_path.with_name("README.md")

    assert readme.is_file()
    content = readme.read_text(encoding="utf-8")
    for command in ("graph", "deploy", "inspect", "exec", "destroy"):
        assert f"nslab {command}" in content


@pytest.mark.parametrize(
    "manifest_path",
    sorted(_EXAMPLES.glob("*/nslab.yaml")),
    ids=lambda path: path.parent.name,
)
def test_every_example_document_has_current_mermaid_and_command_output(
    manifest_path: Path,
) -> None:
    name = manifest_path.parent.name
    plan = compile_plan(load_manifest(manifest_path))
    expected_graph = f"```mermaid\n{render_graph(plan, 'mermaid')}\n```"
    documents = (
        manifest_path.with_name("README.md"),
        _DOCS_EXAMPLES / f"{name}.md",
        _DOCS_EXAMPLES / f"{name}.zh.md",
    )
    documented_commands: list[tuple[str, ...]] = []

    for document in documents:
        content = document.read_text(encoding="utf-8")
        lines = content.splitlines()
        assert "nslab graph --format mermaid" in content
        assert expected_graph in content
        assert f"deployed topology: {name}" in content
        assert "status: deployed" in content
        assert f"destroyed topology: {name}" in content
        assert "```console" in content
        for index, line in enumerate(lines):
            if line.startswith("$ sudo nslab"):
                assert lines[index + 1]
                assert not lines[index + 1].startswith("$")
        documented_commands.append(
            tuple(
                line.removeprefix("$ ")
                for line in lines
                if line.startswith(("$ sudo nslab", "sudo nslab"))
            )
        )

    assert documented_commands[0] == documented_commands[1] == documented_commands[2]


def test_cli_docs_show_every_current_graph_format() -> None:
    plan = compile_plan(load_manifest(_EXAMPLES / "bridge-fdb" / "nslab.yaml"))
    output_fences = {
        "tree": "text",
        "box": "text",
        "mermaid": "mermaid",
        "dot": "dot",
        "json": "json",
    }

    for document in (_DOCS / "cli.md", _DOCS / "cli.zh.md"):
        content = document.read_text(encoding="utf-8")
        for output_format, fence in output_fences.items():
            expected = f"```{fence}\n{render_graph(plan, output_format)}\n```"
            assert expected in content


def test_bridge_stp_example_combines_election_tiebreak_and_failover() -> None:
    manifest = load_manifest(_EXAMPLES / "bridge-stp" / "nslab.yaml")
    plan = compile_plan(manifest)

    assert plan.name == "bridge-stp"
    assert tuple(plan.nodes) == ("h1", "sw1", "sw2", "sw3", "sw4", "h2")
    assert len(plan.links) == 7
    assert plan.nodes["sw1"].bridge_priority == 4096
    assert plan.nodes["sw2"].bridge_priority == 8192
    assert plan.nodes["sw3"].bridge_priority == 12288
    assert plan.nodes["sw4"].bridge_priority == 16384
    assert tuple(
        (link.left.node, link.left.interface, link.right.node, link.right.interface)
        for link in plan.links[1:3]
    ) == (
        ("sw1", "swp1", "sw2", "swp1"),
        ("sw1", "swp2", "sw2", "swp2"),
    )
    assert plan.nodes["sw1"].bridge_ports["swp1"].path_cost == 10
    assert plan.nodes["sw1"].bridge_ports["swp2"].path_cost == 10
    assert plan.nodes["sw1"].bridge_ports["swp1"].priority == 32
    assert plan.nodes["sw1"].bridge_ports["swp2"].priority == 16
    assert plan.nodes["sw4"].bridge_ports["swp1"].path_cost == 10
    assert plan.nodes["sw4"].bridge_ports["swp2"].path_cost == 100
    assert plan.nodes["h1"].interfaces["eth0"] == (IPv4Interface("10.20.0.1/24"),)
    assert plan.nodes["h2"].interfaces["eth0"] == (IPv4Interface("10.20.0.2/24"),)


def test_bridge_vlan_example_has_access_ports_and_tagged_trunk() -> None:
    plan = compile_plan(load_manifest(_EXAMPLES / "bridge-vlan" / "nslab.yaml"))

    assert plan.name == "bridge-vlan"
    assert tuple(plan.nodes) == ("h10a", "sw1", "h20a", "sw2", "h10b", "h20b")
    assert len(plan.links) == 5
    access10 = (BridgeVlanPlan(vid=10, pvid=True, untagged=True),)
    access20 = (BridgeVlanPlan(vid=20, pvid=True, untagged=True),)
    trunk = (
        BridgeVlanPlan(vid=10, pvid=False, untagged=False),
        BridgeVlanPlan(vid=20, pvid=False, untagged=False),
    )
    assert plan.nodes["sw1"].bridge_ports["access10"].vlans == access10
    assert plan.nodes["sw1"].bridge_ports["access20"].vlans == access20
    assert plan.nodes["sw1"].bridge_ports["trunk"].vlans == trunk
    assert plan.nodes["sw2"].bridge_ports["trunk"].vlans == trunk


def test_ipv4_forward_example_compiles_router_and_bidirectional_routes() -> None:
    manifest = load_manifest(_EXAMPLES / "ipv4-forward" / "nslab.yaml")
    plan = compile_plan(manifest)

    assert plan.name == "ipv4-forward"
    assert tuple(plan.nodes) == ("h1", "r1", "h2")
    assert tuple(
        (link.left.node, link.left.interface, link.right.node, link.right.interface)
        for link in plan.links
    ) == (
        ("h1", "eth0", "r1", "eth0"),
        ("r1", "eth1", "h2", "eth0"),
    )

    r1 = plan.nodes["r1"]
    assert r1.kind == "linux"
    assert r1.interfaces == {
        "eth0": (IPv4Interface("192.0.2.1/24"),),
        "eth1": (IPv4Interface("198.51.100.1/24"),),
    }
    assert r1.routes == ()
    assert r1.sysctls == {"net.ipv4.ip_forward": 1}

    assert plan.nodes["h1"].routes == (
        RoutePlan(
            dst=IPv4Network("198.51.100.0/24"),
            via=IPv4Address("192.0.2.1"),
            dev="eth0",
        ),
    )
    assert plan.nodes["h2"].routes == (
        RoutePlan(
            dst=IPv4Network("192.0.2.0/24"),
            via=IPv4Address("198.51.100.1"),
            dev="eth0",
        ),
    )


def test_ipv6_forward_example_compiles_router_and_default_routes() -> None:
    plan = compile_plan(load_manifest(_EXAMPLES / "ipv6-forward" / "nslab.yaml"))

    assert plan.name == "ipv6-forward"
    assert tuple(plan.nodes) == ("h1", "r1", "h2")
    assert plan.nodes["r1"].interfaces == {
        "eth0": (IPv6Interface("2001:db8:1::1/64"),),
        "eth1": (IPv6Interface("2001:db8:2::1/64"),),
    }
    assert plan.nodes["r1"].sysctls == {"net.ipv6.conf.all.forwarding": 1}
    assert plan.nodes["h1"].routes == (
        RoutePlan(
            dst=IPv6Network("::/0"),
            via=IPv6Address("2001:db8:1::1"),
            dev="eth0",
        ),
    )
    assert plan.nodes["h2"].routes == (
        RoutePlan(
            dst=IPv6Network("::/0"),
            via=IPv6Address("2001:db8:2::1"),
            dev="eth0",
        ),
    )


def test_netem_example_compiles_bidirectional_link_impairment() -> None:
    plan = compile_plan(load_manifest(_EXAMPLES / "netem" / "nslab.yaml"))

    assert plan.name == "netem"
    assert tuple(plan.nodes) == ("h1", "h2")
    assert len(plan.links) == 1
    assert plan.links[0].netem == NetemPlan(
        delay_ms=100,
        jitter_ms=10,
        loss_percent=5,
    )
