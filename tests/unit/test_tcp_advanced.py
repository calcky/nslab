import importlib.util
import json
import signal
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "examples/tcp-behavior/advanced.py"
_SPEC = importlib.util.spec_from_file_location("tcp_advanced", _SCRIPT)
assert _SPEC and _SPEC.loader
lab = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lab)


def test_payload_is_mss_aligned_and_uniquely_marked():
    data = lab.payload(8)
    assert len(data) == 8 * lab.MSS
    for index in range(8):
        segment = data[index * lab.MSS : (index + 1) * lab.MSS]
        assert segment[:4] == index.to_bytes(4, "big")
        assert segment[4:] == bytes([65 + index]) * (lab.MSS - 4)


def test_missing_counters_are_unknown_not_zero():
    result = lab.deltas({"TcpRetransSegs": 9}, {"TcpRetransSegs": 12})
    assert result["TcpRetransSegs"] == 3
    assert result["TcpExtTCPTimeouts"] is None


@pytest.mark.parametrize("scenario", lab.SCENARIOS)
def test_missing_evidence_is_never_observed(scenario):
    assert not lab.observed(scenario, {}, {})


@pytest.mark.parametrize(
    ("scenario", "sender", "receiver"),
    [
        ("sack", {"TcpExtTCPSackRecovery": 1}, {}),
        ("no-sack", {"TcpExtTCPSackRecovery": 0, "TcpRetransSegs": 2}, {}),
        ("dsack", {"TcpExtTCPDSACKRecv": 8}, {"TcpExtTCPDSACKOldSent": 8}),
        ("no-dsack", {"TcpExtTCPDSACKRecv": 0}, {"TcpExtTCPDSACKOldSent": 0}),
        ("rack", {"TcpRetransSegs": 1, "TcpExtTCPTimeouts": 0, "TcpExtTCPLossProbes": 0}, {}),
        ("rack-zero", {"TcpRetransSegs": 1, "TcpExtTCPLossProbes": 0}, {}),
        ("tlp", {"TcpExtTCPLossProbes": 1}, {}),
        ("no-tlp", {"TcpExtTCPLossProbes": 0, "TcpExtTCPTimeouts": 1}, {}),
        ("ecn", {"TcpExtTCPDeliveredCE": 1}, {"IpExtInCEPkts": 16}),
        ("no-ecn", {"TcpExtTCPDeliveredCE": 0}, {"IpExtInCEPkts": 0}),
    ],
)
def test_scenario_evidence(scenario, sender, receiver):
    assert lab.observed(scenario, sender, receiver)


def test_generic_retransmission_does_not_prove_rack_or_tlp():
    sender = {"TcpRetransSegs": 1, "TcpExtTCPTimeouts": 1, "TcpExtTCPLossProbes": 0}
    assert not lab.observed("rack", sender, {})
    assert not lab.observed("tlp", sender, {})
    with pytest.raises(ValueError, match="unknown scenario"):
        lab.observed("unknown", {}, {})


def test_loss_requires_every_target_to_be_hit():
    action = {"kind": "gact", "stats": {"drops": 1, "packets": 2}}
    rule = {"options": {"actions": [action]}}
    assert not lab.injection_observed("sack", [rule])
    assert lab.injection_observed("sack", [{}, rule, rule])
    assert lab.injection_observed("rack", [rule])


def test_ecn_marker_requires_expected_matches():
    rule = {"options": {"actions": [{"kind": "pedit", "stats": {"packets": 4}}]}}
    assert lab.injection_observed("ecn", [rule])
    assert not lab.injection_observed("no-ecn", [rule])
    rule["options"]["actions"][0]["stats"]["packets"] = 0
    assert lab.injection_observed("no-ecn", [rule])
    assert not lab.injection_observed("ecn", [rule])
    assert not lab.injection_observed("no-ecn", [])


