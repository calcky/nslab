from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from textwrap import dedent

import pytest
from pyroute2 import NetNS

from nslab.backend.base import inventory_matches_plan
from nslab.backend.pyroute2 import Pyroute2Backend
from nslab.errors import NslabError
from nslab.manifest import Manifest
from nslab.planner import compile_plan

pytestmark = pytest.mark.root

_IS_LINUX = sys.platform.startswith("linux")
_IS_ROOT = getattr(os, "geteuid", lambda: -1)() == 0


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


def signal_probe_group_if_leader_matches(
    probe_pid: int,
    probe_pidfd: int,
    recorded_pgid: int,
    selected_signal: signal.Signals,
) -> None:
    try:
        signal.pidfd_send_signal(probe_pidfd, 0)
        current_pgid = os.getpgid(probe_pid)
    except ProcessLookupError:
        return
    if current_pgid != recorded_pgid:
        return
    try:
        signal.pidfd_send_signal(probe_pidfd, 0)
    except ProcessLookupError:
        return
    os.killpg(recorded_pgid, selected_signal)


def run_interrupt_probe(
    namespace: str,
    *,
    selected_signal: signal.Signals,
    process_group: bool,
    ignore_target_signals: bool,
) -> tuple[int, str, str, float]:
    if ignore_target_signals:
        target_code = dedent(
            """\
            import os
            import signal
            import time
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            print(f"exec-ready {os.getpid()}", flush=True)
            time.sleep(60)
            """
        )
        target_argv = (sys.executable, "-c", target_code)
    else:
        target_argv = (
            "/bin/sh",
            "-c",
            "printf 'exec-ready %s\\n' \"$$\"; exec /bin/sleep 60",
        )
    probe_code = dedent(
        f"""\
        import signal
        import sys
        from nslab.backend.pyroute2 import Pyroute2Backend
        from nslab.errors import OperationCancelled

        def cancel(signum, _frame):
            raise OperationCancelled(
                message="operation cancelled by SIGTERM",
                details={{"signal": signum}},
            )

        signal.signal(signal.SIGTERM, cancel)
        try:
            Pyroute2Backend().execute(
                {namespace!r},
                {target_argv!r},
                capture_output=False,
            )
        except KeyboardInterrupt:
            print("OPERATION_CANCELLED: operation interrupted", file=sys.stderr)
            raise SystemExit(130)
        except OperationCancelled as error:
            print(f"{{error.code}}: {{error.message}}", file=sys.stderr)
            raise SystemExit(143)
        """
    )
    probe = subprocess.Popen(
        [sys.executable, "-c", probe_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    recorded_pgid = probe.pid
    probe_pidfd: int | None = None
    target_pidfd: int | None = None
    try:
        probe_pidfd = os.pidfd_open(probe.pid)
        if os.getpgid(probe.pid) != recorded_pgid:
            raise RuntimeError("interrupt probe did not retain its session group")
        assert probe.stdout is not None
        with external_timeout(10):
            ready = probe.stdout.readline()
        prefix, raw_pid = ready.rstrip().split()
        assert prefix == "exec-ready"
        target_pidfd = os.pidfd_open(int(raw_pid))
        started = time.monotonic()
        if process_group:
            signal_probe_group_if_leader_matches(
                probe.pid,
                probe_pidfd,
                recorded_pgid,
                selected_signal,
            )
        else:
            with suppress(ProcessLookupError):
                signal.pidfd_send_signal(probe_pidfd, selected_signal)
        remaining_stdout, probe_stderr = probe.communicate(timeout=10)
        elapsed = time.monotonic() - started
        return int(probe.returncode), remaining_stdout, probe_stderr, elapsed
    finally:
        primary = sys.exception()
        cleanup_failures: list[tuple[str, BaseException]] = []

        def attempt_cleanup(label: str, cleanup: Callable[[], None]) -> None:
            try:
                cleanup()
            except ProcessLookupError:
                pass
            except BaseException as error:
                cleanup_failures.append((label, error))

        if target_pidfd is not None:
            attempt_cleanup(
                "target pidfd kill",
                lambda: signal.pidfd_send_signal(target_pidfd, signal.SIGKILL),
            )
        if probe_pidfd is not None:
            attempt_cleanup(
                "probe group kill",
                lambda: signal_probe_group_if_leader_matches(
                    probe.pid,
                    probe_pidfd,
                    recorded_pgid,
                    signal.SIGKILL,
                ),
            )
            attempt_cleanup(
                "probe leader kill",
                lambda: signal.pidfd_send_signal(probe_pidfd, signal.SIGKILL),
            )
        else:

            def kill_unreaped_probe_group() -> None:
                if os.getpgid(probe.pid) == recorded_pgid:
                    os.killpg(recorded_pgid, signal.SIGKILL)

            attempt_cleanup("probe group kill", kill_unreaped_probe_group)
            attempt_cleanup("probe leader kill", probe.kill)
        attempt_cleanup("probe wait", lambda: probe.wait(timeout=10))
        if target_pidfd is not None:
            attempt_cleanup("target pidfd close", lambda: os.close(target_pidfd))
        if probe_pidfd is not None:
            attempt_cleanup("probe pidfd close", lambda: os.close(probe_pidfd))

        if primary is not None:
            for label, failure in cleanup_failures:
                primary.add_note(f"{label} cleanup failed: {failure!r}")
        elif cleanup_failures:
            (_, cleanup_error), *secondary = cleanup_failures
            for label, failure in secondary:
                cleanup_error.add_note(f"{label} cleanup failed: {failure!r}")
            raise cleanup_error


@pytest.mark.skipif(not _IS_LINUX, reason="requires Linux network namespaces")
@pytest.mark.skipif(not _IS_ROOT, reason="requires effective UID 0")
def test_pyroute2_backend_creates_and_inventories_two_node_veth() -> None:
    deployment = f"it-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    manifest = Manifest.model_validate(
        {
            "version": 1,
            "name": deployment,
            "topology": {
                "nodes": {
                    "left": {
                        "kind": "linux",
                        "interfaces": {
                            "eth0": {"addresses": ["198.18.0.1/30"]},
                        },
                    },
                    "right": {
                        "kind": "linux",
                        "interfaces": {
                            "eth0": {"addresses": ["198.18.0.2/30"]},
                        },
                    },
                },
                "links": [
                    {
                        "endpoints": ["left:eth0", "right:eth0"],
                        "mtu": 1480,
                    }
                ],
            },
        }
    )
    plan = compile_plan(manifest)
    ownership_token = f"nslab-owned-{uuid.uuid4().hex}"
    backend = Pyroute2Backend(ownership_token_factory=lambda: ownership_token)
    nodes = tuple(plan.nodes.values())

    def cleanup_namespaces() -> None:
        cleanup_errors: list[BaseException] = []
        for node in reversed(nodes):
            try:
                backend.delete_namespace(node.namespace)
            except NslabError as error:
                if error.code != "RESOURCE_MISSING":
                    cleanup_errors.append(error)
            except BaseException as error:
                cleanup_errors.append(error)
        if cleanup_errors and sys.exc_info()[0] is None:
            raise cleanup_errors[0]

    try:
        for node in nodes:
            backend.create_namespace(node)
        backend.create_veth(plan.links[0])
        for node in nodes:
            backend.configure_node(node, plan)

        inventory = backend.inventory(plan)

        assert inventory_matches_plan(plan, inventory)
        assert inventory.root_interfaces == {}
        for node in nodes:
            observed = inventory.namespaces[node.namespace]
            assert observed.exists is True
            assert observed.interfaces["eth0"].kind == "veth"
            assert observed.interfaces["eth0"].mtu == 1480
            assert observed.interfaces["eth0"].up is True
            assert observed.interfaces["eth0"].addresses == node.interfaces["eth0"]
            assert observed.interfaces["eth0"].link_id == ownership_token

            namespace = NetNS(node.namespace, flags=0)
            try:
                (index,) = namespace.link_lookup(ifname="eth0")
                (message,) = namespace.get_links(index)
                assert message.get_attr("IFLA_IFALIAS") == ownership_token
            finally:
                namespace.close()

        with external_timeout(10):
            success = backend.execute(nodes[0].namespace, ("/usr/bin/printf", "exec-ok"))
        assert success.returncode == 0
        assert success.stdout == "exec-ok"
        assert success.stderr == ""

        with external_timeout(10):
            nonzero = backend.execute(nodes[0].namespace, ("/usr/bin/false",))
        assert nonzero.returncode == 1
        assert nonzero.stdout == ""
        assert nonzero.stderr == ""

        assignment_argv = ("FOO=bar", "/usr/bin/printenv", "FOO")
        with external_timeout(10):
            assignment = backend.execute(nodes[0].namespace, assignment_argv)
        assert assignment.argv == assignment_argv
        assert assignment.returncode != 0
        assert assignment.stdout == ""
        assert assignment.stderr

        with external_timeout(10):
            empty = backend.execute(nodes[0].namespace, ())
        assert empty.argv == ()
        assert empty.returncode != 0
        assert empty.stdout == ""
        assert empty.stderr

        missing_argv = (f"/nslab-command-does-not-exist-{uuid.uuid4().hex}",)
        with external_timeout(10):
            missing = backend.execute(nodes[0].namespace, missing_argv)
        assert missing.argv == missing_argv
        assert missing.returncode == 127
        assert missing.stdout == ""
        assert missing.stderr

        sigint_result = run_interrupt_probe(
            nodes[0].namespace,
            selected_signal=signal.SIGINT,
            process_group=True,
            ignore_target_signals=False,
        )
        assert sigint_result[:3] == (
            130,
            "",
            "OPERATION_CANCELLED: operation interrupted\n",
        )
        assert sigint_result[3] < 5

        sigterm_result = run_interrupt_probe(
            nodes[0].namespace,
            selected_signal=signal.SIGTERM,
            process_group=False,
            ignore_target_signals=True,
        )
        assert sigterm_result[:3] == (
            143,
            "",
            "OPERATION_CANCELLED: operation cancelled by SIGTERM\n",
        )
        assert sigterm_result[3] < 5
        assert inventory_matches_plan(plan, backend.inventory(plan))
    finally:
        cleanup_namespaces()

    try:
        inventory_after_cleanup = backend.inventory(plan)
        assert inventory_after_cleanup.root_interfaces == {}
        for node in nodes:
            assert inventory_after_cleanup.namespaces[node.namespace].exists is False
    finally:
        cleanup_namespaces()


@pytest.mark.skipif(not _IS_LINUX, reason="requires Linux network namespaces")
@pytest.mark.skipif(not _IS_ROOT, reason="requires effective UID 0")
def test_pyroute2_backend_configures_and_inventories_stp_tuning() -> None:
    deployment = f"stp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    manifest = Manifest.model_validate(
        {
            "version": 1,
            "name": deployment,
            "topology": {
                "nodes": {
                    "host": {"kind": "linux"},
                    "switch": {
                        "kind": "bridge",
                        "bridge": {
                            "name": "br0",
                            "stp": True,
                            "vlan_filtering": False,
                            "priority": 4096,
                            "ports": {
                                "swp1": {"path_cost": 10, "priority": 16},
                            },
                        },
                    },
                },
                "links": [{"endpoints": ["host:eth0", "switch:swp1"]}],
            },
        }
    )
    plan = compile_plan(manifest)
    backend = Pyroute2Backend()
    nodes = tuple(plan.nodes.values())

    def cleanup_namespaces() -> None:
        cleanup_errors: list[BaseException] = []
        for node in reversed(nodes):
            try:
                backend.delete_namespace(node.namespace)
            except NslabError as error:
                if error.code != "RESOURCE_MISSING":
                    cleanup_errors.append(error)
            except BaseException as error:
                cleanup_errors.append(error)
        if cleanup_errors and sys.exc_info()[0] is None:
            raise cleanup_errors[0]

    try:
        with external_timeout(30):
            for node in nodes:
                backend.create_namespace(node)
            backend.create_bridge(plan.nodes["switch"])
            backend.create_veth(plan.links[0])
            for node in nodes:
                backend.configure_node(node, plan)
            inventory = backend.inventory(plan)

        switch = inventory.namespaces[plan.nodes["switch"].namespace]
        assert inventory_matches_plan(plan, inventory)
        assert switch.interfaces["br0"].stp is True
        assert switch.interfaces["br0"].bridge_priority == 4096
        assert switch.interfaces["swp1"].path_cost == 10
        assert switch.interfaces["swp1"].port_priority == 16
    finally:
        cleanup_namespaces()
