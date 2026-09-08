#!/usr/bin/env python3
"""Check normal IPv4 forwarding and local INPUT/OUTPUT in an isolated topology."""

import argparse
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import paths
from check import Lab, interrupt, signals, stop

CASES = {
    "forward": ("h1", "10.73.1.1", "h2", "10.73.2.2", "eth0"),
    "input": ("h1", "10.73.1.1", "r1", "10.73.1.254", "eth0"),
    "output": ("r1", "10.73.2.254", "h2", "10.73.2.2", "eth1"),
}
RECEIVE = ("kprobe:ip_rcv", "kprobe:ip_local_deliver")
OUTPUT = ("kprobe:ip_output", "tracepoint:net:net_dev_queue")
FORWARD = ("kprobe:ip_rcv", "kprobe:ip_forward", "kprobe:ip_output", "tracepoint:net:net_dev_queue")


def evidence(case, namespace, identifier, events):
    _, source, _, destination, _ = CASES[case]
    required = {
        8: FORWARD if case == "forward" else RECEIVE if case == "input" else OUTPUT,
        0: FORWARD if case == "forward" else OUTPUT if case == "input" else RECEIVE,
    }
    gaps = []
    if any(event["namespace"] != namespace for event in events):
        gaps.append("events escaped the target namespace filter")
    for kind, endpoints in ((8, (source, destination)), (0, (destination, source))):
        for sequence in (1, 2, 3):
            packet = sorted(
                (
                    event
                    for event in events
                    if (
                        event["namespace"],
                        event["source"],
                        event["destination"],
                        event["type"],
                        event["id"],
                        event["seq"],
                    )
                    == (namespace, *endpoints, kind, identifier, sequence)
                ),
                key=lambda e: e["ts"],
            )
            observed = [event["hook"] for event in packet]
            cursor = 0
            for hook in required[kind]:
                try:
                    cursor = observed.index(hook, cursor) + 1
                except ValueError:
                    gaps.append(f"type={kind} seq={sequence}: not observed in order: {hook}")
            if case != "forward" and "kprobe:ip_forward" in observed:
                gaps.append(f"type={kind} seq={sequence}: unexpected forwarding on a local path")
    return gaps


class PathLab(Lab):
    def __init__(self, output):
        super().__init__(output)
        self.name = self.name.replace("drop-check-", "path-check-")

    def scenario(self, scenario, identifier, namespace):
        directory = self.output / scenario
        directory.mkdir()
        result = {"scenario": scenario, "id": identifier, "status": "error"}
        first_process = len(self.processes)
        try:
            log = directory / "paths.log"
            tracer = self.spawn(
                [
                    "python3",
                    str(self.source.with_name("trace.py")),
                    "--mode",
                    "path",
                    "--stacks",
                    "--seconds",
                    "60",
                    "--netns",
                    str(Path("/run/netns") / self.namespaces["r1"]),
                ],
                log,
            )
            ready = self.ready(tracer, log, f"ready netns={namespace}\n")
            if not ready and tracer.returncode != 77:
                raise RuntimeError(log.read_text())
            source_node, _, destination_node, destination, source_interface = CASES[scenario]
            captures = []
            for label, node, interface in (
                ("source", source_node, source_interface),
                ("destination", destination_node, "eth0"),
            ):
                capture_log = directory / f"{label}-capture.log"
                capture = self.spawn(
                    self.ns(
                        node,
                        "tcpdump",
                        "-n",
                        "-U",
                        "--immediate-mode",
                        "-s",
                        "160",
                        "-Z",
                        "root",
                        "-i",
                        interface,
                        "-w",
                        str(directory / f"{label}.pcap"),
                        f"icmp and (icmp[0] = 0 or icmp[0] = 8) and icmp[4:2] = {identifier}",
                    ),
                    capture_log,
                )
                if not self.ready(capture, capture_log, "listening on"):
                    raise RuntimeError(capture_log.read_text())
                captures.append(capture)
            ping = self.run(
                self.ns(
                    source_node,
                    "ping",
                    "-n",
                    "-c",
                    "3",
                    "-i",
                    "0.2",
                    "-W",
                    "1",
                    "-e",
                    str(identifier),
                    destination,
                ),
                allowed=(0, 1),
            )
            result["ping_returncode"] = ping.returncode
            time.sleep(0.3)
            for capture in captures:
                if capture.poll() is not None:
                    raise RuntimeError("tcpdump exited during observation")
                stop(capture)
                if capture.returncode:
                    raise RuntimeError("tcpdump failed")
            if ready and tracer.poll() is not None:
                raise RuntimeError("tracer exited during observation")
            stop(tracer)
            if ready and tracer.returncode:
                raise RuntimeError(log.read_text())
            counts = {}
            for label in ("source", "destination"):
                packets = self.run(
                    ["tcpdump", "-nn", "-tt", "-r", str(directory / f"{label}.pcap")]
                ).stdout
                (directory / f"{label}-packets.txt").write_text(packets)
                counts[label] = len(
                    re.findall(
                        rf"ICMP echo (?:request|reply), id {identifier}, seq [123],", packets
                    )
                )
            text = log.read_text()
            events = paths.parse(text)
            result.update(
                events=events, captures=counts, trace_status="collected" if ready else "skipped"
            )
            result["probes"] = next(
                (
                    json.loads(line.removeprefix("probes="))
                    for line in text.splitlines()
                    if line.startswith("probes=")
                ),
                {},
            )
            result["enabled_but_unseen"] = sorted(
                set(result["probes"].get("enabled", [])) - {event["hook"] for event in events}
            )
            result["gaps"] = evidence(scenario, namespace, identifier, events)
            if ready and not any(event["stack"] for event in events):
                result["gaps"].append("requested kernel stacks were not observed")
            if re.search(r"\b(lost|dropped)\s+[1-9]\d*\s+events\b", text, re.I):
                result["gaps"].append("tracer reported lost events")
            network_ok = ping.returncode == 0 and counts == {"source": 6, "destination": 6}
            result["status"] = "observed" if network_ok and not result["gaps"] else "inconclusive"
            if not ready and network_ok:
                result["status"] = "skipped"
                result["warning"] = text.strip()
            (directory / "paths.txt").write_text(paths.format_paths(events))
            (directory / "paths-stacks.txt").write_text(paths.format_paths(events, stacks=True))
        except KeyboardInterrupt:
            result["status"] = "interrupted"
            raise
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            result["error"] = str(error)
            raise
        finally:
            with signals(signal.SIG_IGN):
                for proc, _, _ in self.processes[first_process:]:
                    try:
                        stop(proc)
                    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                        result["status"] = "error"
                        result.setdefault("cleanup_errors", []).append(str(error))
                (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("all", *CASES), default="all")
    parser.add_argument(
        "--output", type=Path, help="new directory; default: /tmp/nslab-path-results-*"
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("requires root")
    os.environ["LC_ALL"] = "C"
    os.umask(0o077)
    try:
        if args.output:
            output = args.output.resolve()
            output.mkdir(parents=True, exist_ok=False)
        else:
            output = Path(tempfile.mkdtemp(prefix="nslab-path-results-", dir="/tmp"))
    except OSError as error:
        parser.error(str(error))
    print(f"results: {output}", flush=True)
    with signals(interrupt):
        result = PathLab(output).execute(
            tuple(CASES) if args.scenario == "all" else (args.scenario,)
        )
    if result.get("error"):
        print(result["error"], file=os.sys.stderr)
    return 130 if result["status"] == "interrupted" else int(result["status"] != "observed")


if __name__ == "__main__":
    raise SystemExit(main())
