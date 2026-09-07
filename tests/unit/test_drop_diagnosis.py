import importlib.util
import json
import signal
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

DIRECTORY = Path(__file__).resolve().parents[2] / "examples/drop-diagnosis"


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"drop_{name}", DIRECTORY / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


trace = load_script("trace")
check = load_script("check")


@pytest.mark.parametrize("field", ["unsigned int", "enum skb_drop_reason"])
def test_reason_mapping_uses_running_kernel_numbers(field):
    text = f"field:{field} reason; offset:36; size:4; signed:0;\n"
    text += 'print fmt: __print_symbolic(REC->reason, { 8, "IP_RPFILTER" }, { 0x10, "TC_INGRESS" })'
    assert trace.reason_names(text) == {8: "IP_RPFILTER", 16: "TC_INGRESS"}


@pytest.mark.parametrize("text", ["", "field:void * location;", "field:int reason;"])
def test_missing_reason_support_is_explicit(text):
    with pytest.raises(ValueError):
        trace.reason_names(text)


def test_render_scopes_by_skb_device_namespace_not_current_process():
    program = trace.render((DIRECTORY / "drops.bt").read_text(), 4026541234, 20)
    assert "__NETNS__" not in program and "__SECONDS__" not in program
    assert "$dev->nd_net.net->ns.inum == 4026541234" in program
    assert "interval:s:20" in program
    assert "$ip[20] == 8" in program
    assert "pid ==" not in program
    assert "$skb->tail >= $skb->network_header + 28" in program


@pytest.mark.parametrize(("namespace", "seconds"), [(0, 20), (-1, 20), (1, 0), (1, 301)])
def test_invalid_template_arguments_are_rejected(namespace, seconds):
    with pytest.raises(ValueError):
        trace.render("__NETNS__ __SECONDS__", namespace, seconds)


def test_events_keep_packet_identity_and_unknown_reasons():
    text = 'reason_names={"16":"IP_RPFILTER"}\nready netns=100\n'
    text += "drop ns=100 ifindex=2 id=20000 seq=1 reason=16 location=ip_rcv_finish_core+0x20\n"
    text += "drop ns=100 ifindex=2 id=20000 seq=2 reason=999 location=0xffff1234\n"
    events = check.parse_events(text)
    assert events[0] == {
        "namespace": 100,
        "ifindex": 2,
        "id": 20000,
        "seq": 1,
        "reason": 16,
        "reason_name": "IP_RPFILTER",
        "location": "ip_rcv_finish_core+0x20",
    }
    assert events[1]["reason_name"] == "UNKNOWN_999"


def test_truncated_event_does_not_silently_pass():
    with pytest.raises(ValueError, match="malformed drop event"):
        check.parse_events("drop ns=100 ifindex=2")


def events(reason="IP_RPFILTER", namespace=100, identifier=20000):
    return [
        {"namespace": namespace, "id": identifier, "seq": seq, "reason_name": reason}
        for seq in (1, 2, 3)
    ]


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("no-route", "IP_INNOROUTES"),
        ("no-route", "IP_OUTNOROUTES"),
        ("rp-filter", "IP_RPFILTER"),
        ("tc-ingress", "TC_INGRESS"),
    ],
)
def test_observation_requires_matching_reasons(scenario, reason):
    assert check.classify(scenario, 20000, 100, events(reason), 3, 0, 1)
    assert not check.classify(scenario, 20000, 100, events("NOT_SPECIFIED"), 3, 0, 1)


def test_ping_failure_or_unrelated_trace_is_not_proof():
    assert not check.classify("rp-filter", 20000, 100, [], 3, 0, 1)
    assert not check.classify("rp-filter", 20000, 100, events(namespace=200), 3, 0, 1)
    assert not check.classify("rp-filter", 20000, 100, events(identifier=30000), 3, 0, 1)
    assert not check.classify("rp-filter", 20000, 100, events()[:2], 3, 0, 1)
    assert not check.classify("rp-filter", 20000, 100, events(), 3, 1, 1)
    assert not check.classify("rp-filter", 20000, 100, events(), 3, 0, 2)


def test_baseline_needs_delivery_and_no_matching_drops():
    assert check.classify("baseline", 20000, 100, [], 3, 3, 0)
    assert not check.classify("baseline", 20000, 100, events(), 3, 3, 0)
    assert not check.classify("baseline", 20000, 100, [], 0, 0, 0)
    with pytest.raises(ValueError):
        check.classify("unknown", 20000, 100, [], 0, 0, 0)


def test_ping_has_count_without_deadline_semantics(tmp_path):
    lab = check.Lab(tmp_path)
    lab.namespaces = {"h1": "isolated-h1"}
    lab.run = Mock()
    lab.ping(20000)
    argv = lab.run.call_args.args[0]
    assert argv[:4] == ["ip", "netns", "exec", "isolated-h1"]
    assert argv[argv.index("-c") + 1] == "3"
    assert "-w" not in argv


@pytest.mark.parametrize("scenario", check.SCENARIOS)
def test_fault_and_restore_are_scoped_to_router_namespace(tmp_path, scenario):
    lab = check.Lab(tmp_path)
    lab.namespaces = {"r1": "isolated-r1"}
    lab.run = Mock()
    lab.fault(scenario)
    lab.fault(scenario, restore=True)
    assert all(
        call.args[0][:4] == ["ip", "netns", "exec", "isolated-r1"]
        for call in lab.run.call_args_list
    )
    if scenario == "rp-filter":
        commands = [call.args[0] for call in lab.run.call_args_list]
        assert any("net.ipv4.conf.eth0.rp_filter=1" in command for command in commands)
        assert any("net.ipv4.conf.eth0.rp_filter=0" in command for command in commands)


def test_deploy_defers_sigterm(tmp_path):
    lab = check.Lab(tmp_path)

    def run(argv, timeout):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return "deployed"

    lab.run = Mock(side_effect=run)
    with pytest.raises(KeyboardInterrupt, match="after deploy"):
        lab.cli("deploy")


def test_forced_process_stop_is_reported(monkeypatch):
    process = Mock(pid=123)
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("tracer", 5), 0]
    monkeypatch.setattr(check.os, "killpg", Mock())
    with pytest.raises(RuntimeError, match="SIGKILL"):
        check.stop(process)
    assert check.os.killpg.call_args_list[-1].args == (123, signal.SIGKILL)


def test_failed_deploy_still_destroys_and_preserves_diagnostics(tmp_path):
    lab = check.Lab(tmp_path)
    lab.run = Mock(return_value=Mock(stdout=""))
    lab.cli = Mock(
        side_effect=[RuntimeError("deployment failed"), Mock(), Mock(stdout='{"status":"absent"}')]
    )
    result = lab.execute(("baseline",))
    assert result["status"] == "error"
    assert result["error"] == "deployment failed"
    assert result["cleanup_errors"] == []
    assert [call.args[0] for call in lab.cli.call_args_list] == ["deploy", "destroy", "inspect"]
    assert json.loads((tmp_path / "summary.json").read_text())["status"] == "error"
