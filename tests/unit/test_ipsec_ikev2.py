import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "examples/ipsec/ikev2.py"
_SPEC = importlib.util.spec_from_file_location("ipsec_ikev2", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
ikev2 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ikev2)


@pytest.mark.parametrize("node,local,remote", [("r1", 1, 2), ("r2", 2, 1)])
def test_connection_selectors_and_crypto(node, local, remote):
    config = ikev2.connection(node, "test-only-secret")
    assert f"local_addrs = 192.0.2.{local}" in config
    assert f"remote_addrs = 192.0.2.{remote}" in config
    assert f"local_ts = 10.90.{local}.0/24" in config
    assert f"remote_ts = 10.90.{remote}.0/24" in config
    assert "version = 2" in config
    assert "esp_proposals = aes128gcm16" in config
    assert "rekey_time = 5m" in config
    assert "secret = test-only-secret" in config
    assert "start_action = none" in config


def test_daemon_does_not_load_host_configuration_or_install_routes():
    assert "include" not in ikev2.DAEMON_CONFIG
    assert "install_routes = no" in ikev2.DAEMON_CONFIG
    assert "stroke" not in ikev2.DAEMON_CONFIG


def test_control_uses_private_socket_and_credentials(tmp_path, monkeypatch):
    lab = ikev2.Lab(tmp_path)
    run = Mock(return_value="ok")
    monkeypatch.setattr(lab, "command", run)
    lab.control("r1", "--list-sas")
    argv = run.call_args.args[0]
    assert argv[-2:] == ["--uri", f"unix://{tmp_path}/r1/run/charon.vici"]
    env = run.call_args.kwargs["env"]
    assert env["SWANCTL_DIR"] == str(tmp_path / "r1")
    assert env["STRONGSWAN_CONF"] == str(tmp_path / "strongswan.conf")


def test_existing_xfrm_is_refused_before_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(ikev2.shutil, "which", lambda binary: binary)
    lab = ikev2.Lab(tmp_path)
    monkeypatch.setattr(lab, "command", Mock(return_value="existing SA"))
    launch = Mock()
    monkeypatch.setattr(ikev2.subprocess, "Popen", launch)
    with pytest.raises(RuntimeError, match="XFRM is not empty"):
        lab.start()
    launch.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_missing_dependency_is_non_mutating(tmp_path, monkeypatch):
    monkeypatch.setattr(ikev2.shutil, "which", lambda binary: None)
    lab = ikev2.Lab(tmp_path)
    with pytest.raises(RuntimeError, match="missing executable"):
        lab.start()
    assert not list(tmp_path.iterdir())


def test_spi_comparison_ignores_counters_and_order():
    assert ikev2.spi_set("spi 0x1 reqid 1 bytes 100 spi 0x2") == {"0x1", "0x2"}
    assert ikev2.spi_set("spi 0x2 bytes 500 spi 0x1") == {"0x1", "0x2"}
    assert ikev2.spi_set("") == set()


def test_cleanup_signals_daemon_before_waiting_on_wrapper(tmp_path, monkeypatch):
    lab = ikev2.Lab(tmp_path)
    events = []
    lab.pidfds = [42]
    process = Mock()
    process.wait.side_effect = lambda **kwargs: events.append("wait")
    lab.processes = [process]
    monkeypatch.setattr(ikev2.signal, "pidfd_send_signal", lambda *args: events.append("term"))
    monkeypatch.setattr(ikev2.os, "close", lambda fd: events.append("close"))
    lab.close()
    assert events == ["term", "wait", "close"]


def test_cleanup_escalates_only_its_wrapper_group(tmp_path, monkeypatch):
    lab = ikev2.Lab(tmp_path)
    process = Mock(pid=12345)
    process.wait.side_effect = [
        subprocess.TimeoutExpired("charon", 10),
        subprocess.TimeoutExpired("charon", 5),
        0,
    ]
    lab.processes = [process]
    kill = Mock()
    monkeypatch.setattr(ikev2.os, "killpg", kill)
    lab.close()
    assert [call.args for call in kill.call_args_list] == [
        (12345, ikev2.signal.SIGTERM),
        (12345, ikev2.signal.SIGKILL),
    ]


def test_command_is_bounded_and_checks_exit_status(tmp_path, monkeypatch):
    lab = ikev2.Lab(tmp_path)
    run = Mock(return_value=Mock(stdout="result"))
    monkeypatch.setattr(ikev2.subprocess, "run", run)
    assert lab.command(["true"]) == "result"
    assert run.call_args.kwargs["timeout"] == 30
    assert run.call_args.kwargs["check"] is True


def test_cleanup_reports_residual_states_without_flushing(tmp_path, monkeypatch):
    lab = ikev2.Lab(tmp_path)
    lab.started_nodes = ["r1"]
    command = Mock(side_effect=["remaining SA", ""])
    monkeypatch.setattr(lab, "command", command)
    assert lab.close() == ["r1: XFRM state remains; stop processes and redeploy"]
    assert all("flush" not in call.args[0] for call in command.call_args_list)
