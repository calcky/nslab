from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
from collections.abc import Sequence
from contextlib import suppress
from multiprocessing.connection import Connection
from typing import Any

import pytest

from nslab.backend.pyroute2 import Pyroute2Backend


def _run_forked_stdin_probe(
    argv: Sequence[str],
    options: dict[str, Any],
    sender: Connection,
) -> None:
    child_options = dict(options)
    child_options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process = subprocess.Popen(list(argv), **child_options)
    sender.send(("ready", process.pid))
    stdout, stderr = process.communicate()
    sender.send(("done", process.returncode, stdout, stderr))
    sender.close()


class _ForkedStdinProbe:
    def __init__(
        self,
        server: multiprocessing.Process,
        receiver: Connection,
        pid: int,
    ) -> None:
        self.server = server
        self.receiver = receiver
        self.pid = pid
        self.returncode: int | None = None
        self.observed_stdout = ""
        self.observed_stderr = ""

    def communicate(self) -> tuple[None, None]:
        status, returncode, stdout, stderr = self.receiver.recv()
        assert status == "done"
        self.returncode = int(returncode)
        self.observed_stdout = str(stdout)
        self.observed_stderr = str(stderr)
        return None, None

    def release(self) -> None:
        self.server.join(timeout=10)
        if self.server.is_alive():
            self.server.kill()
            self.server.join(timeout=10)
            raise TimeoutError("forked stdin probe did not exit")
        self.receiver.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux pidfds")
def test_passthrough_stdin_survives_multiprocessing_proxy_bootstrap() -> None:
    context = multiprocessing.get_context("fork")
    observed: list[_ForkedStdinProbe] = []

    def factory(
        _namespace: str,
        argv: Sequence[str],
        **options: Any,
    ) -> _ForkedStdinProbe:
        receiver, sender = context.Pipe(duplex=False)
        server = context.Process(
            target=_run_forked_stdin_probe,
            args=(argv, options, sender),
        )
        try:
            server.start()
            sender.close()
            if not receiver.poll(10):
                raise TimeoutError("forked stdin probe handshake timed out")
            status, target_pid = receiver.recv()
            assert status == "ready"
            probe = _ForkedStdinProbe(server, receiver, int(target_pid))
            observed.append(probe)
            return probe
        except BaseException:
            with suppress(BaseException):
                sender.close()
            with suppress(BaseException):
                receiver.close()
            if server.pid is not None:
                with suppress(BaseException):
                    if server.is_alive():
                        server.kill()
                with suppress(BaseException):
                    server.join(timeout=10)
            raise

    saved_stdin = os.dup(0)
    saved_sys_stdin = sys.stdin
    stdin_wrapper = None
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"stdin-through-proxy\n")
        os.close(write_fd)
        write_fd = -1
        os.dup2(read_fd, 0)
        os.close(read_fd)
        read_fd = -1
        stdin_wrapper = os.fdopen(0, encoding="utf-8")
        sys.stdin = stdin_wrapper

        result = Pyroute2Backend(nspopen_factory=factory).execute(
            "unused-test-namespace",
            (
                sys.executable,
                "-c",
                "import sys; sys.stdout.write(sys.stdin.read())",
            ),
            capture_output=False,
        )
    finally:
        sys.stdin = saved_sys_stdin
        try:
            if stdin_wrapper is not None:
                stdin_wrapper.close()
        finally:
            try:
                os.dup2(saved_stdin, 0)
            finally:
                os.close(saved_stdin)
                if read_fd >= 0:
                    os.close(read_fd)
                if write_fd >= 0:
                    os.close(write_fd)

    assert len(observed) == 1
    assert (
        result.returncode,
        result.stdout,
        result.stderr,
        observed[0].observed_stdout,
        observed[0].observed_stderr,
    ) == (0, "", "", "stdin-through-proxy\n", "")
    assert observed[0].server.exitcode == 0
