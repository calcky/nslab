import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

DIRECTORY = Path(__file__).resolve().parents[2] / "examples/kernel-path"


def load(name):
    spec = importlib.util.spec_from_file_location(f"kernel_{name}", DIRECTORY / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


paths = load("paths")
with patch.dict(sys.modules, {"paths": paths, "check": load("check")}):
    check = load("check_paths")


def test_probe_discovery_reports_missing_without_inventing_functions(tmp_path):
    (tmp_path / "available_filter_functions").write_text(
        "ip_rcv\nip_forward.isra.0\nip_output [ipv4]\n"
    )
    event = tmp_path / "events/net/net_dev_queue"
    event.mkdir(parents=True)
    (event / "format").write_text("field:void * skbaddr; offset:8;")
    probes, missing = paths.discover(tmp_path)
    assert [probe[0] for probe in probes] == [
        "tracepoint:net:net_dev_queue",
        "kprobe:ip_rcv",
        "kprobe:ip_output",
    ]
    assert "kprobe:ip_forward" in missing
    assert "tracepoint:net:netif_receive_skb" in missing


def test_no_supported_probe_is_an_explicit_error(tmp_path):
    (tmp_path / "available_filter_functions").write_text("other_function\n")
    with pytest.raises(ValueError, match="none of the supported"):
        paths.discover(tmp_path)


def test_local_output_uses_explicit_net_argument_when_device_is_null():
    program = paths.program(100, 20, [("kprobe:ip_local_out", "local-output", "arg2", "arg0")])
    assert "$skb = (struct sk_buff *)arg2" in program
    assert "$net = (struct net *)arg0" in program
    assert "$net->ns.inum == 100" in program
    assert "$skb->tail >= $skb->network_header + 28" in program
    assert "kstack" not in program
    assert "str($dev->name, 16)" in program
    assert "interval:s:20" in program


def test_stacks_are_opt_in_and_ipv4_predicate_is_bidirectional():
    program = paths.program(100, 20, [("kprobe:ip_rcv", "input", "arg0", "0")], stack=True)
    assert "kstack(12)" in program
    assert "$ip[20] == 0 || $ip[20] == 8" in program
    assert program.count("$src ==") == 6


@pytest.mark.parametrize(("namespace", "seconds"), [(0, 1), (1, 0), (1, 301), (2**64, 1)])
def test_invalid_generator_arguments(namespace, seconds):
    with pytest.raises(ValueError):
        paths.program(namespace, seconds, [])


def log_line(ts=100, skb="0xabc", ns=10, kind=8, source="10.73.1.1", destination="10.73.2.2"):
    return (
        f"path ts={ts} cpu=1 ns={ns} ifindex=2 ifname=eth0 skb={skb} "
        f"src={source} dst={destination} type={kind} id=20000 seq=1 "
        "stage=forward hook=kprobe:ip_forward\n"
    )


def test_parser_sorts_cross_cpu_events_and_keeps_stacks():
    events = paths.parse(log_line(200) + "    ip_forward+1\n    ip_rcv+123\n" + log_line(100))
    assert [event["ts"] for event in events] == [100, 200]
    assert events[1]["stack"] == ["ip_forward+1", "ip_rcv+123"]


def test_packet_identity_does_not_depend_on_skb_address():
    events = paths.parse(log_line(100) + log_line(200, skb="0xdef"))
    assert len(paths.group(events)) == 1
    assert [event["skb"] for event in paths.group(events)[0]] == ["0xabc", "0xdef"]


def test_direction_and_namespace_separate_identical_ids():
    events = paths.parse(
        log_line() + log_line(ns=11) + log_line(kind=0, source="10.73.2.2", destination="10.73.1.1")
    )
    assert len(paths.group(events)) == 3


def test_stack_display_is_optional():
    events = paths.parse(log_line() + "    some_caller+10\n")
    assert "some_caller" not in paths.format_paths(events)
    assert "some_caller" in paths.format_paths(events, stacks=True)
    assert "does not prove" in paths.format_paths([])


def test_malformed_event_is_not_silently_dropped():
    with pytest.raises(ValueError, match="malformed path"):
        paths.parse("path ts=100 cpu=1")


def test_json_mode_keeps_warnings_off_stdout(tmp_path, monkeypatch, capsys):
    log = tmp_path / "trace.log"
    log.write_text(
        'probes={"unavailable":["kprobe:ip_local_out"]}\n' + log_line() + "Lost 2 events\n"
    )
    monkeypatch.setattr(sys, "argv", ["paths.py", str(log), "--json"])
    paths.main()
    result = capsys.readouterr()
    assert len(json.loads(result.out)) == 1
    assert "Unavailable probes" in result.err and "lost events" in result.err


def packet_events(case):
    _, source, _, destination, _ = check.CASES[case]
    result = []
    for kind in (8, 0):
        hooks = (
            check.FORWARD
            if case == "forward"
            else (check.RECEIVE if (case == "input") == (kind == 8) else check.OUTPUT)
        )
        for seq in (1, 2, 3):
            for ts, hook in enumerate(hooks):
                result.append(
                    {
                        "namespace": 100,
                        "source": source if kind == 8 else destination,
                        "destination": destination if kind == 8 else source,
                        "type": kind,
                        "id": 20000,
                        "seq": seq,
                        "hook": hook,
                        "ts": ts,
                    }
                )
    return result


@pytest.mark.parametrize("case", check.CASES)
def test_required_stage_evidence_for_each_request_and_reply(case):
    events = packet_events(case)
    assert check.evidence(case, 100, 20000, events) == []
    assert check.evidence(case, 100, 20000, events[:-1])
    assert check.evidence(case, 101, 20000, events)
    assert check.evidence(case, 100, 20001, events)


def test_reversed_stage_order_is_not_a_valid_path():
    events = packet_events("forward")
    events[0]["ts"] = 999
    assert check.evidence("forward", 100, 20000, events)


def test_local_packets_must_not_have_forwarding_events():
    events = packet_events("input")
    events.append({**events[0], "hook": "kprobe:ip_forward"})
    assert any(
        "unexpected forwarding" in gap for gap in check.evidence("input", 100, 20000, events)
    )
