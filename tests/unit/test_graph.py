from __future__ import annotations

import json

import pytest

from nslab.errors import NslabError
from nslab.graph import _Canvas, _escape_label, render_graph
from nslab.manifest import Manifest
from nslab.planner import TopologyPlan, compile_plan


def _bridge_manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "version": 1,
            "name": "bridge-fdb",
            "topology": {
                "nodes": {
                    "h1": {
                        "kind": "linux",
                        "interfaces": {"eth0": {"addresses": ["10.10.0.1/24"]}},
                    },
                    "sw1": {
                        "kind": "bridge",
                        "bridge": {
                            "name": "br0",
                            "stp": False,
                            "vlan_filtering": False,
                        },
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


def _stp_manifest() -> Manifest:
    document = _bridge_manifest().model_dump(mode="json")
    bridge = document["topology"]["nodes"]["sw1"]["bridge"]
    bridge.update(
        {
            "stp": True,
            "priority": 4096,
            "ports": {
                "swp1": {"path_cost": 10, "priority": 16},
                "swp2": {"path_cost": 100},
            },
        }
    )
    return Manifest.model_validate(document)


def _cycle_and_isolated_manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "version": 1,
            "name": "cycle",
            "topology": {
                "nodes": {
                    "a": {"kind": "linux"},
                    "b": {"kind": "linux"},
                    "c": {"kind": "linux"},
                    "alone": {"kind": "linux"},
                },
                "links": [
                    {"endpoints": ["a:p0", "b:p0"]},
                    {"endpoints": ["b:p1", "c:p0"]},
                    {"endpoints": ["c:p1", "a:p1"]},
                ],
            },
        }
    )


def _single_link_manifest(left_interface: str, right_interface: str) -> Manifest:
    return Manifest.model_validate(
        {
            "version": 1,
            "name": "label-edge",
            "topology": {
                "nodes": {
                    "a": {"kind": "linux"},
                    "b": {"kind": "linux"},
                },
                "links": [
                    {
                        "endpoints": [
                            f"a:{left_interface}",
                            f"b:{right_interface}",
                        ]
                    }
                ],
            },
        }
    )


class _CountingText(dict[tuple[int, int], str]):
    def __init__(self, values: dict[tuple[int, int], str]) -> None:
        super().__init__(values)
        self.lookups = 0

    def get(self, key: tuple[int, int], default: str | None = None) -> str | None:
        self.lookups += 1
        return super().get(key, default)


@pytest.fixture
def bridge_plan() -> TopologyPlan:
    return compile_plan(_bridge_manifest())


@pytest.mark.parametrize(
    ("detail", "bridge_summary"),
    [
        (False, "sw1 [bridge · br0]"),
        (True, "sw1 [bridge · br0 · stp off · vlan filtering off]"),
    ],
)
def test_tree_renders_bridge_with_optional_details(
    bridge_plan: TopologyPlan,
    detail: bool,
    bridge_summary: str,
) -> None:
    first_detail = "│  " + " " * len("swp1 ↔ eth0  ")
    last_detail = "   " + " " * len("swp2 ↔ eth0  ")

    assert render_graph(bridge_plan, "tree", detail=detail) == "\n".join(
        [
            "Topology: bridge-fdb",
            "",
            bridge_summary,
            "├─ swp1 ↔ eth0  h1 [linux]",
            f"{first_detail}eth0: 10.10.0.1/24",
            "└─ swp2 ↔ eth0  h2 [linux]",
            f"{last_detail}eth0: 10.10.0.2/24",
        ]
    )


def test_terminal_graph_hides_stp_tuning_unless_detail_is_requested() -> None:
    plan = compile_plan(_stp_manifest())

    compact = render_graph(plan, "tree")
    detailed = render_graph(plan, "tree", detail=True)

    assert "sw1 [bridge · br0]" in compact
    assert "priority 4096" not in compact
    assert "stp cost" not in compact
    assert "sw1 [bridge · br0 · stp on · priority 4096 · vlan filtering off]" in detailed
    assert "swp1: stp cost 10 · priority 16" in detailed
    assert "swp2: stp cost 100" in detailed


def test_terminal_graph_renders_bridge_vlan_membership_only_with_detail() -> None:
    document = _bridge_manifest().model_dump(mode="json")
    bridge = document["topology"]["nodes"]["sw1"]["bridge"]
    bridge.update(
        {
            "vlan_filtering": True,
            "ports": {
                "swp1": {
                    "vlans": [
                        {"vid": 10, "pvid": True, "untagged": True},
                    ]
                },
                "swp2": {"vlans": [{"vid": 10}, {"vid": 20}]},
            },
        }
    )
    plan = compile_plan(Manifest.model_validate(document))

    compact = render_graph(plan, "tree")
    detailed = render_graph(plan, "tree", detail=True)

    assert "vlans" not in compact
    assert "swp1: vlans 10 pvid untagged" in detailed
    assert "swp2: vlans 10, 20" in detailed


def test_terminal_graph_renders_netem_only_with_detail_and_json_keeps_structure() -> None:
    document = _bridge_manifest().model_dump(mode="json")
    document["topology"]["links"][0]["netem"] = {
        "delay_ms": 100,
        "jitter_ms": 10,
        "loss_percent": 5,
    }
    plan = compile_plan(Manifest.model_validate(document))

    compact = render_graph(plan, "tree")
    detailed = render_graph(plan, "tree", detail=True)
    box = render_graph(plan, "box", detail=True)
    graph_json = json.loads(render_graph(plan, "json"))

    assert "netem" not in compact
    assert "eth0: netem delay 100ms · jitter 10ms · loss 5%" in detailed
    assert "swp1: netem delay 100ms · jitter 10ms · loss 5%" in detailed
    assert "eth0: netem delay 100ms · jitter 10ms · loss 5%" in box
    assert graph_json["links"][0]["netem"] == {
        "delay_ms": 100,
        "jitter_ms": 10,
        "loss_percent": 5,
    }
    assert "netem" not in graph_json["links"][1]


def test_tree_renders_cycle_cross_link_and_isolated_component() -> None:
    output = render_graph(compile_plan(_cycle_and_isolated_manifest()), "tree")

    assert output.splitlines() == [
        "Topology: cycle",
        "",
        "a [linux]",
        "├─ p0 ↔ p0  b [linux]",
        "└─ p1 ↔ p1  c [linux]",
        "Cross-links:",
        "  ↩ [L1] b:p1 ↔ c:p0",
        "",
        "alone [linux]",
    ]


def test_tree_renders_long_chain_without_recursion_error() -> None:
    node_count = 1100
    manifest = Manifest.model_validate(
        {
            "version": 1,
            "name": "long-chain",
            "topology": {
                "nodes": {f"n{index:04d}": {"kind": "linux"} for index in range(node_count)},
                "links": [
                    {
                        "endpoints": [
                            f"n{index:04d}:right",
                            f"n{index + 1:04d}:left",
                        ]
                    }
                    for index in range(node_count - 1)
                ],
            },
        }
    )

    output = render_graph(compile_plan(manifest), "tree")

    assert output.startswith("Topology: long-chain\n\nn0001 [linux]\n")
    assert output.count("[linux]") == node_count
    assert "n1099 [linux]" in output


def test_tree_classifies_parallel_and_self_loop_links_as_cross_links() -> None:
    manifest = Manifest.model_validate(
        {
            "version": 1,
            "name": "multigraph",
            "topology": {
                "nodes": {
                    "a": {"kind": "linux"},
                    "b": {"kind": "linux"},
                },
                "links": [
                    {"endpoints": ["a:p0", "b:p0"]},
                    {"endpoints": ["a:p1", "b:p1"]},
                    {"endpoints": ["a:p2", "a:p3"]},
                ],
            },
        }
    )

    output = render_graph(compile_plan(manifest), "tree")

    assert output.splitlines() == [
        "Topology: multigraph",
        "",
        "a [linux]",
        "└─ p0 ↔ p0  b [linux]",
        "Cross-links:",
        "  ↩ [L1] a:p1 ↔ b:p1",
        "  ↩ [L2] a:p2 ↔ a:p3",
    ]


def test_tree_preserves_long_name_and_multiple_address_order() -> None:
    manifest = Manifest.model_validate(
        {
            "version": 1,
            "name": "address-view",
            "topology": {
                "nodes": {
                    "host-with-long-name": {
                        "kind": "linux",
                        "interfaces": {"eth0": {"addresses": ["10.0.0.1/24", "10.0.0.2/24"]}},
                    },
                    "peer": {"kind": "linux"},
                },
                "links": [{"endpoints": ["host-with-long-name:eth0", "peer:eth0"]}],
            },
        }
    )

    output = render_graph(compile_plan(manifest), "tree")

    assert "host-with-long-name [linux]" in output
    assert "  eth0: 10.0.0.1/24, 10.0.0.2/24" in output


def test_canvas_composes_crossing_line_segments() -> None:
    canvas = _Canvas()
    canvas.add_horizontal(y=1, start=0, end=4)
    canvas.add_vertical(x=2, start=0, end=2)

    assert canvas.render() == "  │\n──┼──\n  │"


def test_canvas_scans_only_each_sparse_row_to_its_occupied_width() -> None:
    canvas = _Canvas()
    canvas.add_text(x=100, y=0, value="X")
    canvas.add_text(x=0, y=100, value="Y")
    counting_text = _CountingText(canvas._text)
    canvas._text = counting_text

    output = canvas.render()

    assert output.splitlines()[0] == f"{' ' * 100}X"
    assert output.splitlines()[-1] == "Y"
    assert counting_text.lookups <= 102


def test_box_renders_detailed_layered_bridge(bridge_plan: TopologyPlan) -> None:
    assert render_graph(bridge_plan, "box", detail=True) == (
        "Topology: bridge-fdb\n"
        "\n"
        " ┌─────────────────────────────────────────────┐\n"
        " │ sw1                                         │\n"
        " │ bridge · br0 · stp off · vlan filtering off │\n"
        " └──────────────────────┬──────────────────────┘\n"
        "                        │\n"
        "           ┌────────────┴─────────────┐\n"
        "           │                          │\n"
        "      swp1 ↔ eth0                swp2 ↔ eth0\n"
        "           │                          │\n"
        "┌──────────┴─────────┐     ┌──────────┴─────────┐\n"
        "│ h1                 │     │ h2                 │\n"
        "│ linux              │     │ linux              │\n"
        "│ eth0: 10.10.0.1/24 │     │ eth0: 10.10.0.2/24 │\n"
        "└────────────────────┘     └────────────────────┘"
    )


def test_box_renders_compact_layered_bridge_by_default(
    bridge_plan: TopologyPlan,
) -> None:
    assert render_graph(bridge_plan, "box") == (
        "Topology: bridge-fdb\n"
        "\n"
        "                ┌──────────────┐\n"
        "                │ sw1          │\n"
        "                │ bridge · br0 │\n"
        "                └───────┬──────┘\n"
        "                        │\n"
        "           ┌────────────┴─────────────┐\n"
        "           │                          │\n"
        "      swp1 ↔ eth0                swp2 ↔ eth0\n"
        "           │                          │\n"
        "┌──────────┴─────────┐     ┌──────────┴─────────┐\n"
        "│ h1                 │     │ h2                 │\n"
        "│ linux              │     │ linux              │\n"
        "│ eth0: 10.10.0.1/24 │     │ eth0: 10.10.0.2/24 │\n"
        "└────────────────────┘     └────────────────────┘"
    )


def test_box_preserves_even_length_label_starting_at_negative_x() -> None:
    output = render_graph(compile_plan(_single_link_manifest("abc", "abcd")), "box")

    assert output == (
        "Topology: label-edge\n"
        "\n"
        " ┌───────┐\n"
        " │ a     │\n"
        " │ linux │\n"
        " └───┬───┘\n"
        "     │\n"
        "     │\n"
        "     │\n"
        "abc ↔ abcd\n"
        "     │\n"
        " ┌───┴───┐\n"
        " │ b     │\n"
        " │ linux │\n"
        " └───────┘"
    )


@pytest.mark.parametrize("left_length", range(1, 16))
@pytest.mark.parametrize("right_length", range(1, 16))
def test_box_preserves_interface_labels_for_odd_and_even_lengths(
    left_length: int, right_length: int
) -> None:
    left_interface = "l" * left_length
    right_interface = "r" * right_length
    output = render_graph(
        compile_plan(_single_link_manifest(left_interface, right_interface)),
        "box",
    )

    assert f"{left_interface} ↔ {right_interface}" in output


def test_box_output_has_no_trailing_spaces(bridge_plan: TopologyPlan) -> None:
    output = render_graph(bridge_plan, "box")

    assert all(line == line.rstrip() for line in output.splitlines())


def test_box_lists_cycle_as_cross_link() -> None:
    output = render_graph(compile_plan(_cycle_and_isolated_manifest()), "box")

    assert "Cross-links:\n  [L1] b:p1 ↔ c:p0" in output
    assert output.count("b:p1 ↔ c:p0") == 1


def test_box_separates_disconnected_component_canvases() -> None:
    output = render_graph(compile_plan(_cycle_and_isolated_manifest()), "box")

    assert "Cross-links:\n  [L1] b:p1 ↔ c:p0\n\n┌───────┐\n│ alone │" in output


def test_box_layers_nodes_with_different_heights_without_overlap() -> None:
    manifest = Manifest.model_validate(
        {
            "version": 1,
            "name": "uneven-layers",
            "topology": {
                "nodes": {
                    "root": {"kind": "linux"},
                    "tall": {
                        "kind": "linux",
                        "interfaces": {
                            "p0": {"addresses": ["10.0.0.1/24"]},
                        },
                    },
                    "short": {"kind": "linux"},
                    "leaf": {"kind": "linux"},
                },
                "links": [
                    {"endpoints": ["root:p0", "tall:p0"]},
                    {"endpoints": ["root:p1", "short:p0"]},
                    {"endpoints": ["short:p1", "leaf:p0"]},
                ],
            },
        }
    )

    output = render_graph(compile_plan(manifest), "box")

    assert output.count("│ root") == 1
    assert output.count("│ tall") == 1
    assert output.count("│ short") == 1
    assert output.count("│ leaf") == 1
    assert "│ p0: 10.0.0.1/24" in output
    assert all(line == line.rstrip() for line in output.splitlines())


def test_box_renders_long_chain_without_recursion_error() -> None:
    node_count = 1100
    manifest = Manifest.model_validate(
        {
            "version": 1,
            "name": "long-box-chain",
            "topology": {
                "nodes": {f"n{index:04d}": {"kind": "linux"} for index in range(node_count)},
                "links": [
                    {
                        "endpoints": [
                            f"n{index:04d}:right",
                            f"n{index + 1:04d}:left",
                        ]
                    }
                    for index in range(node_count - 1)
                ],
            },
        }
    )

    output = render_graph(compile_plan(manifest), "box")

    assert output.startswith("Topology: long-box-chain\n\n")
    assert output.count("│ linux") == node_count
    assert "│ n1099" in output


def test_mermaid_preserves_node_and_link_order(bridge_plan: TopologyPlan) -> None:
    assert render_graph(bridge_plan, "mermaid") == (
        "flowchart LR\n"
        '    n0["h1\\nlinux"]\n'
        '    n1["sw1\\nbridge"]\n'
        '    n2["h2\\nlinux"]\n'
        '    n0 -- "eth0 <-> swp1" --- n1\n'
        '    n2 -- "eth0 <-> swp2" --- n1'
    )


def test_dot_preserves_node_and_link_order_and_quotes_labels(
    bridge_plan: TopologyPlan,
) -> None:
    assert render_graph(bridge_plan, "dot") == (
        "graph nslab {\n"
        '    "h1" [label="h1\\nlinux"];\n'
        '    "sw1" [label="sw1\\nbridge"];\n'
        '    "h2" [label="h2\\nlinux"];\n'
        '    "h1" -- "sw1" [label="eth0 <-> swp1"];\n'
        '    "h2" -- "sw1" [label="eth0 <-> swp2"];\n'
        "}"
    )


def test_json_uses_ordered_nodes_and_links(bridge_plan: TopologyPlan) -> None:
    output = render_graph(bridge_plan, "json")
    document = json.loads(output)

    assert document["name"] == "bridge-fdb"
    assert [node["name"] for node in document["nodes"]] == ["h1", "sw1", "h2"]
    assert [
        [(endpoint["node"], endpoint["interface"]) for endpoint in link["endpoints"]]
        for link in document["links"]
    ] == [
        [("h1", "eth0"), ("sw1", "swp1")],
        [("h2", "eth0"), ("sw1", "swp2")],
    ]
    assert output == json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)


