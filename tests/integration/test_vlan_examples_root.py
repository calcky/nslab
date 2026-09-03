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
_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "vlan" / "nslab.yaml"


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
def test_vlan_example_lifecycle_and_connectivity(tmp_path: Path) -> None:
    deployment = f"vlan-e2e-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    manifest = load_manifest(_EXAMPLE)
    plan = compile_plan(manifest, name_override=deployment)
    backend = Pyroute2Backend()
    service = LifecycleService(backend, StateStore(tmp_path / "state"))

    try:
        with external_timeout(45):
            deployed = service.deploy(plan, manifest)
        assert deployed.changed is True

        with external_timeout(30):
            repeated = service.deploy(plan, manifest)
        assert repeated.changed is False

        inventory = backend.inventory(plan)
        assert inventory_matches_plan(plan, inventory)
        expected_devices = {
            "h1": {"vlan10": ("eth0", 10)},
            "h2": {"vlan10": ("eth0", 10)},
            "r1": {
                "vlan10": ("eth0", 10),
                "vlan20": ("eth0", 20),
            },
        }
        for node_name, node_devices in expected_devices.items():
            namespace = inventory.namespaces[plan.nodes[node_name].namespace]
            for device_name, (parent, vlan_id) in node_devices.items():
                observed = namespace.interfaces[device_name]
                assert observed.kind == "vlan"
                assert observed.parent == parent
                assert observed.vlan_id == vlan_id

        for source, destination in (
            ("h1", "192.168.10.4"),
            ("h1", "192.168.20.2"),
            ("h10", "192.168.20.2"),
        ):
            with external_timeout(30):
                ping = backend.execute(
                    plan.nodes[source].namespace,
                    ("/usr/bin/ping", "-c", "1", "-W", "2", destination),
                )
            assert ping.returncode == 0, ping.stderr or ping.stdout
            assert "1 received" in ping.stdout

        with external_timeout(30):
            destroyed = service.destroy(plan, deployment)
        assert destroyed.status == "absent"
        assert all(not item.exists for item in backend.inventory(plan).namespaces.values())
    finally:
        _cleanup_namespaces(backend, plan)
