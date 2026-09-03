from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from nslab.errors import NslabError
from nslab.planner import (
    BondDevicePlan,
    DevicePlan,
    DummyDevicePlan,
    FqCodelPlan,
    GeneveDevicePlan,
    IpvlanDevicePlan,
    LinkPlan,
    MacvlanDevicePlan,
    NetemPlan,
    NodePlan,
    QdiscPlan,
    TbfPlan,
    TopologyPlan,
    VlanDevicePlan,
    VrfDevicePlan,
    VxlanDevicePlan,
)


@dataclass(frozen=True, slots=True)
class _DisplayEdge:
    link: LinkPlan
    parent: str
    child: str
    parent_interface: str
    child_interface: str


@dataclass(frozen=True, slots=True)
class _DisplayComponent:
    root: str
    children: Mapping[str, tuple[_DisplayEdge, ...]]
    cross_links: tuple[LinkPlan, ...]


def _oriented_edge(link: LinkPlan, parent: str, child: str) -> _DisplayEdge:
    if link.left.node == parent and link.right.node == child:
        return _DisplayEdge(link, parent, child, link.left.interface, link.right.interface)
    return _DisplayEdge(link, parent, child, link.right.interface, link.left.interface)


def _build_display_forest(plan: TopologyPlan) -> tuple[_DisplayComponent, ...]:
    order = {name: index for index, name in enumerate(plan.nodes)}
    adjacency: dict[str, list[tuple[LinkPlan, str]]] = {name: [] for name in plan.nodes}
    for link in plan.links:
        adjacency[link.left.node].append((link, link.right.node))
        adjacency[link.right.node].append((link, link.left.node))

    remaining = set(plan.nodes)
    components: list[_DisplayComponent] = []
    for first in plan.nodes:
        if first not in remaining:
            continue

        member_queue = deque([first])
        remaining.remove(first)
        members: list[str] = []
        while member_queue:
            current = member_queue.popleft()
            members.append(current)
            for _, neighbor in adjacency[current]:
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    member_queue.append(neighbor)

        root = min(members, key=lambda name: (-len(adjacency[name]), order[name]))
        children: dict[str, list[_DisplayEdge]] = {name: [] for name in members}
        seen = {root}
        tree_links: set[int] = set()
        tree_queue = deque([root])
        while tree_queue:
            parent = tree_queue.popleft()
            for link, child in adjacency[parent]:
                if child in seen:
                    continue
                seen.add(child)
                tree_links.add(link.index)
                children[parent].append(_oriented_edge(link, parent, child))
                tree_queue.append(child)

        member_set = set(members)
        cross_links = tuple(
            link
            for link in plan.links
            if link.index not in tree_links
            and link.left.node in member_set
            and link.right.node in member_set
        )
        components.append(
            _DisplayComponent(
                root=root,
                children=MappingProxyType({name: tuple(edges) for name, edges in children.items()}),
                cross_links=cross_links,
            )
        )
    return tuple(components)


def _on_off(value: bool | None) -> str:
    return "on" if value else "off"


def _node_kind_text(node: NodePlan, *, detail: bool) -> str:
    if node.kind == "bridge":
        summary = f"bridge · {node.bridge_name}"
        if detail:
            summary += f" · stp {_on_off(node.stp)}"
            if node.bridge_priority is not None:
                summary += f" · priority {node.bridge_priority}"
            summary += f" · vlan filtering {_on_off(node.vlan_filtering)}"
        return summary
    summary = "linux"
    if detail and node.routing is not None:
        protocols = []
        if node.routing.ospf is not None:
            protocols.append("ospf")
        if node.routing.bgp is not None:
            protocols.append(f"bgp as {node.routing.bgp.local_as}")
        if protocols:
            summary += " · " + " · ".join(protocols)
    return summary


def _node_summary(node: NodePlan, *, detail: bool) -> str:
    return f"{node.name} [{_node_kind_text(node, detail=detail)}]"