@pytest.mark.parametrize("scenario", ["sack", "rack-zero", "ecn"])
def test_setup_modifies_only_resolved_namespaces_and_injects_on_ingress(tmp_path, scenario):
    experiment = lab.Experiment(tmp_path, scenario)
    report = {"nodes": [{"name": node, "namespace": f"isolated-{node}"} for node in ("h1", "h2")]}
    experiment.cli = Mock(side_effect=["deployed", json.dumps(report)])
    experiment.run = Mock(return_value="")
    assert experiment.setup() == report
    commands = [call.args[0] for call in experiment.run.call_args_list]
    assert all(command[:3] == ["ip", "netns", "exec"] for command in commands)
    filters = [command for command in commands if "filter" in command]
    assert filters
    assert all(command[3] == "isolated-h2" and "ingress" in command for command in filters)
    if scenario == "ecn":
        marker = filters[0]
        assert marker[marker.index("retain") + 1] == "0x03"
    if scenario == "rack-zero":
        assert any("net.ipv4.tcp_recovery=0" in command for command in commands)


def test_timeout_preserves_partial_output(tmp_path, monkeypatch):
    experiment = lab.Experiment(tmp_path, "sack")
    process = Mock(pid=123)
    process.communicate.side_effect = [
        subprocess.TimeoutExpired(["example"], 1),
        ("partial", "diagnostic"),
    ]
    context = Mock()
    context.__enter__ = Mock(return_value=process)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(lab.subprocess, "Popen", Mock(return_value=context))
    monkeypatch.setattr(lab.os, "killpg", Mock())
    with pytest.raises(subprocess.TimeoutExpired):
        experiment.run(["example"], timeout=1)
    assert experiment.commands[0]["stdout"] == "partial"
    assert experiment.commands[0]["stderr"] == "diagnostic"
    assert experiment.commands[0]["returncode"] is None
    lab.os.killpg.assert_called_once_with(123, signal.SIGINT)


def test_failed_setup_still_destroys_and_records_result(tmp_path):
    experiment = lab.Experiment(tmp_path, "sack")
    experiment.setup = Mock(side_effect=RuntimeError("injection failed"))
    experiment.cli = Mock(side_effect=["destroyed", '{"status":"absent"}'])
    experiment.run = Mock(return_value="")
    result = experiment.execute()
    assert result["status"] == "error"
    assert result["error"] == "injection failed"
    assert experiment.cli.call_args_list[0].args == ("destroy",)
    assert json.loads((tmp_path / "result.json").read_text())["cleanup_errors"] == []


def test_interrupt_still_cleans_up_and_restores_signal_handlers(tmp_path):
    previous = signal.getsignal(signal.SIGTERM)
    experiment = lab.Experiment(tmp_path, "sack")
    experiment.setup = Mock(side_effect=KeyboardInterrupt())
    experiment.cli = Mock(side_effect=["destroyed", '{"status":"absent"}'])
    experiment.run = Mock(return_value="")
    with lab.signals(lab.interrupted):
        result = experiment.execute()
        assert signal.getsignal(signal.SIGTERM) is lab.interrupted
    assert result["interrupted"]
    assert result["status"] == "interrupted"
    assert signal.getsignal(signal.SIGTERM) == previous


def test_deploy_defers_interrupt_until_transaction_finishes(tmp_path):
    experiment = lab.Experiment(tmp_path, "sack")

    def deploy(command, timeout):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert command[1] == "deploy"
        return "deployed"

    experiment.run = Mock(side_effect=deploy)
    with pytest.raises(KeyboardInterrupt, match="after deploy"):
        experiment.cli("deploy")


def test_cleanup_retries_destroy_and_reports_namespace_leak(tmp_path):
    experiment = lab.Experiment(tmp_path, "sack")
    experiment.namespaces = {"h1": "isolated-h1"}
    experiment.cli = Mock(side_effect=["destroyed", '{"status":"absent"}'] * 2)
    experiment.run = Mock(return_value="isolated-h1 (id: 0)\n")
    result = {"status": "observed"}
    experiment.cleanup(result)
    assert experiment.cli.call_count == 4
    assert result["status"] == "error"
    assert result["cleanup_errors"] == ["namespace remains after destroy"]
