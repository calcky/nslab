from __future__ import annotations

import os
import signal
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType

import pytest

from nslab.backend.base import inventory_matches_plan
from nslab.backend.pyroute2 import Pyroute2Backend
from nslab.errors import NslabError
from nslab.lifecycle import LifecycleService
from nslab.manifest import load_manifest
from nslab.planner import TopologyPlan, compile_plan
from nslab.state import StateStore

pytestmark = pytest.mark.root

_IS_LINUX = os.name == "posix" and Path("/proc/self/ns/net").exists()
_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "ip-tunnels" / "nslab.yaml"


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


@pytest.mark.skipif(not _IS_LINUX, reason="requires Linux network namespaces")
@pytest.mark.skipif(not _IS_ROOT, reason="requires effective UID 0")
def test_ip_tunnels_example_lifecycle_and_connectivity(tmp_path: Path) -> None:
    deployment = f"tunnel-e2e-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    manifest = load_manifest(_EXAMPLE)
    plan = compile_plan(manifest, name_override=deployment)
    backend = Pyroute2Backend()
    service = LifecycleService(backend, StateStore(tmp_path / "state"))

    try:
        with external_timeout(30):
            assert service.deploy(plan, manifest).changed is True
        with external_timeout(30):
            assert service.deploy(plan, manifest).changed is False

        inventory = backend.inventory(plan)
        assert inventory_matches_plan(plan, inventory)
        r1 = inventory.namespaces[plan.nodes["r1"].namespace].interfaces
        assert r1["gre1"].gre_link == "underlay0"
        assert r1["gre1"].gre_key == 100
        assert r1["gre1"].mtu == 1472
        assert r1["ipip0"].ipip_link == "underlay0"
        assert r1["ipip0"].mtu == 1480

        for destination in ("10.10.0.2", "10.20.0.2"):
            with external_timeout(10):
                ping = backend.execute(
                    plan.nodes["r1"].namespace,
                    ("/usr/bin/ping", "-c", "1", "-W", "2", destination),
                )
            assert ping.returncode == 0, ping.stderr or ping.stdout

        with external_timeout(30):
            assert service.destroy(plan, deployment).status == "absent"
        assert all(not item.exists for item in backend.inventory(plan).namespaces.values())
    finally:
        _cleanup_namespaces(backend, plan)
