import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "examples/bpf-path/check_redirect.py"
_SPEC = importlib.util.spec_from_file_location("bpf_redirect_check", _SCRIPT)
assert _SPEC and _SPEC.loader
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)


def test_latest_counter_sample_is_used():
    output = (
        "attached devmap on eth0; output eth1; Ctrl+C detaches\n"
        "seen=0 redirected=0 map_miss=0 fib_fallback=0 passed=0\n"
        "seen=5 redirected=3 map_miss=0 fib_fallback=0 passed=2\n"
    )
    assert check.read_counters(output) == (5, 3, 0, 0, 2)


def test_empty_map_fallback_counters():
    assert check.read_counters("seen=5 redirected=0 map_miss=3 fib_fallback=0 passed=5") == (
        5,
        0,
        3,
        0,
        5,
    )


def test_counter_values_are_not_truncated_to_32_bits():
    output = f"seen={2**40} redirected={2**40} map_miss=0 fib_fallback=0 passed=0"
    assert check.read_counters(output)[:2] == (2**40, 2**40)


@pytest.mark.parametrize("output", ["", "attach failed", "seen=1 redirected=1"])
def test_missing_or_incomplete_counters_fail(output):
    with pytest.raises(RuntimeError, match="no counter sample"):
        check.read_counters(output)


def test_stop_waits_for_graceful_detachment():
    process = Mock()
    process.poll.return_value = None
    check.stop(process)
    process.send_signal.assert_called_once_with(check.signal.SIGINT)
    process.wait.assert_called_once_with(timeout=10)
    process.kill.assert_not_called()


def test_forced_cleanup_is_reported_as_failure():
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("loader", 10), 0]
    with pytest.raises(RuntimeError, match="SIGKILL"):
        check.stop(process)
    process.kill.assert_called_once()
    assert process.wait.call_count == 2


def test_completed_process_is_not_signalled():
    process = Mock()
    process.poll.return_value = 0
    check.stop(process)
    process.send_signal.assert_not_called()


def test_failed_command_preserves_diagnostics(monkeypatch):
    monkeypatch.setattr(
        check.subprocess,
        "run",
        Mock(
            return_value=Mock(
                returncode=1,
                stdout="verifier output",
                stderr="attach failed",
            )
        ),
    )
    with pytest.raises(RuntimeError, match="verifier output\nattach failed"):
        check.run(["loader"])
