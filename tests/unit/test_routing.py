from __future__ import annotations

import json
import os
from contextlib import nullcontext
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from pathlib import Path
from unittest.mock import Mock, call

import pytest
import yaml
from pydantic import ValidationError

import nslab.backend.pyroute2 as pyroute2_module
import nslab.routing as routing_module
from nslab.backend.base import InterfaceInventory
from nslab.backend.fake import FakeNetworkBackend
from nslab.backend.pyroute2 import Pyroute2Backend
from nslab.errors import NslabError
from nslab.lifecycle import LifecycleService
from nslab.manifest import Manifest, load_manifest
from nslab.planner import RoutePlan, compile_plan
from nslab.routing import FrrRuntime, render_frr_config
from nslab.state import StateStore

_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def _plan(example: str):
    manifest = load_manifest(_EXAMPLES / example / "nslab.yaml")
    return manifest, compile_plan(manifest)


def test_render_ospf_config_uses_connected_networks_and_passive_interfaces() -> None:
    _, plan = _plan("ospf")

    config = render_frr_config(plan.nodes["r1"])

    assert "router ospf\n" in config
    assert " ospf router-id 1.1.1.1\n" in config
    assert " network 10.0.12.0/30 area 0.0.0.0\n" in config
    assert " passive-interface eth2\n" in config
    assert "router bgp" not in config


def test_render_ospf_config_marks_direct_router_links_point_to_point() -> None:
    _, plan = _plan("ospf")

    config = render_frr_config(plan.nodes["r1"], plan)

    assert "interface eth0\n ip ospf network point-to-point\n!\n" in config
    assert "interface eth1\n ip ospf network point-to-point\n!\n" in config
    assert "interface eth2\n ip ospf network point-to-point\n!\n" not in config


def test_render_bgp_config_emits_neighbors_and_ipv4_networks() -> None:
    _, plan = _plan("bgp")

    config = render_frr_config(plan.nodes["r1"])

    assert "router bgp 65001\n" in config
    assert " bgp router-id 1.1.1.1\n" in config
    assert " neighbor 10.1.12.2 remote-as 65002\n" in config
    assert " address-family ipv4 unicast\n" in config
    assert "  network 198.18.1.0/24\n" in config
    assert "  neighbor 10.1.12.2 activate\n" in config
    assert "router ospf" not in config