def _netem_text(netem: NetemPlan) -> str:
    values = []
    if netem.rate:
        values.append(f"rate {netem.rate}")
    if netem.delay_ms:
        values.append(f"delay {netem.delay_ms}ms")
    if netem.jitter_ms:
        values.append(f"jitter {netem.jitter_ms}ms")
    if netem.loss_percent:
        values.append(f"loss {netem.loss_percent}%")
    return " · ".join(values)


def _qdisc_text(qdisc: QdiscPlan) -> str:
    if isinstance(qdisc, TbfPlan):
        return f"tbf rate {qdisc.rate} · burst {qdisc.burst_bytes}B · latency {qdisc.latency_ms}ms"
    assert isinstance(qdisc, FqCodelPlan)
    return (
        f"fq_codel target {qdisc.target_ms}ms · interval {qdisc.interval_ms}ms"
        f" · limit {qdisc.limit} · ecn {'on' if qdisc.ecn else 'off'}"
    )


def _device_text(
    device: DevicePlan,
    *,
    include_addresses: bool = True,
    detail: bool = False,
) -> str:
    if isinstance(device, VlanDevicePlan):
        text = f"{device.name}: vlan {device.vlan_id} on {device.link}"
        if include_addresses and device.addresses:
            text += f" · {', '.join(str(address) for address in device.addresses)}"
        return text
    if isinstance(device, VrfDevicePlan):
        return f"{device.name}: vrf table {device.table} · members {', '.join(device.interfaces)}"
    if isinstance(device, BondDevicePlan):
        text = f"{device.name}: bond {device.mode} · members {', '.join(device.interfaces)}"
        if detail:
            options = [f"miimon {device.miimon_ms}ms"]
            if device.primary is not None:
                options.append(f"primary {device.primary}")
            if device.lacp_rate is not None:
                options.append(f"lacp {device.lacp_rate}")
            if device.xmit_hash_policy is not None:
                options.append(f"hash {device.xmit_hash_policy}")
            if device.min_links is not None:
                options.append(f"min links {device.min_links}")
            text += f" · {' · '.join(options)}"
        if include_addresses and device.addresses:
            text += f" · {', '.join(str(address) for address in device.addresses)}"
        return text
    if isinstance(device, DummyDevicePlan):
        text = f"{device.name}: dummy"
        if detail and device.mtu is not None:
            text += f" · mtu {device.mtu}"
        if include_addresses and device.addresses:
            text += f" · {', '.join(str(address) for address in device.addresses)}"
        return text
    if isinstance(device, GeneveDevicePlan):
        text = f"{device.name}: geneve {device.vni} -> {device.remote}"
        if detail:
            text += f" · via {device.link} · udp {device.dst_port}"
            if device.mtu is not None:
                text += f" · mtu {device.mtu}"
        if include_addresses and device.addresses:
            text += f" · {', '.join(str(address) for address in device.addresses)}"
        return text
    if isinstance(device, MacvlanDevicePlan):
        text = f"{device.name}: macvlan {device.mode} on {device.link}"
        if detail and device.mtu is not None:
            text += f" · mtu {device.mtu}"
        if include_addresses and device.addresses:
            text += f" · {', '.join(str(address) for address in device.addresses)}"
        return text
    if isinstance(device, IpvlanDevicePlan):
        text = f"{device.name}: ipvlan {device.mode} on {device.link}"
        if detail and device.mtu is not None:
            text += f" · mtu {device.mtu}"
        if include_addresses and device.addresses:
            text += f" · {', '.join(str(address) for address in device.addresses)}"
        return text
    assert isinstance(device, VxlanDevicePlan)
    text = f"{device.name}: vxlan {device.vni} -> {device.remote}"
    if detail:
        text += (
            f" · local {device.local} via {device.link}"
            f" · udp {device.dst_port}"
            f" · learning {'on' if device.learning else 'off'}"
        )
        if device.mtu is not None:
            text += f" · mtu {device.mtu}"
    if include_addresses and device.addresses:
        text += f" · {', '.join(str(address) for address in device.addresses)}"
    return text


