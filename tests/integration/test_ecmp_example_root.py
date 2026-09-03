from __future__ import annotations

import os
import signal
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from types import FrameType

import pytest

from nslab.backend.base import inventory_matches_plan
from nslab.backend.pyroute2 import Pyroute2Backend
from nslab.errors import NslabError
from nslab.lifecycle import LifecycleService
from nslab.manifest import load_manifest
from nslab.planner import RouteNextHopPlan, TopologyPlan, compile_plan
from nslab.state import StateStore

pytestmark = pytest.mark.root

_IS_LINUX = os.name == "posix" and Path("/proc/self/ns/net").exists()
_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "ecmp" / "nslab.yaml"


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
def test_ecmp_example_lifecycle_routes_and_connectivity(tmp_path: Path) -> None:
    deployment = f"ecmp-e2e-{os.getpid()}-{uuid.uuid4().hex[:6]}"
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
        r1_routes = inventory.namespaces[plan.nodes["r1"].namespace].routes
        ecmp_route = next(route for route in r1_routes if route.dst == IPv4Network("192.0.2.0/24"))
        assert ecmp_route.nexthops == (
            RouteNextHopPlan(IPv4Address("10.0.12.2"), "eth1", 1),
            RouteNextHopPlan(IPv4Address("10.0.13.2"), "eth2", 1),
        )

        route_output = backend.execute(
            plan.nodes["r1"].namespace,
            ("/usr/sbin/ip", "-4", "route", "show", "192.0.2.0/24"),
        )
        assert route_output.returncode == 0, route_output.stderr
        assert "nexthop via 10.0.12.2 dev eth1 weight 1" in route_output.stdout
        assert "nexthop via 10.0.13.2 dev eth2 weight 1" in route_output.stdout

        with external_timeout(10):
            ping = backend.execute(
                plan.nodes["h1"].namespace,
                ("/usr/bin/ping", "-c", "3", "-W", "2", "192.0.2.2"),
            )
        assert ping.returncode == 0, ping.stderr or ping.stdout
        assert "3 received" in ping.stdout

        with external_timeout(30):
            assert service.destroy(plan, deployment).status == "absent"
        assert all(not item.exists for item in backend.inventory(plan).namespaces.values())
    finally:
        _cleanup_namespaces(backend, plan)
