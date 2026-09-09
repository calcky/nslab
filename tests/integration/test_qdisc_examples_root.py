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
from nslab.planner import CakePlan, HtbPlan, SimpleQdiscPlan, TopologyPlan, compile_plan
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
    expected_type: type[HtbPlan] | type[CakePlan] | type[SimpleQdiscPlan],
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


_SIMPLE_QDISCS = [
    {"kind": "pfifo", "limit": 1000},
    {"kind": "bfifo", "limit": 1514000},
    {"kind": "pfifo_fast"},
    {"kind": "prio"},
    {"kind": "sfq", "limit": 1000, "quantum": 1514, "perturb": 10, "flows": 128},
    {"kind": "fq", "limit": 1000, "quantum": 1514},
    {"kind": "codel", "limit": 1000, "target_ms": 5, "interval_ms": 100, "ecn": False},
    {
        "kind": "red",
        "limit": 1514000,
        "min": 125000,
        "max": 375000,
        "avpkt": 1500,
        "burst": 139,
        "probability": 0.02,
        "ecn": False,
    },
    {
        "kind": "pie",
        "limit": 1000,
        "target_ms": 5,
        "tupdate_ms": 15,
        "alpha": 2,
        "beta": 20,
        "ecn": False,
        "bytemode": False,
    },
    {
        "kind": "fq_pie",
        "limit": 1000,
        "target_ms": 5,
        "tupdate_ms": 15,
        "alpha": 2,
        "beta": 20,
        "ecn": False,
        "bytemode": False,
    },
]


@pytest.mark.skipif(not _IS_LINUX or not _IS_ROOT, reason="requires Linux EUID 0")
def test_shipped_red_example_lifecycle(tmp_path: Path) -> None:
    document = load_manifest(_EXAMPLES / "qdisc" / "nslab.yaml").model_dump(mode="json")
    topology = document["topology"]
    topology["nodes"] = {name: topology["nodes"][name] for name in ("h9", "h10")}
    topology["links"] = [topology["links"][4]]
    _assert_qdisc_lifecycle(
        tmp_path,
        Manifest.model_validate(document),
        expected_type=SimpleQdiscPlan,
        source_node="h9",
        destination="10.60.5.2",
    )


def _simple_manifest(qdisc: dict[str, object], *, htb: bool) -> Manifest:
    document = _htb_manifest().model_dump(mode="json")
    document["topology"]["links"][0]["qdisc"] = (
        {"kind": "htb", "rate": "100mbit", "leaf": qdisc} if htb else qdisc
    )
    return Manifest.model_validate(document)


@pytest.mark.skipif(not _IS_LINUX or not _IS_ROOT, reason="requires Linux EUID 0")
@pytest.mark.parametrize("htb", [False, True], ids=["root", "htb"])
@pytest.mark.parametrize("qdisc", _SIMPLE_QDISCS, ids=lambda item: item["kind"])
def test_simple_qdisc_lifecycle_inventory_and_connectivity(
    tmp_path: Path,
    qdisc: dict[str, object],
    htb: bool,
) -> None:
    _assert_qdisc_lifecycle(
        tmp_path,
        _simple_manifest(qdisc, htb=htb),
        expected_type=HtbPlan if htb else SimpleQdiscPlan,
        source_node="h7",
        destination="10.60.4.2",
    )


@pytest.mark.skipif(not _IS_LINUX or not _IS_ROOT, reason="requires Linux EUID 0")
@pytest.mark.parametrize("htb", [False, True], ids=["root", "htb"])
@pytest.mark.parametrize(
    "qdisc",
    [
        {"kind": "red", "limit": 1514000, "avpkt": 1500},
        {
            "kind": "red",
            "limit": 100000,
            "min": 1500,
            "max": 4500,
            "avpkt": 1500,
            "burst": 1,
            "ecn": True,
        },
        {"kind": "codel", "ecn": True},
        {"kind": "pie", "ecn": True, "bytemode": True},
        {"kind": "fq_pie", "ecn": True, "bytemode": True},
    ],
    ids=["red-defaults", "red-ewma-one", "codel-ecn", "pie-flags", "fq-pie-flags"],
)
def test_simple_qdisc_defaults_and_flags(
    tmp_path: Path,
    qdisc: dict[str, object],
    htb: bool,
) -> None:
    _assert_qdisc_lifecycle(
        tmp_path,
        _simple_manifest(qdisc, htb=htb),
        expected_type=HtbPlan if htb else SimpleQdiscPlan,
        source_node="h7",
        destination="10.60.4.2",
    )


@pytest.mark.skipif(not _IS_LINUX or not _IS_ROOT, reason="requires Linux EUID 0")
@pytest.mark.parametrize("htb", [False, True], ids=["root", "htb"])
def test_simple_qdisc_external_change_is_detected(tmp_path: Path, htb: bool) -> None:
    manifest = _simple_manifest({"kind": "pfifo", "limit": 1000}, htb=htb)
    deployment = f"qdisc-e2e-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    plan = compile_plan(manifest, name_override=deployment)
    backend = Pyroute2Backend()
    service = LifecycleService(backend, StateStore(tmp_path / "state"))
    try:
        with external_timeout(30):
            assert service.deploy(plan, manifest).changed
        endpoint = plan.links[0].left
        location = ("parent", "1:1", "handle", "10:") if htb else ("root",)
        result = backend.execute(
            endpoint.namespace,
            (
                "tc",
                "qdisc",
                "change",
                "dev",
                endpoint.interface,
                *location,
                "pfifo",
                "limit",
                "2000",
            ),
        )
        assert result.returncode == 0, result.stderr
        actual = backend.inventory(plan)
        observed = actual.namespaces[endpoint.namespace].interfaces[endpoint.interface].qdisc
        leaf = observed.leaf if isinstance(observed, HtbPlan) else observed
        assert isinstance(leaf, SimpleQdiscPlan)
        assert leaf.options["limit"] == 2000
        assert not inventory_matches_plan(plan, actual)
        with external_timeout(30), pytest.raises(NslabError) as caught:
            service.deploy(plan, manifest)
        assert caught.value.code == "DEPLOYMENT_DRIFT"
        with external_timeout(30):
            assert service.destroy(plan, deployment).status == "absent"
    finally:
        _cleanup_namespaces(backend, plan)


@pytest.mark.skipif(not _IS_LINUX or not _IS_ROOT, reason="requires Linux EUID 0")
def test_tc_inventory_failure_rolls_back_owned_namespaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _simple_manifest({"kind": "pfifo", "limit": 1000}, htb=True)
    deployment = f"qdisc-e2e-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    plan = compile_plan(manifest, name_override=deployment)
    backend = Pyroute2Backend()
    service = LifecycleService(backend, StateStore(tmp_path / "state"))

    def fail_inventory(_namespace: str, _interface: str) -> object:
        raise NslabError(code="INVENTORY_UNSUPPORTED", message="injected tc read failure")

    monkeypatch.setattr(backend, "_read_tc_qdiscs", fail_inventory)
    try:
        with external_timeout(30), pytest.raises(NslabError) as caught:
            service.deploy(plan, manifest)
        assert caught.value.code == "INVENTORY_UNSUPPORTED"
        assert caught.value.details["rollback_complete"] is True
        assert all(not item.exists for item in backend.inventory(plan).namespaces.values())
    finally:
        _cleanup_namespaces(backend, plan)