def _node_details(node: NodePlan, plan: TopologyPlan, *, detail: bool) -> tuple[str, ...]:
    lines = [
        f"{interface}: {', '.join(str(address) for address in addresses)}"
        for interface, addresses in node.interfaces.items()
        if addresses
    ]
    lines.extend(_device_text(device, detail=detail) for device in node.devices.values())
    if detail:
        sections_by_interface: dict[str, list[str]] = {}
        for interface, port in node.bridge_ports.items():
            sections: list[str] = []
            stp_settings: list[str] = []
            if port.path_cost is not None:
                stp_settings.append(f"cost {port.path_cost}")
            if port.priority is not None:
                stp_settings.append(f"priority {port.priority}")
            if stp_settings:
                sections.append(f"stp {' · '.join(stp_settings)}")
            if port.vlans:
                vlan_settings = []
                for vlan in port.vlans:
                    flags = []
                    if vlan.pvid:
                        flags.append("pvid")
                    if vlan.untagged:
                        flags.append("untagged")
                    suffix = f" {' '.join(flags)}" if flags else ""
                    vlan_settings.append(f"{vlan.vid}{suffix}")
                sections.append(f"vlans {', '.join(vlan_settings)}")
            if sections:
                sections_by_interface[interface] = sections
        for link in plan.links:
            if link.netem is None and link.qdisc is None:
                continue
            for endpoint in (link.left, link.right):
                if endpoint.node == node.name:
                    sections = sections_by_interface.setdefault(endpoint.interface, [])
                    if link.netem is not None:
                        sections.append(f"netem {_netem_text(link.netem)}")
                    if link.qdisc is not None:
                        sections.append(f"qdisc {_qdisc_text(link.qdisc)}")
        lines.extend(
            f"{interface}: {' · '.join(sections)}"
            for interface, sections in sections_by_interface.items()
        )
    return tuple(lines)


def _append_tree_children(
    lines: list[str],
    plan: TopologyPlan,
    component: _DisplayComponent,
    parent: str,
    prefix: str,
    *,
    detail: bool,
) -> None:
    children = component.children[parent]
    stack: list[tuple[_DisplayEdge, str, bool]] = []
    for index in range(len(children) - 1, -1, -1):
        stack.append((children[index], prefix, index == len(children) - 1))

    while stack:
        edge, edge_prefix, is_last = stack.pop()
        branch = "└─ " if is_last else "├─ "
        continuation = "   " if is_last else "│  "
        edge_text = f"{edge.parent_interface} ↔ {edge.child_interface}  "
        child = plan.nodes[edge.child]
        lines.append(f"{edge_prefix}{branch}{edge_text}{_node_summary(child, detail=detail)}")
        child_prefix = f"{edge_prefix}{continuation}{' ' * len(edge_text)}"
        lines.extend(
            f"{child_prefix}{node_detail}"
            for node_detail in _node_details(child, plan, detail=detail)
        )

        grandchildren = component.children[edge.child]
        for index in range(len(grandchildren) - 1, -1, -1):
            stack.append(
                (
                    grandchildren[index],
                    child_prefix,
                    index == len(grandchildren) - 1,
                )
            )


def _cross_link_line(link: LinkPlan) -> str:
    return (
        f"  ↩ [L{link.index}] {link.left.node}:{link.left.interface} ↔ "
        f"{link.right.node}:{link.right.interface}"
    )


