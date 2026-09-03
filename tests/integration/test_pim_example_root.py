from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from types import FrameType

import pytest

from nslab.backend.base import ExecResult, inventory_matches_plan
from nslab.backend.pyroute2 import Pyroute2Backend
from nslab.errors import NslabError
from nslab.lifecycle import LifecycleService
from nslab.manifest import load_manifest
from nslab.planner import TopologyPlan, compile_plan
from nslab.routing import routing_pathspace
from nslab.state import StateStore

pytestmark = pytest.mark.root

_IS_LINUX = os.name == "posix" and Path("/proc/self/ns/net").exists()
_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
_HAS_PIMD = Path("/usr/lib/frr/pimd").is_file()
_IP = shutil.which("ip")
_EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "pim"
_EXAMPLE = _EXAMPLE_DIR / "nslab.yaml"
_RECEIVER = _EXAMPLE_DIR / "multicast_receive.py"
_SENDER = _EXAMPLE_DIR / "multicast_send.py"


@contextmanager
def external_timeout(seconds: int) -> Iterator[None]:
    def timeout_handler(_signum: int, _frame: FrameType | None) -> None:
        raise TimeoutError(f"operation exceeded {seconds} seconds")

    previous_handler = signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


def _cleanup_namespaces(backend: Pyroute2Backend, plan: TopologyPlan) -> None:
    for node in reversed(tuple(plan.nodes.values())):
        try:
            backend.delete_namespace(node.namespace)
        except NslabError as error:
            if error.code != "RESOURCE_MISSING":
                raise


def _vtysh(
    backend: Pyroute2Backend,
    plan: TopologyPlan,
    node_name: str,
    command: str,
) -> ExecResult:
    node = plan.nodes[node_name]
    return backend.execute(
        node.namespace,
        (
            "/usr/bin/vtysh",
            "-N",
            routing_pathspace(plan, node),
            "-c",
            command,
        ),
    )


def _wait_for(
    operation: Callable[[], ExecResult],
    predicate: Callable[[str], bool],
    *,
    description: str,
    timeout: float = 45.0,
) -> str:
    deadline = time.monotonic() + timeout
    latest = ""
    while time.monotonic() < deadline:
        result = operation()
        latest = result.stdout
        if result.returncode == 0 and predicate(latest):
            return latest
        time.sleep(0.5)
    pytest.fail(f"{description} did not converge:\n{latest}")


@pytest.mark.skipif(not _IS_LINUX, reason="requires Linux network namespaces")
@pytest.mark.skipif(not _IS_ROOT, reason="requires effective UID 0")
@pytest.mark.skipif(not _HAS_PIMD, reason="requires FRRouting pimd")
@pytest.mark.skipif(_IP is None, reason="requires iproute2")
def test_pim_example_control_plane_and_multicast_forwarding(tmp_path: Path) -> None:
    deployment = f"pim-e2e-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    manifest = load_manifest(_EXAMPLE)
    plan = compile_plan(manifest, name_override=deployment)
    backend = Pyroute2Backend()
    service = LifecycleService(backend, StateStore(tmp_path / "state"))

    try:
        with external_timeout(30):
            assert service.deploy(plan, manifest).changed is True
        with external_timeout(30):
            assert service.deploy(plan, manifest).changed is False
        assert inventory_matches_plan(plan, backend.inventory(plan))

        pim_neighbors = _wait_for(
            lambda: _vtysh(backend, plan, "r2", "show ip pim neighbor"),
            lambda output: "10.0.12.1" in output and "10.0.23.2" in output,
            description="PIM neighbors",
        )
        assert "eth0" in pim_neighbors and "eth1" in pim_neighbors
        rp_info = _wait_for(
            lambda: _vtysh(backend, plan, "r3", "show ip pim rp-info"),
            lambda output: "10.255.0.2" in output and "eth0" in output,
            description="RP route",
        )
        assert "Static" in rp_info

        receiver_commands = (
            (
                plan.nodes["receiver1"].namespace,
                "192.0.31.2",
            ),
            (
                plan.nodes["receiver2"].namespace,
                "192.0.32.2",
            ),
        )
        assert _IP is not None
        receivers: list[subprocess.Popen[str]] = []
        try:
            for namespace, address in receiver_commands:
                receivers.append(
                    subprocess.Popen(
                        (
                            _IP,
                            "netns",
                            "exec",
                            namespace,
                            sys.executable,
                            str(_RECEIVER),
                            address,
                            "--timeout",
                            "45",
                        ),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            igmp_groups = _wait_for(
                lambda: _vtysh(backend, plan, "r3", "show ip igmp groups"),
                lambda output: output.count("239.1.1.1") == 2,
                description="IGMP memberships",
            )
            assert "eth1" in igmp_groups and "eth2" in igmp_groups

            sent = backend.execute(
                plan.nodes["source"].namespace,
                (
                    sys.executable,
                    str(_SENDER),
                    "192.0.1.2",
                    "--delay",
                    "0",
                    "--count",
                    "10",
                    "--interval",
                    "0.2",
                ),
            )
            assert sent.returncode == 0, sent.stderr or sent.stdout
            deadline = time.monotonic() + 45
            received: list[tuple[int, str, str]] = []
            for process in receivers:
                stdout, stderr = process.communicate(timeout=max(0.1, deadline - time.monotonic()))
                received.append((process.returncode, stdout, stderr))
        finally:
            for process in receivers:
                if process.poll() is None:
                    process.terminate()
            for process in receivers:
                if process.poll() is None:
                    with suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=3)
                if process.poll() is None:
                    process.kill()
                    process.wait()

        for returncode, stdout, stderr in received:
            assert returncode == 0, stderr or stdout
            assert stdout.count(" from 192.0.1.2:") == 3

        with external_timeout(30):
            assert service.destroy(plan, deployment).status == "absent"
        assert all(not item.exists for item in backend.inventory(plan).namespaces.values())
    finally:
        with suppress(NslabError):
            backend.stop_routing(plan)
        _cleanup_namespaces(backend, plan)
