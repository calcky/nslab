from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import traceback
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import NoReturn, cast

from nslab.backend.base import NetworkBackend
from nslab.completion import (
    COMPLETION_SHELLS,
    GRAPH_FORMATS,
    INSPECT_FORMATS,
    LIFECYCLE_COMMANDS,
    PUBLIC_COMMANDS,
    hidden_completion_candidates,
    render_completion_script,
)
from nslab.errors import NslabError, OperationCancelled
from nslab.executor import execute_in_node
from nslab.graph import render_graph
from nslab.inspector import InspectionReport, inspect_topology
from nslab.lifecycle import LifecycleService
from nslab.manifest import Manifest, load_manifest
from nslab.planner import TopologyPlan, compile_plan
from nslab.snapshot import validate_snapshot
from nslab.state import DeploymentLock, StateSnapshot, StateStore
from nslab.version import version_text

COMMANDS = PUBLIC_COMMANDS

STATE_ROOT = Path("/var/lib/nslab")
LOCK_ROOT = Path("/run/nslab")


@dataclass(frozen=True, slots=True)
class _TopologySelection:
    manifest: Manifest
    plan: TopologyPlan
    snapshot: StateSnapshot | None = None


def _add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-t", "--topo", type=Path, metavar="PATH")
    parser.add_argument("-n", "--name", metavar="NAME")
    parser.add_argument("--debug", action="store_true", default=argparse.SUPPRESS)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nslab")
    parser.add_argument(
        "--version",
        action="version",
        version=version_text(),
        help="show version and commit hash",
    )
    parser.add_argument("--debug", action="store_true", help="show exception tracebacks")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in LIFECYCLE_COMMANDS:
        command_parser = subparsers.add_parser(command)
        _add_selection_arguments(command_parser)

    inspect_parser = subparsers.add_parser("inspect")
    _add_selection_arguments(inspect_parser)
    inspect_parser.add_argument(
        "--format",
        choices=INSPECT_FORMATS,
        default="table",
        dest="output_format",
    )

    exec_parser = subparsers.add_parser("exec")
    _add_selection_arguments(exec_parser)
    exec_parser.add_argument("-N", "--node", required=True)
    exec_parser.add_argument("command_argv", nargs=argparse.REMAINDER)

    graph_parser = subparsers.add_parser("graph")
    _add_selection_arguments(graph_parser)
    graph_parser.add_argument(
        "--format",
        choices=GRAPH_FORMATS,
        default="tree",
        dest="output_format",
    )
    graph_parser.add_argument("--detail", action="store_true")

    completion_parser = subparsers.add_parser("completion")
    completion_parser.add_argument("shell", choices=COMPLETION_SHELLS)
    return parser


def _effective_uid() -> int:
    return os.geteuid()


def _make_state_store() -> StateStore:
    return StateStore(STATE_ROOT)


def _make_backend() -> NetworkBackend:
    from nslab.backend.pyroute2 import Pyroute2Backend

    return Pyroute2Backend()


def _make_lifecycle(backend: NetworkBackend, state_store: StateStore) -> LifecycleService:
    return LifecycleService(
        backend,
        state_store,
        lock_factory=lambda name: DeploymentLock(LOCK_ROOT, name),
    )


def _state_not_found(name: str) -> NslabError:
    return NslabError(
        code="DEPLOYMENT_NOT_FOUND",
        message=f"deployment state was not found: {name}",
        details={"name": name},
    )


def _selection_from_snapshot(snapshot: StateSnapshot) -> _TopologySelection:
    validated = validate_snapshot(snapshot)
    manifest = Manifest.model_validate(dict(snapshot.manifest))
    return _TopologySelection(
        manifest=manifest,
        plan=validated.plan,
        snapshot=snapshot,
    )


def _select_topology(
    arguments: argparse.Namespace,
    state_store: StateStore | None = None,
) -> _TopologySelection:
    command = cast(str, arguments.command)
    topology = cast(Path | None, getattr(arguments, "topo", None))
    name = cast(str | None, getattr(arguments, "name", None))

    if command != "deploy" and topology is None and name is not None:
        store = _make_state_store() if state_store is None else state_store
        snapshot = store.load(name)
        if snapshot is None:
            raise _state_not_found(name)
        return _selection_from_snapshot(snapshot)

    path = topology if topology is not None else Path.cwd() / "nslab.yaml"
    manifest = load_manifest(path)
    return _TopologySelection(
        manifest=manifest,
        plan=compile_plan(manifest, name_override=name),
    )


def _require_root(command: str) -> None:
    effective_uid = _effective_uid()
    if effective_uid == 0:
        return
    raise NslabError(
        code="PRIVILEGE_REQUIRED",
        message=f"{command} requires root privileges",
        details={"command": command, "euid": effective_uid},
    )