def _render_tree(plan: TopologyPlan, *, detail: bool) -> str:
    lines = [f"Topology: {plan.name}", ""]
    components = _build_display_forest(plan)
    for component_index, component in enumerate(components):
        root = plan.nodes[component.root]
        lines.append(_node_summary(root, detail=detail))
        lines.extend(f"  {node_detail}" for node_detail in _node_details(root, plan, detail=detail))
        _append_tree_children(lines, plan, component, component.root, "", detail=detail)
        if component.cross_links:
            lines.append("Cross-links:")
            lines.extend(_cross_link_line(link) for link in component.cross_links)
        if component_index != len(components) - 1:
            lines.append("")
    return "\n".join(lines)


_UP, _RIGHT, _DOWN, _LEFT = 1, 2, 4, 8
_LINE_GLYPHS = {
    _UP: "│",
    _DOWN: "│",
    _LEFT: "─",
    _RIGHT: "─",
    _UP | _DOWN: "│",
    _LEFT | _RIGHT: "─",
    _RIGHT | _DOWN: "┌",
    _LEFT | _DOWN: "┐",
    _RIGHT | _UP: "└",
    _LEFT | _UP: "┘",
    _UP | _RIGHT | _DOWN: "├",
    _UP | _LEFT | _DOWN: "┤",
    _LEFT | _RIGHT | _DOWN: "┬",
    _LEFT | _RIGHT | _UP: "┴",
    _UP | _RIGHT | _DOWN | _LEFT: "┼",
}


class _Canvas:
    def __init__(self) -> None:
        self._lines: dict[tuple[int, int], int] = {}
        self._text: dict[tuple[int, int], str] = {}

    def _connect(self, first: tuple[int, int], second: tuple[int, int]) -> None:
        first_x, first_y = first
        second_x, second_y = second
        if second_x == first_x + 1 and second_y == first_y:
            first_flag, second_flag = _RIGHT, _LEFT
        elif second_x == first_x and second_y == first_y + 1:
            first_flag, second_flag = _DOWN, _UP
        else:
            raise ValueError("canvas segments must connect adjacent cells")
        self._lines[first] = self._lines.get(first, 0) | first_flag
        self._lines[second] = self._lines.get(second, 0) | second_flag

    def add_horizontal(self, *, y: int, start: int, end: int) -> None:
        for x in range(min(start, end), max(start, end)):
            self._connect((x, y), (x + 1, y))

    def add_vertical(self, *, x: int, start: int, end: int) -> None:
        for y in range(min(start, end), max(start, end)):
            self._connect((x, y), (x, y + 1))

    def add_text(self, *, x: int, y: int, value: str) -> None:
        for offset, character in enumerate(value):
            self._text[(x + offset, y)] = character

    def render(self) -> str:
        occupied = set(self._lines) | set(self._text)
        if not occupied:
            return ""
        start_x = min(0, min(x for x, _ in occupied))
        row_max: dict[int, int] = {}
        for x, y in occupied:
            row_max[y] = max(row_max.get(y, x), x)
        max_y = max(y for _, y in occupied)
        rows: list[str] = []
        for y in range(max_y + 1):
            row = "".join(
                self._text.get((x, y), _LINE_GLYPHS.get(self._lines.get((x, y), 0), " "))
                for x in range(start_x, row_max.get(y, start_x - 1) + 1)
            )
            rows.append(row.rstrip())
        return "\n".join(rows).rstrip()


@dataclass(frozen=True, slots=True)
class _Box:
    lines: tuple[str, ...]
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class _Placement:
    node: str
    x: int
    y: int
    box: _Box


_SIBLING_GAP = 5
_LAYER_GAP = 5


def _make_box(node: NodePlan, plan: TopologyPlan, *, detail: bool) -> _Box:
    content = (
        node.name,
        _node_kind_text(node, detail=detail),
        *_node_details(node, plan, detail=detail),
    )
    width = max(len(line) for line in content) + 4
    return _Box(lines=content, width=width, height=len(content) + 2)


def _tree_depths(component: _DisplayComponent) -> dict[str, int]:
    depths = {component.root: 0}
    queue = deque([component.root])
    while queue:
        parent = queue.popleft()
        for edge in component.children[parent]:
            depths[edge.child] = depths[parent] + 1
            queue.append(edge.child)
    return depths


