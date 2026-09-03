from __future__ import annotations

import os
import signal
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from nslab.backend.base import inventory_matches_plan
from nslab.backend.pyroute2 import Pyroute2Backend
from nslab.errors import NslabError
from nslab.lifecycle import LifecycleService
from nslab.manifest import load_manifest
from nslab.planner import TopologyPlan, compile_plan
from nslab.state import StateStore

pytestmark = pytest.mark.root

_IS_LINUX = sys.platform.startswith("linux")
_IS_ROOT = getattr(os, "geteuid", lambda: -1)() == 0
_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "bridge-port-controls" / "nslab.yaml"


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
def test_bridge_port_controls_configure_inventory_and_forwarding(tmp_path: Path) -> None:
    deployment = f"bridge-port-e2e-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    manifest = load_manifest(_EXAMPLE)
    plan = compile_plan(manifest, name_override=deployment)
    backend = Pyroute2Backend()
    store = StateStore(tmp_path / "state")
    service = LifecycleService(backend, store)

    try:
        with external_timeout(45):
            assert service.deploy(plan, manifest).changed is True

        inventory = backend.inventory(plan)
        assert inventory_matches_plan(plan, inventory)
        switch = inventory.namespaces[plan.nodes["sw1"].namespace]
        swp1 = switch.interfaces["swp1"]
        swp2 = switch.interfaces["swp2"]
        assert swp1.hairpin is True
        assert swp1.isolated is True
        assert swp2.isolated is True
        assert swp2.learning is False
        assert swp2.flood is False
        assert swp2.multicast_flood is False

        isolated_ping = backend.execute(
            plan.nodes["h1"].namespace,
            ("/usr/bin/ping", "-c", "1", "-W", "1", "10.20.0.2"),
        )
        assert isolated_ping.returncode != 0

        uplink_ping = backend.execute(
            plan.nodes["h1"].namespace,
            ("/usr/bin/ping", "-c", "1", "-W", "2", "10.20.0.3"),
        )
        assert uplink_ping.returncode == 0, uplink_ping.stderr or uplink_ping.stdout

        unknown_unicast_ping = backend.execute(
            plan.nodes["h3"].namespace,
            ("/usr/bin/ping", "-c", "1", "-W", "1", "10.20.0.2"),
        )
        assert unknown_unicast_ping.returncode != 0
        fdb = backend.execute(
            plan.nodes["sw1"].namespace,
            ("/usr/sbin/bridge", "fdb", "show", "br", "br0", "dev", "swp2"),
        )
        assert fdb.returncode == 0, fdb.stderr or fdb.stdout
        assert "02:00:00:00:20:02" not in fdb.stdout

        enable_flood = backend.execute(
            plan.nodes["sw1"].namespace,
            ("/usr/sbin/bridge", "link", "set", "dev", "swp2", "flood", "on"),
        )
        assert enable_flood.returncode == 0, enable_flood.stderr or enable_flood.stdout
        assert not inventory_matches_plan(plan, backend.inventory(plan))

        flooded_ping = backend.execute(
            plan.nodes["h3"].namespace,
            ("/usr/bin/ping", "-c", "1", "-W", "2", "10.20.0.2"),
        )
        assert flooded_ping.returncode == 0, flooded_ping.stderr or flooded_ping.stdout

        disable_flood = backend.execute(
            plan.nodes["sw1"].namespace,
            ("/usr/sbin/bridge", "link", "set", "dev", "swp2", "flood", "off"),
        )
        assert disable_flood.returncode == 0, disable_flood.stderr or disable_flood.stdout
        assert inventory_matches_plan(plan, backend.inventory(plan))

        with external_timeout(30):
            assert service.destroy(plan, deployment).status == "absent"
    finally:
        _cleanup_namespaces(backend, plan)
