from __future__ import annotations

import json
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

import nslab.cli as cli
from nslab.backend.base import ExecResult, LiveInventory
from nslab.lifecycle import LifecycleResult
from nslab.manifest import Manifest, normalized_manifest
from nslab.planner import TopologyPlan, compile_plan
from nslab.state import StateSnapshot


def _write_manifest(path: Path, *, name: str = "bridge-fdb") -> Manifest:
    path.write_text(
        f"""\
version: 1
name: {name}
topology:
  nodes:
    h1:
      kind: linux
      interfaces:
        eth0:
          addresses: [10.10.0.1/24]
    sw1:
      kind: bridge
      bridge:
        name: br0
        stp: false
        vlan_filtering: false
    h2:
      kind: linux
      interfaces:
        eth0:
          addresses: [10.10.0.2/24]
  links:
    - endpoints: [h1:eth0, sw1:swp1]
      mtu: 1500
    - endpoints: [h2:eth0, sw1:swp2]
      mtu: 1500
""",
        encoding="utf-8",
    )
    from nslab.manifest import load_manifest

    return load_manifest(path)


def _snapshot(manifest: Manifest, *, name: str | None = None) -> StateSnapshot:
    plan = compile_plan(manifest, name_override=name)
    interfaces: dict[str, object] = {}
    for node in plan.nodes.values():
        if node.kind == "bridge":
            assert node.bridge_name is not None
            interfaces[f"{node.name}:{node.bridge_name}"] = {
                "name": node.bridge_name,
                "kind": "bridge",
                "namespace": node.namespace,
                "ifindex": 10,
            }
    for link in plan.links:
        for endpoint in (link.left, link.right):
            interfaces[f"{endpoint.node}:{endpoint.interface}"] = {
                "name": endpoint.interface,
                "kind": "veth",
                "namespace": endpoint.namespace,
                "temporary_name": endpoint.temporary_name,
                "ifindex": 20 + link.index,
                "link_id": f"test-link-{link.index}",
            }
    return StateSnapshot(
        schema=1,
        name=plan.name,
        fingerprint=plan.fingerprint,
        manifest=normalized_manifest(manifest),
        namespaces={node_name: node.namespace for node_name, node in plan.nodes.items()},
        interfaces=interfaces,
        created_at="2026-09-01T12:00:00+00:00",
        status="deployed",
    )


class _MemoryStore:
    def __init__(self, snapshot: StateSnapshot | None = None) -> None:
        self.root = Path("/test/state")
        self.snapshot = snapshot
        self.loads: list[str] = []

    def load(self, name: str) -> StateSnapshot | None:
        self.loads.append(name)
        if self.snapshot is None or self.snapshot.name != name:
            return None
        return self.snapshot


@dataclass
class _LifecycleRecorder:
    calls: list[tuple[str, object, object | None]]

    def deploy(self, plan: TopologyPlan, manifest: Manifest) -> LifecycleResult:
        self.calls.append(("deploy", plan, manifest))
        return LifecycleResult("deploy", plan.name, True, "deployed", f"deployed {plan.name}")

    def destroy(self, plan: TopologyPlan | None, name: str) -> LifecycleResult:
        self.calls.append(("destroy", plan, name))
        return LifecycleResult("destroy", name, True, "absent", f"destroyed {name}")

    def redeploy(self, plan: TopologyPlan, manifest: Manifest) -> LifecycleResult:
        self.calls.append(("redeploy", plan, manifest))
        return LifecycleResult(
            "redeploy",
            plan.name,
            True,
            "deployed",
            f"redeployed {plan.name}",
        )


class _InspectionReport:
    status = "deployed"
    nodes = (
        SimpleNamespace(name="h1", kind="linux", status="matching", namespace="ns-h1"),
        SimpleNamespace(name="sw1", kind="bridge", status="matching", namespace="ns-sw1"),
        SimpleNamespace(name="h2", kind="linux", status="matching", namespace="ns-h2"),
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "nodes": [
                {
                    "name": node.name,
                    "kind": node.kind,
                    "status": node.status,
                    "namespace": node.namespace,
                }
                for node in self.nodes
            ],
            "differences": [],
        }