def _subtree_widths(component: _DisplayComponent, boxes: Mapping[str, _Box]) -> dict[str, int]:
    traversal: list[str] = []
    stack = [component.root]
    while stack:
        node = stack.pop()
        traversal.append(node)
        stack.extend(edge.child for edge in component.children[node])

    widths: dict[str, int] = {}
    for node in reversed(traversal):
        child_spans: list[int] = []
        for edge in component.children[node]:
            label_width = len(f"{edge.parent_interface} ↔ {edge.child_interface}")
            child_span = max(widths[edge.child], label_width)
            widths[edge.child] = child_span
            child_spans.append(child_span)
        children_width = (
            sum(child_spans) + _SIBLING_GAP * (len(child_spans) - 1) if child_spans else 0
        )
        widths[node] = max(boxes[node].width, children_width)
    return widths


def _place_component(
    component: _DisplayComponent, boxes: Mapping[str, _Box]
) -> tuple[_Placement, ...]:
    depths = _tree_depths(component)
    layer_heights: dict[int, int] = {}
    for name, depth in depths.items():
        layer_heights[depth] = max(layer_heights.get(depth, 0), boxes[name].height)

    layer_tops: dict[int, int] = {}
    next_top = 0
    for depth in range(max(depths.values()) + 1):
        layer_tops[depth] = next_top
        next_top += layer_heights[depth] + _LAYER_GAP

    widths = _subtree_widths(component, boxes)
    placements: list[_Placement] = []
    stack: list[tuple[str, int]] = [(component.root, 0)]
    while stack:
        node, left = stack.pop()
        span = widths[node]
        box = boxes[node]
        placements.append(
            _Placement(
                node=node,
                x=left + (span - box.width) // 2,
                y=layer_tops[depths[node]],
                box=box,
            )
        )
        children = component.children[node]
        if not children:
            continue
        children_width = sum(widths[edge.child] for edge in children)
        children_width += _SIBLING_GAP * (len(children) - 1)
        child_left = left + (span - children_width) // 2
        child_positions: list[tuple[str, int]] = []
        for edge in children:
            child_positions.append((edge.child, child_left))
            child_left += widths[edge.child] + _SIBLING_GAP
        stack.extend(reversed(child_positions))
    return tuple(placements)


def _draw_box(canvas: _Canvas, placement: _Placement) -> None:
    left = placement.x
    right = left + placement.box.width - 1
    top = placement.y
    bottom = top + placement.box.height - 1
    canvas.add_horizontal(y=top, start=left, end=right)
    canvas.add_horizontal(y=bottom, start=left, end=right)
    canvas.add_vertical(x=left, start=top, end=bottom)
    canvas.add_vertical(x=right, start=top, end=bottom)
    for offset, line in enumerate(placement.box.lines, start=1):
        canvas.add_text(x=left + 2, y=top + offset, value=line)


