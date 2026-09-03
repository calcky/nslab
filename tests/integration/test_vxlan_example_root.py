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
_EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "vxlan"
_EXAMPLE = _EXAMPLES / "nslab.yaml"


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
def test_vxlan_example_lifecycle_and_both_overlay_connectivity(tmp_path: Path) -> None:
    deployment = f"vxlan-e2e-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    manifest = load_manifest(_EXAMPLE)
    plan = compile_plan(manifest, name_override=deployment)
    backend = Pyroute2Backend()
    service = LifecycleService(backend, StateStore(tmp_path / "state"))

    try:
        with external_timeout(30):
            deployed = service.deploy(plan, manifest)
        assert deployed.changed is True

        with external_timeout(30):
            repeated = service.deploy(plan, manifest)
        assert repeated.changed is False

        inventory = backend.inventory(plan)
        assert inventory_matches_plan(plan, inventory)
        vtep1 = inventory.namespaces[plan.nodes["vtep1"].namespace]
        assert vtep1.interfaces["underlay0"].master is None
        assert vtep1.interfaces["vxlan100"].master == "br0"

        for name, local, remote in (
            ("r1", "192.0.2.3", "192.0.2.4"),
            ("r2", "192.0.2.4", "192.0.2.3"),
        ):
            routed_vtep = inventory.namespaces[plan.nodes[name].namespace]
            interface = routed_vtep.interfaces["vxlan200"]
            assert interface.master is None
            assert str(interface.vxlan_local) == local
            assert str(interface.vxlan_remote) == remote
            assert interface.addresses

        link = backend.execute(
            plan.nodes["vtep1"].namespace,
            ("/usr/sbin/ip", "-d", "link", "show", "vxlan100"),
        )
        assert link.returncode == 0, link.stderr
        assert "vxlan id 100" in link.stdout
        assert "remote 192.0.2.2" in link.stdout
        assert "dstport 4789" in link.stdout

        fdb = backend.execute(
            plan.nodes["vtep1"].namespace,
            ("/usr/sbin/bridge", "fdb", "show", "dev", "vxlan100"),
        )
        assert fdb.returncode == 0, fdb.stderr
        assert "00:00:00:00:00:00 dst 192.0.2.2" in fdb.stdout

        ping = backend.execute(
            plan.nodes["h1"].namespace,
            ("/usr/bin/ping", "-c", "2", "-W", "2", "10.70.0.2"),
        )
        assert ping.returncode == 0, ping.stderr

        for node, destination in (("h3", "10.80.2.2"), ("h4", "10.80.1.1")):
            with external_timeout(30):
                routed_ping = backend.execute(
                    plan.nodes[node].namespace,
                    ("/usr/bin/ping", "-c", "1", "-W", "2", destination),
                )
            assert routed_ping.returncode == 0, routed_ping.stderr or routed_ping.stdout

        with external_timeout(30):
            destroyed = service.destroy(plan, deployment)
        assert destroyed.status == "absent"
        assert all(not item.exists for item in backend.inventory(plan).namespaces.values())
    finally:
        _cleanup_namespaces(backend, plan)
