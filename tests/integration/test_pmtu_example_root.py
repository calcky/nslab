from __future__ import annotations

import os
import re
import signal
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from nslab.backend.base import ExecResult, inventory_matches_plan
from nslab.backend.pyroute2 import Pyroute2Backend
from nslab.errors import NslabError
from nslab.lifecycle import LifecycleService
from nslab.manifest import load_manifest
from nslab.planner import TopologyPlan, compile_plan
from nslab.state import StateStore

pytestmark = pytest.mark.root

_IS_LINUX = sys.platform.startswith("linux")
_IS_ROOT = getattr(os, "geteuid", lambda: -1)() == 0
_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "pmtu" / "nslab.yaml"


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


def _execute(backend: Pyroute2Backend, plan: TopologyPlan, node: str, *argv: str) -> ExecResult:
    return backend.execute(plan.nodes[node].namespace, argv)


def _nstat_value(output: str, counter: str) -> int:
    match = re.search(rf"^{re.escape(counter)}\s+(\d+)", output, flags=re.MULTILINE)
    assert match is not None, output
    return int(match.group(1))


@pytest.mark.skipif(not _IS_LINUX, reason="requires Linux network namespaces")
@pytest.mark.skipif(not _IS_ROOT, reason="requires effective UID 0")
def test_pmtu_example_fragments_ipv4_and_learns_dual_stack_pmtu(tmp_path: Path) -> None:
    deployment = f"pmtu-e2e-{os.getpid()}-{uuid.uuid4().hex[:6]}"
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
        assert inventory.namespaces[plan.nodes["r1"].namespace].interfaces["eth1"].mtu == 1280
        assert inventory.namespaces[plan.nodes["r2"].namespace].interfaces["eth0"].mtu == 1280

        time.sleep(2)
        for destination, family in (
            ("198.51.100.2", "-4"),
            ("2001:db8:3::2", "-6"),
        ):
            ping = _execute(
                backend,
                plan,
                "h1",
                "/usr/bin/ping",
                family,
                "-c",
                "1",
                "-W",
                "2",
                destination,
            )
            assert ping.returncode == 0, ping.stderr or ping.stdout

        fragmented = _execute(
            backend,
            plan,
            "h1",
            "/usr/bin/ping",
            "-4",
            "-c",
            "1",
            "-W",
            "2",
            "-M",
            "dont",
            "-s",
            "1400",
            "198.51.100.2",
        )
        assert fragmented.returncode == 0, fragmented.stderr or fragmented.stdout
        r1_stats = _execute(
            backend,
            plan,
            "r1",
            "/usr/bin/nstat",
            "-az",
            "IpFragOKs",
            "IpFragCreates",
        )
        assert r1_stats.returncode == 0, r1_stats.stderr or r1_stats.stdout
        assert _nstat_value(r1_stats.stdout, "IpFragOKs") >= 1
        assert _nstat_value(r1_stats.stdout, "IpFragCreates") >= 2
        h2_stats = _execute(
            backend,
            plan,
            "h2",
            "/usr/bin/nstat",
            "-az",
            "IpReasmReqds",
            "IpReasmOKs",
        )
        assert h2_stats.returncode == 0, h2_stats.stderr or h2_stats.stdout
        assert _nstat_value(h2_stats.stdout, "IpReasmReqds") >= 2
        assert _nstat_value(h2_stats.stdout, "IpReasmOKs") >= 1

        ipv4_df = _execute(
            backend,
            plan,
            "h1",
            "/usr/bin/ping",
            "-4",
            "-c",
            "1",
            "-W",
            "2",
            "-M",
            "do",
            "-s",
            "1400",
            "198.51.100.2",
        )
        assert ipv4_df.returncode != 0
        assert "Frag needed" in ipv4_df.stdout
        assert "mtu = 1280" in ipv4_df.stdout
        ipv4_route = _execute(
            backend,
            plan,
            "h1",
            "/usr/sbin/ip",
            "-4",
            "route",
            "get",
            "198.51.100.2",
        )
        assert ipv4_route.returncode == 0, ipv4_route.stderr or ipv4_route.stdout
        assert "mtu 1280" in ipv4_route.stdout

        ipv6_large = _execute(
            backend,
            plan,
            "h1",
            "/usr/bin/ping",
            "-6",
            "-c",
            "1",
            "-W",
            "2",
            "-s",
            "1400",
            "2001:db8:3::2",
        )
        assert ipv6_large.returncode != 0
        assert "Packet too big" in ipv6_large.stdout
        assert "mtu=1280" in ipv6_large.stdout
        ipv6_route = _execute(
            backend,
            plan,
            "h1",
            "/usr/sbin/ip",
            "-6",
            "route",
            "get",
            "2001:db8:3::2",
        )
        assert ipv6_route.returncode == 0, ipv6_route.stderr or ipv6_route.stdout
        assert "mtu 1280" in ipv6_route.stdout

        ipv6_fits = _execute(
            backend,
            plan,
            "h1",
            "/usr/bin/ping",
            "-6",
            "-c",
            "1",
            "-W",
            "2",
            "-s",
            "1232",
            "2001:db8:3::2",
        )
        assert ipv6_fits.returncode == 0, ipv6_fits.stderr or ipv6_fits.stdout
        assert inventory_matches_plan(plan, backend.inventory(plan))

        with external_timeout(30):
            destroyed = service.destroy(plan, deployment)
        assert destroyed.status == "absent"
        assert all(not item.exists for item in backend.inventory(plan).namespaces.values())
    finally:
        _cleanup_namespaces(backend, plan)
