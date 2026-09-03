from __future__ import annotations

import os
import signal
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from nslab.backend.base import inventory_matches_plan
from nslab.backend.pyroute2 import Pyroute2Backend
from nslab.errors import NslabError
from nslab.inspector import inspect_topology
from nslab.lifecycle import LifecycleService
from nslab.manifest import load_manifest
from nslab.planner import TopologyPlan, compile_plan
from nslab.state import StateStore

pytestmark = pytest.mark.root

_IS_LINUX = sys.platform.startswith("linux")
_IS_ROOT = getattr(os, "geteuid", lambda: -1)() == 0
_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "neighbors" / "nslab.yaml"


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


def _cleanup_namespaces(backend: Pyroute2Backend, plan: TopologyPlan) -> None:
    cleanup_errors: list[BaseException] = []
    for node in reversed(tuple(plan.nodes.values())):
        try:
            backend.delete_namespace(node.namespace)
        except NslabError as error:
            if error.code != "RESOURCE_MISSING":
                cleanup_errors.append(error)
        except BaseException as error:
            cleanup_errors.append(error)
    if cleanup_errors and sys.exception() is None:
        raise cleanup_errors[0]


@pytest.mark.skipif(not _IS_LINUX, reason="requires Linux network namespaces")
@pytest.mark.skipif(not _IS_ROOT, reason="requires effective UID 0")
def test_neighbors_example_round_trips_static_and_proxy_entries(tmp_path: Path) -> None:
    deployment = f"neighbors-e2e-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    manifest = load_manifest(_EXAMPLE)
    plan = compile_plan(manifest, name_override=deployment)
    backend = Pyroute2Backend()
    store = StateStore(tmp_path / "state")
    service = LifecycleService(backend, store)

    try:
        with external_timeout(45):
            deployed = service.deploy(plan, manifest)
        assert deployed.changed is True

        # Global IPv6 addresses cannot send or receive until the default
        # one-second duplicate address detection cycle has completed.
        time.sleep(2)

        with external_timeout(30):
            repeated = service.deploy(plan, manifest)
        assert repeated.changed is False

        inventory = backend.inventory(plan)
        assert inventory_matches_plan(plan, inventory)
        assert inventory.namespaces[plan.nodes["h1"].namespace].interfaces["eth0"].mac == (
            "02:00:00:00:01:01"
        )
        assert {
            neighbor.state for neighbor in plan.nodes["r1"].neighbors if not neighbor.proxy
        } == {
            "permanent",
            "reachable",
        }
        assert {neighbor.state for neighbor in plan.nodes["h2"].neighbors} == {"stale", "noarp"}
        assert sum(neighbor.proxy for neighbor in plan.nodes["r1"].neighbors) == 2

        for destination, family_option in (
            ("192.0.2.200", "-4"),
            ("2001:db8:1::200", "-6"),
        ):
            with external_timeout(30):
                ping = backend.execute(
                    plan.nodes["h1"].namespace,
                    ("/usr/bin/ping", family_option, "-c", "1", "-W", "2", destination),
                )
            assert ping.returncode == 0, ping.stderr or ping.stdout
            assert "1 received" in ping.stdout

        post_traffic = backend.inventory(plan)
        assert inventory_matches_plan(plan, post_traffic)
        assert inspect_topology(plan, store.load(plan.name), post_traffic).status == "deployed"

        with external_timeout(30):
            destroyed = service.destroy(plan, deployment)
        assert destroyed.status == "absent"
        assert all(not item.exists for item in backend.inventory(plan).namespaces.values())
    finally:
        _cleanup_namespaces(backend, plan)
