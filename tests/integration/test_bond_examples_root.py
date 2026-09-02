from __future__ import annotations

import os
import signal
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from nslab.backend.base import ExecResult, inventory_matches_plan
from nslab.backend.pyroute2 import Pyroute2Backend
from nslab.errors import NslabError
from nslab.lifecycle import LifecycleService
from nslab.manifest import load_manifest
from nslab.planner import BondDevicePlan, TopologyPlan, compile_plan
from nslab.state import StateStore

pytestmark = pytest.mark.root

_IS_LINUX = sys.platform.startswith("linux")
_IS_ROOT = getattr(os, "geteuid", lambda: -1)() == 0
_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


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


def _wait_for(
    operation: Callable[[], ExecResult],
    predicate: Callable[[ExecResult], bool],
    *,
    description: str,
    seconds: float = 10.0,
) -> ExecResult:
    deadline = time.monotonic() + seconds
    last: ExecResult | None = None
    while time.monotonic() < deadline:
        last = operation()
        if predicate(last):
            return last
        time.sleep(0.1)
    output = "" if last is None else last.stderr or last.stdout
    raise AssertionError(f"timed out waiting for {description}: {output}")


def _wait_for_ping(
    backend: Pyroute2Backend,
    plan: TopologyPlan,
    destination: str,
) -> ExecResult:
    return _wait_for(
        lambda: backend.execute(
            plan.nodes["h1"].namespace,
            ("/usr/bin/ping", "-c", "1", "-W", "1", destination),
        ),
        lambda result: result.returncode == 0 and "1 received" in result.stdout,
        description=f"bond connectivity to {destination}",
    )


def _wait_for_active_slave(
    backend: Pyroute2Backend,
    plan: TopologyPlan,
    node: str,
    expected: str,
) -> None:
    _wait_for(
        lambda: backend.execute(
            plan.nodes[node].namespace,
            ("/usr/bin/cat", "/proc/net/bonding/bond0"),
        ),
        lambda result: (
            result.returncode == 0 and f"Currently Active Slave: {expected}" in result.stdout
        ),
        description=f"{node} active slave {expected}",
    )


@pytest.mark.skipif(not _IS_LINUX, reason="requires Linux network namespaces")
@pytest.mark.skipif(not _IS_ROOT, reason="requires effective UID 0")
@pytest.mark.parametrize(
    ("example", "mode", "destination"),
    [
        ("bond-active-backup", "active-backup", "10.60.0.2"),
        ("bond-8023ad", "802.3ad", "10.61.0.2"),
    ],
)
def test_bond_example_lifecycle_connectivity_and_mode_behavior(
    tmp_path: Path,
    example: str,
    mode: str,
    destination: str,
) -> None:
    deployment = f"bond-e2e-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    manifest = load_manifest(_EXAMPLES / example / "nslab.yaml")
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
        for node in plan.nodes.values():
            desired = node.devices["bond0"]
            assert isinstance(desired, BondDevicePlan)
            observed = inventory.namespaces[node.namespace]
            bond = observed.interfaces["bond0"]
            assert bond.bond_mode == mode
            assert bond.bond_miimon_ms == 100
            assert observed.interfaces["eth0"].master == "bond0"
            assert observed.interfaces["eth1"].master == "bond0"

        ping = _wait_for_ping(backend, plan, destination)
        assert ping.returncode == 0

        if mode == "active-backup":
            _wait_for_active_slave(backend, plan, "h1", "eth0")
            _wait_for_active_slave(backend, plan, "h2", "eth0")

            down = backend.execute(
                plan.nodes["h1"].namespace,
                ("/usr/sbin/ip", "link", "set", "eth0", "down"),
            )
            assert down.returncode == 0, down.stderr
            _wait_for_active_slave(backend, plan, "h1", "eth1")
            _wait_for_active_slave(backend, plan, "h2", "eth1")
            _wait_for_ping(backend, plan, destination)

            up = backend.execute(
                plan.nodes["h1"].namespace,
                ("/usr/sbin/ip", "link", "set", "eth0", "up"),
            )
            assert up.returncode == 0, up.stderr
            _wait_for_active_slave(backend, plan, "h1", "eth0")
            _wait_for_active_slave(backend, plan, "h2", "eth0")
            assert inventory_matches_plan(plan, backend.inventory(plan))
        else:
            status = _wait_for(
                lambda: backend.execute(
                    plan.nodes["h1"].namespace,
                    ("/usr/bin/cat", "/proc/net/bonding/bond0"),
                ),
                lambda result: (
                    result.returncode == 0
                    and "Bonding Mode: IEEE 802.3ad" in result.stdout
                    and "Number of ports: 2" in result.stdout
                ),
                description="two-port 802.3ad aggregator",
            )
            assert "LACP rate: fast" in status.stdout
            assert "Transmit Hash Policy: layer3+4" in status.stdout

        with external_timeout(30):
            destroyed = service.destroy(plan, deployment)
        assert destroyed.status == "absent"
        assert all(not item.exists for item in backend.inventory(plan).namespaces.values())
    finally:
        _cleanup_namespaces(backend, plan)