def test_mermaid_uses_schema_names_with_hyphens_and_underscores() -> None:
    manifest = Manifest.model_validate(
        {
            "version": 1,
            "name": "name-test",
            "topology": {
                "nodes": {
                    "host-a": {"kind": "linux"},
                    "switch_b": {"kind": "linux"},
                },
                "links": [{"endpoints": ["host-a:eth-0", "switch_b:eth_1"]}],
            },
        }
    )

    output = render_graph(compile_plan(manifest), "mermaid")

    assert output == (
        "flowchart LR\n"
        '    n0["host-a\\nlinux"]\n'
        '    n1["switch_b\\nlinux"]\n'
        '    n0 -- "eth-0 <-> eth_1" --- n1'
    )


def test_mermaid_reserved_words_never_become_identifiers() -> None:
    manifest = Manifest.model_validate(
        {
            "version": 1,
            "name": "reserved-test",
            "topology": {
                "nodes": {
                    "end": {"kind": "linux"},
                    "subgraph": {"kind": "linux"},
                },
                "links": [{"endpoints": ["end:eth0", "subgraph:eth0"]}],
            },
        }
    )

    output = render_graph(compile_plan(manifest), "mermaid")

    assert output == (
        "flowchart LR\n"
        '    n0["end\\nlinux"]\n'
        '    n1["subgraph\\nlinux"]\n'
        '    n0 -- "eth0 <-> eth0" --- n1'
    )