@pytest.mark.parametrize(
    ("node", "field", "value"),
    [
        ("r1", "sysctls", {}),
        ("r2", "sysctls", {"net.ipv4.ip_forward": 1}),
    ],
)
def test_manifest_rejects_invalid_dynamic_routing_requirements(
    node: str, field: str, value: object
) -> None:
    document = yaml.safe_load((_EXAMPLES / "ospf" / "nslab.yaml").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    node_document = document["topology"]["nodes"][node]
    node_document[field] = value
    if node == "r2":
        node_document["routing"]["ospf"]["passive_interfaces"] = ["eth9"]

    with pytest.raises(ValidationError, match="dynamic routing|passive interface"):
        Manifest.model_validate(document)


def test_manifest_rejects_duplicate_ospf_router_ids() -> None:
    document = yaml.safe_load((_EXAMPLES / "ospf" / "nslab.yaml").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    document["topology"]["nodes"]["r2"]["routing"]["ospf"]["router_id"] = "1.1.1.1"

    with pytest.raises(ValidationError, match="duplicate OSPF router_id"):
        Manifest.model_validate(document)


def test_manifest_rejects_bgp_neighbor_outside_a_connected_network() -> None:
    document = yaml.safe_load((_EXAMPLES / "bgp" / "nslab.yaml").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    neighbor = document["topology"]["nodes"]["r1"]["routing"]["bgp"]["neighbors"][0]
    neighbor["address"] = "192.0.2.1"

    with pytest.raises(ValidationError, match="not directly connected"):
        Manifest.model_validate(document)


@dataclass
class _FakeProcess:
    pid: int
    returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


def test_frr_runtime_start_is_idempotent_and_stop_removes_runtime_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, plan = _plan("ospf")
    live: set[int] = set()
    processes: list[_FakeProcess] = []
    namespace_calls: list[str] = []
    argv_calls: list[tuple[object, ...]] = []

    def process_factory(*args: object, **kwargs: object) -> _FakeProcess:
        argv_calls.append(tuple(args[0]) if args else ())
        preexec = kwargs.get("preexec_fn")
        if callable(preexec):
            preexec()
        process = _FakeProcess(100_000 + len(processes))
        processes.append(process)
        live.add(process.pid)
        return process

    def kill_group(pid: int, _signal: int) -> None:
        live.discard(pid)

    monkeypatch.setattr(routing_module.os, "killpg", kill_group)
    runtime = FrrRuntime(
        runtime_root=tmp_path / "runtime",
        frr_state_root=tmp_path / "frr-state",
        process_factory=process_factory,
        binary_resolver=lambda daemon: f"/usr/lib/frr/{daemon}",
        namespace_setter=namespace_calls.append,
        require_zebra_socket=False,
        pid_exists=lambda pid: pid in live,
        frr_config_root=tmp_path / "frr-config",
    )

    runtime.start(plan)

    assert len(processes) == 6
    assert len(namespace_calls) == 6
    assert all(
        argv[argv.index("--vty_socket") + 1]
        == str(tmp_path / "frr-state" / f"nslab-{plan.name}-r1")
        for argv in argv_calls[:2]
    )
    assert runtime.ready(plan)
    metadata_path = tmp_path / "runtime" / plan.name / "r1" / "processes.json"
    document = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert {record["daemon"] for record in document["processes"]} == {"zebra", "ospfd"}
    assert (tmp_path / "runtime" / plan.name / ".nslab-marker").read_text(
        encoding="ascii"
    ) == runtime._runtime_marker(plan)
    assert (tmp_path / "frr-state" / "nslab-ospf-r1" / ".nslab-marker").read_text(
        encoding="ascii"
    ) == runtime._state_marker(plan, plan.nodes["r1"])

    runtime.start(plan)
    assert len(processes) == 6

    runtime.stop(plan)

    assert not runtime.ready(plan)
    assert not (tmp_path / "runtime" / plan.name).exists()
    assert not (tmp_path / "frr-state" / "nslab-ospf-r1").exists()


def test_frr_runtime_rejects_unmanaged_state_pathspace(tmp_path: Path) -> None:
    _, plan = _plan("ospf")
    state_dir = tmp_path / "frr-state" / "nslab-ospf-r1"
    state_dir.mkdir(parents=True)
    (state_dir / "foreign.sock").write_text("owned elsewhere", encoding="utf-8")
    runtime = FrrRuntime(
        runtime_root=tmp_path / "runtime",
        frr_state_root=tmp_path / "frr-state",
        frr_config_root=tmp_path / "frr-config",
        require_zebra_socket=False,
    )

    with pytest.raises(NslabError, match="not managed by nslab"):
        runtime.stop(plan)
    assert state_dir.exists()


def test_frr_runtime_rejects_marker_symlink(tmp_path: Path) -> None:
    _, plan = _plan("ospf")
    state_dir = tmp_path / "frr-state" / "nslab-ospf-r1"
    state_dir.mkdir(parents=True)
    target = tmp_path / "foreign-marker"
    target.write_text("foreign", encoding="ascii")
    (state_dir / ".nslab-marker").symlink_to(target)
    runtime = FrrRuntime(
        runtime_root=tmp_path / "runtime",
        frr_state_root=tmp_path / "frr-state",
        frr_config_root=tmp_path / "frr-config",
        require_zebra_socket=False,
    )

    with pytest.raises(NslabError, match="marker is a symlink"):
        runtime.stop(plan)
    assert state_dir.is_dir()


def test_frr_runtime_prepares_vty_pathspace_with_matching_marker(tmp_path: Path) -> None:
    _, plan = _plan("ospf")
    node = plan.nodes["r1"]
    runtime = FrrRuntime(
        runtime_root=tmp_path / "runtime",
        frr_state_root=tmp_path / "frr-state",
        frr_config_root=tmp_path / "frr-config",
        require_zebra_socket=False,
    )
    identity = routing_module._FrrIdentity(uid=os.getuid(), gid=os.getgid())

    runtime._prepare_vty_config(plan, node, identity)

    config_dir = tmp_path / "frr-config" / "nslab-ospf-r1"
    assert (config_dir / ".nslab-marker").read_text(encoding="ascii") == runtime._vty_config_marker(
        plan, node
    )
    assert "ip ospf network point-to-point" in (config_dir / "frr.conf").read_text(encoding="utf-8")


def test_frr_runtime_prepares_injected_vty_root_without_system_identity(tmp_path: Path) -> None:
    _, plan = _plan("bgp")
    node = plan.nodes["r1"]
    runtime = FrrRuntime(
        runtime_root=tmp_path / "runtime",
        frr_state_root=tmp_path / "frr-state",
        frr_config_root=tmp_path / "frr-config",
        require_zebra_socket=False,
    )

    runtime._prepare_vty_config(plan, node, None)

    assert (tmp_path / "frr-config" / "nslab-bgp-r1" / ".nslab-marker").is_file()


def test_frr_runtime_uses_resolved_uid_for_owned_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "owned"
    path.mkdir()
    identity = routing_module._FrrIdentity(uid=1234, gid=2345)
    chown = Mock()
    chmod = Mock()
    monkeypatch.setattr(routing_module.os, "chown", chown)
    monkeypatch.setattr(routing_module.os, "chmod", chmod)

    FrrRuntime._prepare_owned_directory(path, identity)
    FrrRuntime._prepare_owned_file(path, identity, 0o640)

    assert chown.call_args_list == [
        call(path, 1234, 2345),
        call(path, 1234, 2345),
    ]
    assert chmod.call_args_list == [call(path, 0o755), call(path, 0o640)]


def test_pyroute2_backend_passes_frr_config_root_to_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class _Runtime:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(pyroute2_module, "FrrRuntime", _Runtime)
    Pyroute2Backend(
        routing_root=tmp_path / "runtime",
        frr_state_root=tmp_path / "state",
        frr_config_root=tmp_path / "config",
    )

    assert captured == {
        "runtime_root": tmp_path / "runtime",
        "frr_state_root": tmp_path / "state",
        "frr_config_root": tmp_path / "config",
    }


def test_frr_runtime_treats_corrupt_metadata_as_not_ready_and_can_clean_pid_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, plan = _plan("bgp")
    node = plan.nodes["r1"]
    node_dir = tmp_path / "runtime" / plan.name / node.name
    node_dir.mkdir(parents=True)
    (node_dir / "processes.json").write_text("{broken", encoding="utf-8")
    (node_dir / "zebra.pid").write_text("100001\n", encoding="ascii")

    live = {100001}
    monkeypatch.setattr(routing_module.os, "killpg", lambda pid, _signal: live.discard(pid))
    runtime = FrrRuntime(
        runtime_root=tmp_path / "runtime",
        frr_state_root=tmp_path / "frr-state",
        require_zebra_socket=False,
        pid_exists=lambda pid: pid in live,
    )

    assert runtime.ready(plan) is False
    runtime.stop(plan)
    assert not (tmp_path / "runtime" / plan.name).exists()


def test_dynamic_route_inventory_accepts_supported_frr_routes_and_skips_unrepresentable_ones() -> (
    None
):
    _, plan = _plan("ospf")
    node = plan.nodes["r1"]
    interface = InterfaceInventory(
        name="eth0",
        kind="veth",
        ifindex=10,
        master=None,
        mtu=1500,
        up=True,
        addresses=(IPv4Interface("10.0.12.1/30"),),
    )
    route = {
        "table": 254,
        "type": 1,
        "dst_len": 24,
        "src_len": 0,
        "tos": 0,
        "flags": 0,
        "proto": 188,
        "scope": 0,
        "attrs": [
            ("RTA_DST", "192.0.3.0"),
            ("RTA_GATEWAY", "10.0.12.2"),
            ("RTA_OIF", 10),
        ],
    }
    unsupported_route = {
        **route,
        "attrs": [*route["attrs"], ("RTA_METRICS", {"mtu": 1400})],
    }

    routes = Pyroute2Backend._inventory_routes(
        [route, unsupported_route],
        {10: "eth0"},
        {"eth0": interface},
        node.namespace,
        allow_dynamic=True,
    )

    assert routes == (RoutePlan(IPv4Network("192.0.3.0/24"), IPv4Address("10.0.12.2"), "eth0"),)

    with pytest.raises(NslabError, match="unsupported route"):
        Pyroute2Backend._inventory_routes(
            [unsupported_route],
            {10: "eth0"},
            {"eth0": interface},
            node.namespace,
        )


def test_fake_lifecycle_starts_routing_after_interfaces_and_stops_before_namespaces(
    tmp_path: Path,
) -> None:
    manifest, plan = _plan("ospf")
    backend = FakeNetworkBackend()
    service = LifecycleService(
        backend,
        StateStore(tmp_path),
        lock_factory=lambda _name: nullcontext(),
    )

    service.deploy(plan, manifest)
    start_index = backend.calls.index(("start_routing", plan.name))
    configure_indices = [
        index for index, call in enumerate(backend.calls) if call[0] == "configure_node"
    ]
    assert start_index > max(configure_indices)

    no_op = service.deploy(plan, manifest)
    assert no_op.changed is False

    backend.calls.clear()
    service.destroy(plan, plan.name)
    stop_index = backend.calls.index(("stop_routing", plan.name))
    delete_index = next(
        index for index, call in enumerate(backend.calls) if call[0] == "delete_namespace"
    )
    assert stop_index < delete_index