def _draw_tree_edge(
    canvas: _Canvas,
    parent: _Placement,
    child: _Placement,
    edge: _DisplayEdge,
) -> None:
    parent_x = parent.x + parent.box.width // 2
    parent_bottom = parent.y + parent.box.height - 1
    child_x = child.x + child.box.width // 2
    child_top = child.y
    branch_y = parent_bottom + 2
    label_y = child_top - 2
    label = f"{edge.parent_interface} ↔ {edge.child_interface}"

    canvas.add_vertical(x=parent_x, start=parent_bottom, end=branch_y)
    canvas.add_horizontal(y=branch_y, start=parent_x, end=child_x)
    canvas.add_vertical(x=child_x, start=branch_y, end=label_y - 1)
    canvas.add_text(x=child_x - len(label) // 2, y=label_y, value=label)
    canvas.add_vertical(x=child_x, start=label_y + 1, end=child_top)


def _render_box_component(
    plan: TopologyPlan,
    component: _DisplayComponent,
    *,
    detail: bool,
) -> str:
    boxes: Mapping[str, _Box] = MappingProxyType(
        {name: _make_box(plan.nodes[name], plan, detail=detail) for name in component.children}
    )
    placements = _place_component(component, boxes)
    by_name = {placement.node: placement for placement in placements}
    canvas = _Canvas()
    for parent, edges in component.children.items():
        for edge in edges:
            _draw_tree_edge(canvas, by_name[parent], by_name[edge.child], edge)
    for placement in placements:
        _draw_box(canvas, placement)
    return canvas.render()


def _box_cross_link_line(link: LinkPlan) -> str:
    return (
        f"  [L{link.index}] {link.left.node}:{link.left.interface} ↔ "
        f"{link.right.node}:{link.right.interface}"
    )


def _render_box(plan: TopologyPlan, *, detail: bool) -> str:
    rendered_components: list[str] = []
    for component in _build_display_forest(plan):
        canvas = _render_box_component(plan, component, detail=detail)
        lines = [canvas]
        if component.cross_links:
            lines.append("Cross-links:")
            lines.extend(_box_cross_link_line(link) for link in component.cross_links)
        rendered_components.append("\n".join(lines))
    return f"Topology: {plan.name}\n\n" + "\n\n".join(rendered_components)


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n")


def _mermaid_node_line(
    node: NodePlan,
    node_id: str,
    *,
    compact_vxlan: bool = False,
) -> str:
    device_lines: list[str] = []
    for device in node.devices.values():
        if compact_vxlan and isinstance(device, VxlanDevicePlan):
            device_lines.append(f"{device.name} (VNI {device.vni})")
        else:
            device_lines.append(_device_text(device, include_addresses=False))
    suffix = "".join(f"\n{line}" for line in device_lines)
    label = _escape_label(f"{node.name}\n{node.kind}{suffix}")
    return f'{node_id}["{label}"]'


def _mermaid_box_node_line(node: NodePlan, node_id: str, plan: TopologyPlan) -> str:
    content = (
        node.name,
        _node_kind_text(node, detail=False),
        *_node_details(node, plan, detail=False),
    )
    label = _escape_label("\n".join(content))
    return f'{node_id}["{label}"]'


def _vxlan_nodes(plan: TopologyPlan) -> set[str]:
    return {
        name
        for name, node in plan.nodes.items()
        if any(
            isinstance(device, (VxlanDevicePlan, GeneveDevicePlan))
            for device in node.devices.values()
        )
    }


def _append_mermaid_box_component(
    lines: list[str],
    plan: TopologyPlan,
    component: _DisplayComponent,
    node_ids: Mapping[str, str],
) -> None:
    members = set(component.children)
    for name in plan.nodes:
        if name in members:
            lines.append("    " + _mermaid_box_node_line(plan.nodes[name], node_ids[name], plan))

    queue = deque([component.root])
    while queue:
        parent = queue.popleft()
        for edge in component.children[parent]:
            label = _escape_label(f"{edge.parent_interface} ↔ {edge.child_interface}")
            lines.append(f'    {node_ids[parent]} -- "{label}" --- {node_ids[edge.child]}')
            queue.append(edge.child)

    for link in component.cross_links:
        label = _escape_label(f"{link.left.interface} ↔ {link.right.interface}")
        lines.append(f'    {node_ids[link.left.node]} -. "{label}" .- {node_ids[link.right.node]}')


def _render_vxlan_mermaid(
    plan: TopologyPlan,
) -> str:
    node_ids = {name: f"n{index}" for index, name in enumerate(plan.nodes)}

    lines = ['%%{init: {"flowchart": {"curve": "step"}}}%%', "flowchart TB"]
    for component in _build_display_forest(plan):
        _append_mermaid_box_component(lines, plan, component, node_ids)

    return "\n".join(lines)


def _render_mermaid(plan: TopologyPlan) -> str:
    if _vxlan_nodes(plan):
        return _render_vxlan_mermaid(plan)

    lines = ["flowchart LR"]
    node_ids = {name: f"n{index}" for index, name in enumerate(plan.nodes)}
    for name, node in plan.nodes.items():
        lines.append("    " + _mermaid_node_line(node, node_ids[name]))
    for link in plan.links:
        label = _escape_label(f"{link.left.interface} <-> {link.right.interface}")
        left = node_ids[link.left.node]
        right = node_ids[link.right.node]
        lines.append(f'    {left} -- "{label}" --- {right}')
    return "\n".join(lines)


def _quoted_dot(value: str) -> str:
    return f'"{_escape_label(value)}"'


def _render_dot(plan: TopologyPlan) -> str:
    lines = ["graph nslab {"]
    for node in plan.nodes.values():
        identifier = _quoted_dot(node.name)
        device_lines = "".join(
            f"\n{_device_text(device, include_addresses=False)}" for device in node.devices.values()
        )
        label = _quoted_dot(f"{node.name}\n{node.kind}{device_lines}")
        lines.append(f"    {identifier} [label={label}];")
    for link in plan.links:
        left = _quoted_dot(link.left.node)
        right = _quoted_dot(link.right.node)
        label = _quoted_dot(f"{link.left.interface} <-> {link.right.interface}")
        lines.append(f"    {left} -- {right} [label={label}];")
    lines.append("}")
    return "\n".join(lines)


def _node_document(node: NodePlan) -> dict[str, object]:
    document: dict[str, object] = {
        "kind": node.kind,
        "name": node.name,
        "namespace": node.namespace,
    }
    if node.devices:
        devices: list[dict[str, object]] = []
        for device in node.devices.values():
            if isinstance(device, VlanDevicePlan):
                devices.append(
                    {
                        "addresses": [str(address) for address in device.addresses],
                        "id": device.vlan_id,
                        "link": device.link,
                        "name": device.name,
                        "type": "vlan",
                    }
                )
            elif isinstance(device, VrfDevicePlan):
                devices.append(
                    {
                        "interfaces": list(device.interfaces),
                        "name": device.name,
                        "table": device.table,
                        "type": "vrf",
                    }
                )
            elif isinstance(device, BondDevicePlan):
                bond_document: dict[str, object] = {
                    "addresses": [str(address) for address in device.addresses],
                    "interfaces": list(device.interfaces),
                    "miimon_ms": device.miimon_ms,
                    "mode": device.mode,
                    "name": device.name,
                    "type": "bond",
                }
                if device.primary is not None:
                    bond_document["primary"] = device.primary
                if device.lacp_rate is not None:
                    bond_document["lacp_rate"] = device.lacp_rate
                if device.xmit_hash_policy is not None:
                    bond_document["xmit_hash_policy"] = device.xmit_hash_policy
                if device.min_links is not None:
                    bond_document["min_links"] = device.min_links
                devices.append(bond_document)
            elif isinstance(device, DummyDevicePlan):
                dummy_document: dict[str, object] = {
                    "mtu": device.mtu,
                    "name": device.name,
                    "type": "dummy",
                }
                if device.addresses:
                    dummy_document["addresses"] = [str(address) for address in device.addresses]
                devices.append(dummy_document)
            elif isinstance(device, GeneveDevicePlan):
                geneve_document: dict[str, object] = {
                    "dst_port": device.dst_port,
                    "link": device.link,
                    "mtu": device.mtu,
                    "name": device.name,
                    "remote": str(device.remote),
                    "type": "geneve",
                    "vni": device.vni,
                }
                if device.addresses:
                    geneve_document["addresses"] = [str(address) for address in device.addresses]
                devices.append(geneve_document)
            elif isinstance(device, MacvlanDevicePlan):
                macvlan_document: dict[str, object] = {
                    "link": device.link,
                    "mode": device.mode,
                    "mtu": device.mtu,
                    "name": device.name,
                    "type": "macvlan",
                }
                if device.addresses:
                    macvlan_document["addresses"] = [str(address) for address in device.addresses]
                devices.append(macvlan_document)
            elif isinstance(device, IpvlanDevicePlan):
                ipvlan_document: dict[str, object] = {
                    "link": device.link,
                    "mode": device.mode,
                    "mtu": device.mtu,
                    "name": device.name,
                    "type": "ipvlan",
                }
                if device.addresses:
                    ipvlan_document["addresses"] = [str(address) for address in device.addresses]
                devices.append(ipvlan_document)
            else:
                assert isinstance(device, VxlanDevicePlan)
                vxlan_document: dict[str, object] = {
                    "dst_port": device.dst_port,
                    "learning": device.learning,
                    "link": device.link,
                    "local": str(device.local),
                    "mtu": device.mtu,
                    "name": device.name,
                    "remote": str(device.remote),
                    "type": "vxlan",
                    "vni": device.vni,
                }
                if device.addresses:
                    vxlan_document["addresses"] = [str(address) for address in device.addresses]
                devices.append(vxlan_document)
        document["devices"] = devices
    return document


def _endpoint_document(node: str, interface: str) -> dict[str, object]:
    return {"interface": interface, "node": node}


def _link_document(link: LinkPlan) -> dict[str, object]:
    document: dict[str, object] = {
        "endpoints": [
            _endpoint_document(link.left.node, link.left.interface),
            _endpoint_document(link.right.node, link.right.interface),
        ],
        "index": link.index,
        "kind": link.kind,
        "mtu": link.mtu,
    }
    if link.netem is not None:
        netem_document: dict[str, object] = {
            "delay_ms": link.netem.delay_ms,
            "jitter_ms": link.netem.jitter_ms,
            "loss_percent": link.netem.loss_percent,
        }
        if link.netem.rate is not None:
            netem_document["rate"] = link.netem.rate
        document["netem"] = netem_document
    if link.qdisc is not None:
        if isinstance(link.qdisc, TbfPlan):
            document["qdisc"] = {
                "burst": link.qdisc.burst_bytes,
                "kind": "tbf",
                "latency_ms": link.qdisc.latency_ms,
                "rate": link.qdisc.rate,
            }
        else:
            assert isinstance(link.qdisc, FqCodelPlan)
            document["qdisc"] = {
                "ecn": link.qdisc.ecn,
                "interval_ms": link.qdisc.interval_ms,
                "kind": "fq_codel",
                "limit": link.qdisc.limit,
                "target_ms": link.qdisc.target_ms,
            }
    return document


def _render_json(plan: TopologyPlan) -> str:
    document: dict[str, object] = {
        "links": [_link_document(link) for link in plan.links],
        "name": plan.name,
        "nodes": [_node_document(node) for node in plan.nodes.values()],
    }
    return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)


def render_graph(
    plan: TopologyPlan,
    output_format: str,
    *,
    detail: bool = False,
) -> str:
    supported_formats = {"tree", "box", "mermaid", "dot", "json"}
    if output_format not in supported_formats:
        raise NslabError(
            code="GRAPH_FORMAT_INVALID",
            message=f"unsupported graph format: {output_format}",
            details={"format": output_format},
        )
    if detail and output_format not in {"tree", "box"}:
        raise NslabError(
            code="GRAPH_DETAIL_UNSUPPORTED",
            message=f"graph detail is not supported for format: {output_format}",
            details={"format": output_format},
        )
    if output_format == "tree":
        return _render_tree(plan, detail=detail)
    if output_format == "box":
        return _render_box(plan, detail=detail)
    if output_format == "mermaid":
        return _render_mermaid(plan)
    if output_format == "dot":
        return _render_dot(plan)
    return _render_json(plan)
