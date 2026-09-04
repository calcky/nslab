from __future__ import annotations

import os
import shutil
import signal
import subprocess
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
from nslab.manifest import Manifest, load_manifest
from nslab.planner import CakePlan, HtbPlan, TopologyPlan, compile_plan
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


def _htb_manifest() -> Manifest:
    source = load_manifest(_EXAMPLES / "qdisc" / "nslab.yaml")
    document = source.model_dump(mode="json")
    topology = document["topology"]
    topology["nodes"] = {name: topology["nodes"][name] for name in ("h7", "h8")}
    topology["links"] = [topology["links"][3]]
    return Manifest.model_validate(document)


def _require_cake_module() -> None:
    if Path("/sys/module/sch_cake").exists():
        return
    modprobe = shutil.which("modprobe")
    if modprobe is None:
        pytest.skip("requires kernel support for sch_cake (modprobe is unavailable)")
    result = subprocess.run(
        (modprobe, "sch_cake"),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        reason = result.stderr.strip() or "module is unavailable"
        pytest.skip(f"requires kernel support for sch_cake: {reason}")


def _assert_qdisc_lifecycle(
    tmp_path: Path,
    manifest: Manifest,
    *,
    expected_type: type[HtbPlan] | type[CakePlan],
    source_node: str,
    destination: str,
) -> None:
    deployment = f"qdisc-e2e-{os.getpid()}-{uuid.uuid4().hex[:6]}"
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
        for link in plan.links:
            for endpoint in (link.left, link.right):
                observed = inventory.namespaces[endpoint.namespace].interfaces[endpoint.interface]
                assert isinstance(observed.qdisc, expected_type)

        with external_timeout(10):
            ping = backend.execute(
                plan.nodes[source_node].namespace,
                ("/usr/bin/ping", "-c", "1", "-W", "2", destination),
            )
        assert ping.returncode == 0, ping.stderr or ping.stdout
        assert "1 received" in ping.stdout

        with external_timeout(30):
            assert service.destroy(plan, deployment).status == "absent"
        assert all(not item.exists for item in backend.inventory(plan).namespaces.values())
    finally:
        _cleanup_namespaces(backend, plan)


@pytest.mark.skipif(not _IS_LINUX, reason="requires Linux network namespaces")
@pytest.mark.skipif(not _IS_ROOT, reason="requires effective UID 0")
def test_htb_fq_codel_lifecycle_inventory_and_connectivity(tmp_path: Path) -> None:
    _assert_qdisc_lifecycle(
        tmp_path,
        _htb_manifest(),
        expected_type=HtbPlan,
        source_node="h7",
        destination="10.60.4.2",
    )


@pytest.mark.skipif(not _IS_LINUX, reason="requires Linux network namespaces")
@pytest.mark.skipif(not _IS_ROOT, reason="requires effective UID 0")
def test_cake_lifecycle_inventory_and_connectivity(tmp_path: Path) -> None:
    _require_cake_module()
    _assert_qdisc_lifecycle(
        tmp_path,
        load_manifest(_EXAMPLES / "cake" / "nslab.yaml"),
        expected_type=CakePlan,
        source_node="h1",
        destination="10.61.0.2",
    )
