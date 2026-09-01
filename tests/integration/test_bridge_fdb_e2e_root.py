from __future__ import annotations

import json
import os
import signal
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import nslab.cli as cli
from nslab.backend.pyroute2 import Pyroute2Backend
from nslab.errors import NslabError
from nslab.manifest import load_manifest
from nslab.planner import compile_plan

pytestmark = pytest.mark.root

_IS_LINUX = sys.platform.startswith("linux")
_IS_ROOT = getattr(os, "geteuid", lambda: -1)() == 0
_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "bridge-fdb" / "nslab.yaml"


@contextmanager
def external_timeout(seconds: int) -> Iterator[None]:
    def raise_timeout(_signum: int, _frame: object) -> None:
        raise TimeoutError(f"operation exceeded {seconds} seconds")

    previous_handler = signal.signal(signal.SIGALRM, raise_timeout)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


@pytest.mark.skipif(not _IS_LINUX, reason="requires Linux network namespaces")
@pytest.mark.skipif(not _IS_ROOT, reason="requires effective UID 0")
def test_bridge_fdb_cli_lifecycle_is_repeatable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    deployment = f"e2e-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    manifest = load_manifest(_EXAMPLE)
    plan = compile_plan(manifest, name_override=deployment)
    backend = Pyroute2Backend()
    selectors = ["--topo", str(_EXAMPLE), "--name", deployment]

    monkeypatch.setattr(cli, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(cli, "LOCK_ROOT", tmp_path / "lock")

    def cleanup_namespaces() -> None:
        cleanup_errors: list[BaseException] = []
        for node in reversed(tuple(plan.nodes.values())):
            try:
                backend.delete_namespace(node.namespace)
            except NslabError as error:
                if error.code != "RESOURCE_MISSING":
                    cleanup_errors.append(error)
            except BaseException as error:
                cleanup_errors.append(error)
        if cleanup_errors and sys.exc_info()[0] is None:
            raise cleanup_errors[0]

    try:
        with external_timeout(30):
            assert cli.main(["graph", *selectors, "--format", "mermaid"]) == 0
        graph_output = capsys.readouterr().out
        assert graph_output.startswith("flowchart LR\n")

        with external_timeout(30):
            assert cli.main(["deploy", *selectors]) == 0
        assert f"deployed topology: {deployment}" in capsys.readouterr().out

        with external_timeout(30):
            assert cli.main(["deploy", *selectors]) == 0
        assert f"topology already deployed: {deployment}" in capsys.readouterr().out

        with external_timeout(30):
            assert cli.main(["inspect", "--name", deployment, "--format", "json"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["status"] == "deployed"
        assert {node["name"] for node in report["nodes"]} == {"h1", "sw1", "h2"}

        with external_timeout(30):
            assert (
                cli.main(
                    [
                        "exec",
                        "--name",
                        deployment,
                        "--node",
                        "h1",
                        "--",
                        "/usr/bin/ping",
                        "-c",
                        "1",
                        "10.10.0.2",
                    ]
                )
                == 0
            )
        assert "1 received" in capsys.readouterr().out

        with external_timeout(30):
            assert cli.main(["redeploy", *selectors]) == 0
        assert f"redeployed topology: {deployment}" in capsys.readouterr().out

        with external_timeout(30):
            assert cli.main(["destroy", *selectors]) == 0
        assert f"destroyed topology: {deployment}" in capsys.readouterr().out

        with external_timeout(30):
            assert cli.main(["destroy", *selectors]) == 0
        assert f"topology already absent: {deployment}" in capsys.readouterr().out
    finally:
        cleanup_namespaces()

    try:
        inventory = backend.inventory(plan)
        assert inventory.root_interfaces == {}
        assert all(not observed.exists for observed in inventory.namespaces.values())
    finally:
        cleanup_namespaces()
