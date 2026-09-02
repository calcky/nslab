from __future__ import annotations

import os
import signal
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from nslab.backend.base import inventory_matches_plan
from nslab.backend.pyroute2 import Pyroute2Backend
from nslab.errors import NslabError
from nslab.lifecycle import LifecycleService
from nslab.manifest import Manifest, load_manifest, normalized_manifest
from nslab.planner import TopologyPlan, compile_plan
from nslab.state import StateStore

pytestmark = pytest.mark.root

_IS_LINUX = sys.platform.startswith("linux")
_IS_ROOT = getattr(os, "geteuid", lambda: -1)() == 0
_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "policy-routing" / "nslab.yaml"


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
def test_policy_routing_example_selects_tables_by_selectors_and_mark(tmp_path: Path) -> None:
    deployment = f"rule-e2e-{os.getpid()}-{uuid.uuid4().hex[:6]}"
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
        r1 = inventory.namespaces[plan.nodes["r1"].namespace]
        assert tuple((rule.priority, rule.table) for rule in r1.rules) == (
            (90, 200),
            (100, 100),
        )

        base_query = (
            "/usr/sbin/ip",
            "-4",
            "route",
            "get",
            "203.0.113.1",
            "from",
            "192.0.2.2",
            "iif",
            "eth0",
        )
        unmarked = backend.execute(plan.nodes["r1"].namespace, base_query)
        marked = backend.execute(plan.nodes["r1"].namespace, (*base_query, "mark", "2"))

        assert unmarked.returncode == 0, unmarked.stderr or unmarked.stdout
        assert "dev eth1" in unmarked.stdout
        assert "table 100" in unmarked.stdout
        assert marked.returncode == 0, marked.stderr or marked.stdout
        assert "dev eth2" in marked.stdout
        assert "table 200" in marked.stdout

        with external_timeout(30):
            ping = backend.execute(
                plan.nodes["h1"].namespace,
                ("/usr/bin/ping", "-c", "1", "-W", "2", "203.0.113.1"),
            )
        assert ping.returncode == 0, ping.stderr or ping.stdout
        assert "1 received" in ping.stdout

        with external_timeout(30):
            destroyed = service.destroy(plan, deployment)
        assert destroyed.status == "absent"
        assert all(not item.exists for item in backend.inventory(plan).namespaces.values())
    finally:
        _cleanup_namespaces(backend, plan)


@pytest.mark.skipif(not _IS_LINUX, reason="requires Linux network namespaces")
@pytest.mark.skipif(not _IS_ROOT, reason="requires effective UID 0")
def test_policy_routing_complete_rule_set_round_trips_through_kernel(tmp_path: Path) -> None:
    document: Any = normalized_manifest(load_manifest(_EXAMPLE))
    document["topology"]["nodes"]["r1"]["rules"] = [
        {
            "priority": 110,
            "family": "ipv4",
            "table": 100,
            "from": "192.0.2.0/24",
            "to": "203.0.113.0/24",
            "not": True,
            "tos": 16,
            "fwmark": 1,
            "fwmask": 255,
            "iif": "eth0",
            "oif": "eth1",
            "uid_range": {"start": 0, "end": 0},
            "protocol": 99,
            "ip_protocol": 6,
            "source_port": {"start": 1000, "end": 2000},
            "destination_port": {"start": 80, "end": 443},
            "tunnel_id": 123,
            "suppress_prefix_length": 24,
            "suppress_interface_group": 7,
            "realms": {"source": 1, "destination": 2},
        },
        {
            "priority": 200,
            "family": "ipv4",
            "action": "goto",
            "goto": 300,
            "fwmark": 2,
        },
        {"priority": 300, "family": "ipv4", "table": 200},
        {"priority": 400, "family": "ipv4", "action": "nop"},
        {"priority": 401, "family": "ipv4", "action": "blackhole"},
        {"priority": 402, "family": "ipv4", "action": "unreachable"},
        {"priority": 403, "family": "ipv4", "action": "prohibit"},
        {"priority": 500, "family": "ipv4", "l3mdev": True},
        {"priority": 32766, "family": "ipv4", "table": 255},
        {"priority": 4_294_967_295, "family": "ipv4", "table": 253},
        {
            "priority": 110,
            "family": "ipv6",
            "action": "prohibit",
            "to": "2001:db8::/32",
        },
    ]
    manifest = Manifest.model_validate(document)
    deployment = f"rule-all-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    plan = compile_plan(manifest, name_override=deployment)
    backend = Pyroute2Backend()
    service = LifecycleService(backend, StateStore(tmp_path / "state"))

    try:
        with external_timeout(45):
            service.deploy(plan, manifest)

        inventory = backend.inventory(plan)
        assert inventory_matches_plan(plan, inventory)
        observed = inventory.namespaces[plan.nodes["r1"].namespace].rules
        assert {rule.action for rule in observed} == {
            "lookup",
            "goto",
            "nop",
            "blackhole",
            "unreachable",
            "prohibit",
        }
        assert {rule.family for rule in observed} == {4, 6}
        assert any(rule.l3mdev and rule.priority == 500 for rule in observed)
        assert any(rule.table == 255 and rule.priority == 32766 for rule in observed)
        assert any(rule.priority == 4_294_967_295 for rule in observed)

        with external_timeout(30):
            destroyed = service.destroy(plan, deployment)
        assert destroyed.status == "absent"
    finally:
        _cleanup_namespaces(backend, plan)