def _render_inspection_table(report: InspectionReport) -> str:
    rows = [
        ("NAME", "KIND", "STATUS", "NAMESPACE"),
        *((node.name, node.kind, node.status, node.namespace) for node in report.nodes),
    ]
    widths = tuple(max(len(row[index]) for row in rows) for index in range(4))
    lines = [f"status: {report.status}", ""]
    for row_index, row in enumerate(rows):
        lines.append("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
        if row_index == 0:
            lines.append("  ".join("-" * width for width in widths))
    return "\n".join(lines)


def _exec_argv(arguments: argparse.Namespace) -> tuple[str, ...]:
    raw = tuple(cast(Sequence[str], arguments.command_argv))
    node = cast(str, arguments.node)
    if not raw or raw[0] != "--":
        raise NslabError(
            code="EXEC_SEPARATOR_REQUIRED",
            message=f"execution command must follow -- for node: {node}",
            details={"node": node},
        )
    return raw[1:]


def _run_graph(arguments: argparse.Namespace) -> int:
    selection = _select_topology(arguments)
    print(
        render_graph(
            selection.plan,
            cast(str, arguments.output_format),
            detail=bool(arguments.detail),
        )
    )
    return 0


def _run_live_command(arguments: argparse.Namespace) -> int:
    command = cast(str, arguments.command)
    _require_root(command)
    state_store = _make_state_store()
    selection = _select_topology(arguments, state_store)
    backend = _make_backend()

    if command in {"deploy", "destroy", "redeploy"}:
        lifecycle = _make_lifecycle(backend, state_store)
        if command == "deploy":
            lifecycle_result = lifecycle.deploy(selection.plan, selection.manifest)
        elif command == "destroy":
            lifecycle_result = lifecycle.destroy(selection.plan, selection.plan.name)
        else:
            lifecycle_result = lifecycle.redeploy(selection.plan, selection.manifest)
        print(lifecycle_result.message)
        return 0

    if command == "inspect":
        snapshot = (
            selection.snapshot
            if selection.snapshot is not None
            else state_store.load(selection.plan.name)
        )
        report = inspect_topology(
            selection.plan,
            snapshot,
            backend.inventory(selection.plan),
        )
        if arguments.output_format == "json":
            print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print(_render_inspection_table(report))
        return 0

    if command == "exec":
        exec_result = execute_in_node(
            backend,
            selection.plan,
            cast(str, arguments.node),
            _exec_argv(arguments),
            capture_output=False,
        )
        return exec_result.returncode

    raise AssertionError(f"unsupported live command: {command}")


def _dispatch(arguments: argparse.Namespace) -> int:
    if arguments.command == "completion":
        print(render_completion_script(cast(str, arguments.shell)), end="")
        return 0
    if arguments.command == "graph":
        return _run_graph(arguments)
    return _run_live_command(arguments)


def _json_output_requested(arguments: argparse.Namespace) -> bool:
    return getattr(arguments, "output_format", None) == "json"


def _print_error(error: NslabError, *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(error.as_dict(), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
    else:
        print(f"{error.code}: {error.message}", file=sys.stderr)


@contextmanager
def _sigterm_cancellation() -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    previous = signal.getsignal(signal.SIGTERM)

    def cancel(signum: int, frame: FrameType | None) -> NoReturn:
        del frame
        raise OperationCancelled(
            message="operation cancelled by SIGTERM",
            details={"signal": signum},
        )

    signal.signal(signal.SIGTERM, cancel)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = tuple(sys.argv[1:] if argv is None else argv)
    if raw_arguments[:1] == ("__complete",):
        try:
            candidates = hidden_completion_candidates(
                raw_arguments[1:],
                cwd=Path.cwd(),
                state_root=STATE_ROOT,
            )
            for candidate in candidates:
                print(candidate)
        except Exception:
            pass
        return 0

    parser = _build_parser()
    try:
        arguments = parser.parse_args(raw_arguments)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 1

    json_output = _json_output_requested(arguments)
    debug = bool(arguments.debug)
    try:
        with _sigterm_cancellation():
            return _dispatch(arguments)
    except NslabError as error:
        _print_error(error, json_output=json_output)
        return 1
    except KeyboardInterrupt:
        cancelled = OperationCancelled(message="operation interrupted")
        _print_error(cancelled, json_output=json_output)
        return 130
    except Exception as error:
        if debug:
            traceback.print_exc()
        internal = NslabError(
            code="INTERNAL_ERROR",
            message="unexpected internal error",
            details={"type": type(error).__name__},
        )
        _print_error(internal, json_output=json_output)
        return 1


def entrypoint() -> NoReturn:
    raise SystemExit(main())