def test_label_escape_handles_backslash_quote_and_line_endings() -> None:
    assert _escape_label('path\\to"node\r\nline') == 'path\\\\to\\"node\\r\\nline'


@pytest.mark.parametrize("output_format", ["tree", "box", "mermaid", "dot", "json"])
def test_equivalent_compiled_plans_render_identically(output_format: str) -> None:
    first_plan = compile_plan(_bridge_manifest())
    second_plan = compile_plan(_bridge_manifest())

    assert first_plan is not second_plan
    assert render_graph(first_plan, output_format) == render_graph(second_plan, output_format)


@pytest.mark.parametrize("output_format", ["tree", "box"])
@pytest.mark.parametrize("detail", [False, True])
def test_equivalent_plans_render_terminal_detail_modes_identically(
    output_format: str,
    detail: bool,
) -> None:
    assert render_graph(
        compile_plan(_bridge_manifest()), output_format, detail=detail
    ) == render_graph(compile_plan(_bridge_manifest()), output_format, detail=detail)


@pytest.mark.parametrize("output_format", ["mermaid", "dot", "json"])
def test_detail_rejects_non_terminal_graph_formats(
    bridge_plan: TopologyPlan,
    output_format: str,
) -> None:
    with pytest.raises(NslabError) as caught:
        render_graph(bridge_plan, output_format, detail=True)

    assert caught.value.as_dict() == {
        "code": "GRAPH_DETAIL_UNSUPPORTED",
        "message": f"graph detail is not supported for format: {output_format}",
        "details": {"format": output_format},
    }


def test_unknown_format_precedes_detail_compatibility_error(
    bridge_plan: TopologyPlan,
) -> None:
    with pytest.raises(NslabError) as caught:
        render_graph(bridge_plan, "svg", detail=True)

    assert caught.value.code == "GRAPH_FORMAT_INVALID"


def test_unsupported_format_raises_stable_domain_error(
    bridge_plan: TopologyPlan,
) -> None:
    with pytest.raises(NslabError) as caught:
        render_graph(bridge_plan, "svg")

    assert caught.value.as_dict() == {
        "code": "GRAPH_FORMAT_INVALID",
        "message": "unsupported graph format: svg",
        "details": {"format": "svg"},
    }