def _patch_euid(monkeypatch: pytest.MonkeyPatch, value: int) -> None:
    monkeypatch.setattr(cli, "_effective_uid", lambda: value, raising=False)


def _patch_live_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    lifecycle: _LifecycleRecorder | None = None,
    store: _MemoryStore | None = None,
    backend: Any | None = None,
) -> tuple[_LifecycleRecorder, _MemoryStore, Any]:
    selected_lifecycle = lifecycle or _LifecycleRecorder([])
    selected_store = store or _MemoryStore()
    selected_backend = backend or Mock()
    monkeypatch.setattr(cli, "_make_state_store", lambda: selected_store, raising=False)
    monkeypatch.setattr(cli, "_make_backend", lambda: selected_backend, raising=False)
    monkeypatch.setattr(
        cli,
        "_make_lifecycle",
        lambda actual_backend, actual_store: selected_lifecycle,
        raising=False,
    )
    return selected_lifecycle, selected_store, selected_backend


def test_graph_uses_only_current_directory_default_without_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_manifest(tmp_path / "nslab.yaml")
    monkeypatch.chdir(tmp_path)
    _patch_euid(monkeypatch, 1000)
    monkeypatch.setattr(
        cli,
        "_make_backend",
        lambda: (_ for _ in ()).throw(AssertionError("graph initialized backend")),
        raising=False,
    )

    assert cli.main(["graph"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("Topology: bridge-fdb\n\n")
    assert "sw1 [bridge · br0]\n" in output
    assert "10.10.0.1/24" in output
    assert "stp " not in output
    assert "vlan filtering " not in output
    assert "h1 [linux]" in output


def test_graph_detail_renders_bridge_state_without_root_or_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_manifest(tmp_path / "nslab.yaml")
    monkeypatch.chdir(tmp_path)
    _patch_euid(monkeypatch, 1000)
    monkeypatch.setattr(
        cli,
        "_make_backend",
        lambda: (_ for _ in ()).throw(AssertionError("graph initialized backend")),
        raising=False,
    )

    assert cli.main(["graph", "--detail"]) == 0
    output = capsys.readouterr().out
    assert "sw1 [bridge · br0 · stp off · vlan filtering off]" in output
    assert "10.10.0.1/24" in output


@pytest.mark.parametrize(
    "arguments",
    [
        ["graph", "--detail", "--format", "box"],
        ["graph", "--format", "box", "--detail"],
    ],
)
def test_graph_detail_accepts_either_option_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    _write_manifest(tmp_path / "nslab.yaml")
    monkeypatch.chdir(tmp_path)
    _patch_euid(monkeypatch, 1000)

    assert cli.main(arguments) == 0
    assert "bridge · br0 · stp off · vlan filtering off" in capsys.readouterr().out


def test_graph_json_rejects_detail_with_structured_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_manifest(tmp_path / "nslab.yaml")
    monkeypatch.chdir(tmp_path)
    _patch_euid(monkeypatch, 1000)

    assert cli.main(["graph", "--format", "json", "--detail"]) == 1
    document = json.loads(capsys.readouterr().err)
    assert document == {
        "code": "GRAPH_DETAIL_UNSUPPORTED",
        "details": {"format": "json"},
        "message": "graph detail is not supported for format: json",
    }


def test_graph_help_lists_detail_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        cli._build_parser().parse_args(["graph", "--help"])

    assert caught.value.code == 0
    assert "--detail" in capsys.readouterr().out


def test_graph_explicit_mermaid_preserves_export_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_manifest(tmp_path / "nslab.yaml")
    monkeypatch.chdir(tmp_path)
    _patch_euid(monkeypatch, 1000)

    assert cli.main(["graph", "--format", "mermaid"]) == 0
    assert capsys.readouterr().out.startswith("flowchart LR\n")


@pytest.mark.parametrize("selector", ["-t", "--topo"])
def test_graph_resolves_explicit_topology_after_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    selector: str,
) -> None:
    topology = tmp_path / "nested" / "lab.yaml"
    topology.parent.mkdir()
    _write_manifest(topology)
    empty_cwd = tmp_path / "cwd"
    empty_cwd.mkdir()
    monkeypatch.chdir(empty_cwd)
    _patch_euid(monkeypatch, 1000)

    assert cli.main(["graph", selector, str(topology), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "bridge-fdb"


def test_missing_default_reports_absolute_current_directory_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_euid(monkeypatch, 1000)

    assert cli.main(["graph"]) == 1

    error = capsys.readouterr().err
    assert "MANIFEST_INVALID" in error
    assert str((tmp_path / "nslab.yaml").resolve()) in error


@pytest.mark.parametrize("selector", ["-n", "--name"])
def test_deploy_name_only_loads_default_file_and_overrides_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    selector: str,
) -> None:
    _write_manifest(tmp_path / "nslab.yaml")
    monkeypatch.chdir(tmp_path)
    _patch_euid(monkeypatch, 0)
    lifecycle, _, _ = _patch_live_dependencies(monkeypatch)

    assert cli.main(["deploy", selector, "demo"]) == 0

    command, plan, loaded_manifest = lifecycle.calls[0]
    assert command == "deploy"
    assert isinstance(plan, TopologyPlan)
    assert isinstance(loaded_manifest, Manifest)
    assert plan.name == "demo"
    assert loaded_manifest.name == "bridge-fdb"
    assert "deployed demo" in capsys.readouterr().out


def test_non_deploy_name_only_loads_stored_normalized_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _write_manifest(tmp_path / "source.yaml")
    snapshot = _snapshot(manifest, name="stored")
    store = _MemoryStore(snapshot)
    monkeypatch.setattr(cli, "_make_state_store", lambda: store, raising=False)
    _patch_euid(monkeypatch, 1000)

    assert cli.main(["graph", "--name", "stored", "--format", "json"]) == 0

    document = json.loads(capsys.readouterr().out)
    assert document["name"] == "stored"
    assert store.loads == ["stored"]


def test_explicit_topology_and_name_use_file_with_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    topology = tmp_path / "source.yaml"
    _write_manifest(topology)
    monkeypatch.setattr(
        cli,
        "_make_state_store",
        lambda: (_ for _ in ()).throw(AssertionError("file selection loaded state")),
        raising=False,
    )
    _patch_euid(monkeypatch, 1000)

    assert cli.main(["graph", "--topo", str(topology), "--name", "demo", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "demo"


@pytest.mark.parametrize("command", ["deploy", "destroy", "redeploy"])
def test_lifecycle_commands_dispatch_to_matching_service_method(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    topology = tmp_path / "lab.yaml"
    _write_manifest(topology)
    _patch_euid(monkeypatch, 0)
    lifecycle, _, _ = _patch_live_dependencies(monkeypatch)

    assert cli.main([command, "-t", str(topology)]) == 0

    assert lifecycle.calls[0][0] == command
    plan = lifecycle.calls[0][1]
    assert isinstance(plan, TopologyPlan)
    assert plan.name == "bridge-fdb"
    assert command in capsys.readouterr().out


@pytest.mark.parametrize("output_format", ["table", "json"])
def test_inspect_output_contains_status_and_ordered_nodes_without_ansi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    output_format: str,
) -> None:
    topology = tmp_path / "lab.yaml"
    _write_manifest(topology)
    _patch_euid(monkeypatch, 0)
    backend = Mock()
    backend.inventory.return_value = LiveInventory(namespaces={})
    _patch_live_dependencies(monkeypatch, backend=backend)
    monkeypatch.setattr(cli, "inspect_topology", lambda *args: _InspectionReport(), raising=False)

    assert cli.main(["inspect", "-t", str(topology), "--format", output_format]) == 0

    output = capsys.readouterr().out
    assert "\x1b" not in output
    if output_format == "json":
        document = json.loads(output)
        assert document["status"] == "deployed"
        assert [node["name"] for node in document["nodes"]] == ["h1", "sw1", "h2"]
    else:
        assert "deployed" in output
        assert output.index("h1") < output.index("sw1") < output.index("h2")


def test_exec_selects_passthrough_does_not_replay_and_returns_child_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    topology = tmp_path / "lab.yaml"
    _write_manifest(topology)
    _patch_euid(monkeypatch, 0)
    _, _, backend = _patch_live_dependencies(monkeypatch)
    calls: list[tuple[Any, TopologyPlan, str, tuple[str, ...], bool]] = []

    def execute(
        actual_backend: Any,
        plan: TopologyPlan,
        node: str,
        argv: tuple[str, ...],
        *,
        capture_output: bool = True,
    ) -> ExecResult:
        calls.append((actual_backend, plan, node, argv, capture_output))
        sys.stdout.write("packet out\n")
        sys.stderr.write("packet err\n")
        return ExecResult(
            argv=argv,
            returncode=7,
            stdout="must not replay stdout\n",
            stderr="must not replay stderr\n",
        )

    monkeypatch.setattr(cli, "execute_in_node", execute, raising=False)

    result = cli.main(
        [
            "exec",
            "-t",
            str(topology),
            "--node",
            "h1",
            "--",
            "ping",
            "-c",
            "1",
            "10.10.0.2",
        ]
    )

    assert result == 7
    assert calls[0][0] is backend
    assert calls[0][2:] == (
        "h1",
        ("ping", "-c", "1", "10.10.0.2"),
        False,
    )
    captured = capsys.readouterr()
    assert captured.out == "packet out\n"
    assert captured.err == "packet err\n"


def test_exec_keyboard_interrupt_returns_130_with_one_cancellation_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    topology = tmp_path / "lab.yaml"
    _write_manifest(topology)
    _patch_euid(monkeypatch, 0)
    _patch_live_dependencies(monkeypatch)
    monkeypatch.setattr(
        cli,
        "execute_in_node",
        Mock(side_effect=KeyboardInterrupt),
        raising=False,
    )

    result = cli.main(
        [
            "exec",
            "-t",
            str(topology),
            "--node",
            "h1",
            "--",
            "iperf3",
            "-s",
        ]
    )

    captured = capsys.readouterr()
    assert result == 130
    assert captured.out == ""
    assert captured.err == "OPERATION_CANCELLED: operation interrupted\n"


@pytest.mark.parametrize(
    "argv",
    [
        ["deploy"],
        ["destroy"],
        ["redeploy"],
        ["inspect"],
        ["exec", "--node", "h1", "--", "true"],
    ],
)
def test_live_commands_require_root_before_loading_or_backend_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    _write_manifest(tmp_path / "nslab.yaml")
    monkeypatch.chdir(tmp_path)
    _patch_euid(monkeypatch, 1000)
    monkeypatch.setattr(
        cli,
        "_make_backend",
        lambda: (_ for _ in ()).throw(AssertionError("backend constructed without root")),
        raising=False,
    )

    assert cli.main(argv) == 1
    assert "PRIVILEGE_REQUIRED" in capsys.readouterr().err


@pytest.mark.parametrize("debug", [False, True])
def test_debug_controls_unexpected_exception_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    debug: bool,
) -> None:
    topology = tmp_path / "lab.yaml"
    _write_manifest(topology)
    _patch_euid(monkeypatch, 0)
    monkeypatch.setattr(
        cli,
        "_make_backend",
        lambda: (_ for _ in ()).throw(RuntimeError("backend exploded")),
        raising=False,
    )
    argv = ["deploy", "-t", str(topology)]
    if debug:
        argv.insert(0, "--debug")

    assert cli.main(argv) == 1

    error = capsys.readouterr().err
    assert ("Traceback (most recent call last)" in error) is debug
    assert "INTERNAL_ERROR" in error


def test_json_capable_command_prints_structured_error_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "missing.yaml"
    _patch_euid(monkeypatch, 1000)

    assert cli.main(["graph", "-t", str(missing), "--format", "json"]) == 1

    document = json.loads(capsys.readouterr().err)
    assert document["code"] == "MANIFEST_INVALID"
    assert document["details"]["path"] == str(missing.resolve())


def test_sigterm_handler_is_restored_before_main_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = tmp_path / "lab.yaml"
    _write_manifest(topology)
    _patch_euid(monkeypatch, 1000)
    previous = object()
    calls: list[tuple[signal.Signals, object]] = []
    monkeypatch.setattr(signal, "getsignal", lambda selected: previous)
    monkeypatch.setattr(
        signal, "signal", lambda selected, handler: calls.append((selected, handler))
    )

    assert cli.main(["graph", "-t", str(topology)]) == 0

    assert calls[0][0] == signal.SIGTERM
    assert callable(calls[0][1])
    assert calls[-1] == (signal.SIGTERM, previous)
