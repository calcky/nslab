#!/usr/bin/env python3
"""Check the drop-diagnosis portion of the kernel-path lab in a disposable topology."""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path

SCENARIOS = ("baseline", "no-route", "rp-filter", "tc-ingress")
EXPECTED = {
    "no-route": {"IP_INNOROUTES", "IP_OUTNOROUTES"},
    "rp-filter": {"IP_RPFILTER"},
    "tc-ingress": {"TC_INGRESS"},
}
COUNTERS = (
    "IpInReceives",
    "IpForwDatagrams",
    "IpInAddrErrors",
    "IpInDiscards",
    "IpOutNoRoutes",
    "IpExtInNoRoutes",
    "TcpExtIPReversePathFilter",
)
EVENT = re.compile(r"^drop ns=(\d+) ifindex=(\d+) id=(\d+) seq=(\d+) reason=(\d+) location=(.+)$")


def parse_events(text):
    names, events = {}, []
    for line in text.splitlines():
        if line.startswith("reason_names="):
            names = json.loads(line.removeprefix("reason_names="))
        elif line.startswith("drop "):
            match = EVENT.fullmatch(line)
            if not match:
                raise ValueError(f"malformed drop event: {line}")
            namespace, interface, identifier, sequence, reason = map(int, match.groups()[:5])
            events.append(
                {
                    "namespace": namespace,
                    "ifindex": interface,
                    "id": identifier,
                    "seq": sequence,
                    "reason": reason,
                    "reason_name": names.get(str(reason), f"UNKNOWN_{reason}"),
                    "location": match.group(6),
                }
            )
    return events


def classify(scenario, identifier, namespace, events, ingress, destination, ping_code):
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario}")
    matched = [
        event
        for event in events
        if event["id"] == identifier
        and event["namespace"] == namespace
        and event["seq"] in (1, 2, 3)
    ]
    if scenario == "baseline":
        return ping_code == 0 and ingress == destination == 3 and not matched
    reasons = {event["seq"] for event in matched if event["reason_name"] in EXPECTED[scenario]}
    return ping_code == 1 and ingress == 3 and destination == 0 and reasons == {1, 2, 3}


@contextmanager
def signals(handler):
    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, original in previous.items():
            signal.signal(sig, original)


def interrupt(signum, frame):
    raise KeyboardInterrupt(f"signal {signum}")


def stop(process):
    if process.poll() is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            raise RuntimeError("process required SIGKILL") from None


class Lab:
    def __init__(self, output):
        self.output = output
        self.source = Path(__file__).resolve()
        self.name = f"drop-check-{uuid.uuid4().hex[:8]}"
        self.binary = os.environ.get("NSLAB_BIN", "nslab")
        self.namespaces = {}
        self.commands = []
        self.processes = []

    def run(self, argv, allowed=(0,), timeout=45):
        record = {"argv": argv, "returncode": None}
        started = time.monotonic()
        try:
            with subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            ) as proc:
                try:
                    stdout, stderr = proc.communicate(timeout=timeout)
                except (KeyboardInterrupt, subprocess.TimeoutExpired):
                    with signals(signal.SIG_IGN):
                        stop(proc)
                    stdout, stderr = proc.communicate(timeout=5)
                    record.update(stdout=stdout, stderr=stderr)
                    raise
                record.update(stdout=stdout, stderr=stderr, returncode=proc.returncode)
            if proc.returncode not in allowed:
                raise RuntimeError(f"command failed: {argv}\n{stdout}\n{stderr}")
            return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
        except (OSError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
            record["error"] = str(error)
            raise
        finally:
            record["seconds"] = time.monotonic() - started
            self.commands.append(record)

    def cli(self, command, *args):
        argv = [
            self.binary,
            command,
            "--topo",
            str(self.source.with_name("nslab.yaml")),
            "--name",
            self.name,
            *args,
        ]
        if command == "deploy":
            pending = []
            with signals(lambda sig, frame: pending.append(sig)):
                result = self.run(argv, timeout=60)
            if pending:
                raise KeyboardInterrupt("interrupted after deploy transaction")
            return result
        return self.run(argv, timeout=60)

    def ns(self, node, *args):
        return ["ip", "netns", "exec", self.namespaces[node], *args]

    def spawn(self, argv, log):
        with log.open("w") as stream:
            process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, start_new_session=True
            )
        self.processes.append((process, argv, log))
        return process

    @staticmethod
    def ready(process, log, marker):
        deadline = time.monotonic() + 20
        while marker not in log.read_text():
            if process.poll() is not None:
                return False
            if time.monotonic() >= deadline:
                raise RuntimeError(f"observer did not become ready: {log.read_text()}")
            time.sleep(0.05)
        return True

    def ping(self, identifier):
        return self.run(
            self.ns(
                "h1",
                "ping",
                "-n",
                "-e",
                str(identifier),
                "-c",
                "3",
                "-i",
                "0.2",
                "-W",
                "1",
                "10.73.2.2",
            ),
            allowed=(0, 1),
        )

    def fault(self, scenario, restore=False):
        if scenario == "no-route":
            argv = ["ip", "route", "add" if restore else "del", "10.73.2.0/24", "dev", "eth1"]
            self.run(self.ns("r1", *argv))
        elif scenario == "rp-filter":
            self.run(
                self.ns("r1", "sysctl", "-w", f"net.ipv4.conf.eth0.rp_filter={0 if restore else 1}")
            )
            self.run(
                self.ns(
                    "r1",
                    "ip",
                    "route",
                    "del" if restore else "add",
                    "10.73.1.1/32",
                    "via",
                    "10.73.2.2",
                    "dev",
                    "eth1",
                )
            )
        elif scenario == "tc-ingress":
            self.run(
                self.ns("r1", "tc", "qdisc", "del" if restore else "add", "dev", "eth0", "clsact")
            )
            if not restore:
                self.run(
                    self.ns(
                        "r1",
                        "tc",
                        "filter",
                        "add",
                        "dev",
                        "eth0",
                        "ingress",
                        "protocol",
                        "ip",
                        "pref",
                        "10",
                        "flower",
                        "src_ip",
                        "10.73.1.1",
                        "dst_ip",
                        "10.73.2.2",
                        "ip_proto",
                        "icmp",
                        "action",
                        "drop",
                    )
                )

    def scenario(self, scenario, identifier, namespace):
        directory = self.output / scenario
        directory.mkdir()
        result = {"scenario": scenario, "id": identifier, "status": "error"}
        first_process = len(self.processes)
        try:
            self.fault(scenario)
            trace_log = directory / "drops.log"
            tracer = self.spawn(
                [
                    "python3",
                    str(self.source.with_name("trace.py")),
                    "--mode",
                    "drop",
                    "--seconds",
                    "60",
                    "--netns",
                    str(Path("/run/netns") / self.namespaces["r1"]),
                ],
                trace_log,
            )
            trace_ready = self.ready(tracer, trace_log, f"ready netns={namespace}\n")
            if not trace_ready and tracer.returncode != 77:
                raise RuntimeError(trace_log.read_text())
            foreign = None
            if trace_ready:
                foreign_log = directory / "other-netns.log"
                foreign = self.spawn(
                    [
                        "python3",
                        str(self.source.with_name("trace.py")),
                        "--mode",
                        "drop",
                        "--seconds",
                        "60",
                        "--netns",
                        str(Path("/run/netns") / self.namespaces["h2"]),
                    ],
                    foreign_log,
                )
                foreign_inode = (Path("/run/netns") / self.namespaces["h2"]).stat().st_ino
                if not self.ready(foreign, foreign_log, f"ready netns={foreign_inode}\n"):
                    raise RuntimeError(foreign_log.read_text())
            captures = []
            for node in ("r1", "h2"):
                log = directory / f"{node}-capture.log"
                proc = self.spawn(
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
                        "eth0",
                        "-w",
                        str(directory / f"{node}.pcap"),
                        "icmp and src host 10.73.1.1 and dst host 10.73.2.2 and icmp[0] = 8",
                    ),
                    log,
                )
                if not self.ready(proc, log, "listening on"):
                    raise RuntimeError(log.read_text())
                captures.append(proc)
            before = json.loads(self.run(self.ns("r1", "nstat", "-aszj")).stdout)["kernel"]
            ping = self.ping(identifier)
            time.sleep(0.3)
            after = json.loads(self.run(self.ns("r1", "nstat", "-aszj")).stdout)["kernel"]
            result.update(
                before=before,
                after=after,
                ping_returncode=ping.returncode,
                delta={
                    key: after[key] - before[key] if key in before and key in after else None
                    for key in COUNTERS
                },
            )
            result["routes"] = json.loads(
                self.run(self.ns("r1", "ip", "-j", "route", "show")).stdout
            )
            result["tc_filters"] = json.loads(
                self.run(
                    self.ns("r1", "tc", "-s", "-j", "filter", "show", "dev", "eth0", "ingress")
                ).stdout
            )
            for proc in captures:
                if proc.poll() is not None:
                    raise RuntimeError("tcpdump exited during collection")
                stop(proc)
                if proc.returncode != 0:
                    raise RuntimeError(f"tcpdump failed: {proc.returncode}")
            if trace_ready and tracer.poll() is not None:
                raise RuntimeError("tracer exited during collection")
            stop(tracer)
            if trace_ready and tracer.returncode != 0:
                raise RuntimeError(f"tracer failed: {trace_log.read_text()}")
            if foreign is not None:
                if foreign.poll() is not None:
                    raise RuntimeError("other-namespace tracer exited during collection")
                stop(foreign)
                if foreign.returncode != 0:
                    raise RuntimeError(foreign_log.read_text())
                result["other_namespace_events"] = parse_events(foreign_log.read_text())
                if result["other_namespace_events"]:
                    raise RuntimeError("unexpected matching drops in h2 namespace control")
            counts = {}
            for node in ("r1", "h2"):
                decoded = self.run(
                    ["tcpdump", "-nn", "-tt", "-r", str(directory / f"{node}.pcap")]
                ).stdout
                (directory / f"{node}-packets.txt").write_text(decoded)
                counts[node] = len(
                    re.findall(rf"ICMP echo request, id {identifier}, seq [123],", decoded)
                )
            result["captures"] = counts
            result["events"] = parse_events(trace_log.read_text())
            result["trace_status"] = "collected" if trace_ready else "skipped"
            self.fault(scenario, restore=True)
            result["recovery_returncode"] = self.ping(identifier + 1).returncode
            if result["recovery_returncode"]:
                raise RuntimeError("connectivity did not recover")
            observed = classify(
                scenario,
                identifier,
                namespace,
                result["events"],
                counts["r1"],
                counts["h2"],
                ping.returncode,
            )
            result["status"] = "observed" if observed else "inconclusive"
            if re.search(r"\b(lost|dropped)\s+\d+\s+events\b", trace_log.read_text(), re.I):
                result["status"] = "inconclusive"
                result["warning"] = "tracing reported lost events"
            if not trace_ready:
                network_ok = (
                    counts["r1"] == 3
                    and counts["h2"] == (3 if scenario == "baseline" else 0)
                    and ping.returncode == (0 if scenario == "baseline" else 1)
                )
                result["status"] = "skipped" if network_ok else "inconclusive"
                result["warning"] = trace_log.read_text().strip()
        except KeyboardInterrupt:
            result["status"] = "interrupted"
            raise
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            result["error"] = str(error)
            # A failed case may leave a fault active; stop the suite and destroy.
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

    def execute(self, selected):
        report = {
            "schema_version": 1,
            "deployment": self.name,
            "status": "error",
            "scenarios": [{"scenario": scenario, "status": "not-run"} for scenario in selected],
            "kernel": os.uname().release,
            "architecture": os.uname().machine,
            "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "manifest": self.source.with_name("nslab.yaml").read_text(),
            "sha256": {
                name: hashlib.sha256(self.source.with_name(name).read_bytes()).hexdigest()
                for name in (
                    "nslab.yaml",
                    "check.py",
                    "trace.py",
                    "drops.bt",
                    "paths.py",
                    "check_paths.py",
                )
            },
        }
        self.save(report)
        try:
            report["nslab_version"] = self.run([self.binary, "--version"]).stdout.strip()
            tracer_binary = shutil.which(os.environ.get("BPFTRACE_BIN", "bpftrace"))
            if tracer_binary:
                report["bpftrace_version"] = self.run([tracer_binary, "--version"]).stdout.strip()
            self.cli("deploy")
            report["inspect"] = json.loads(self.cli("inspect", "--format", "json").stdout)
            self.namespaces = {
                node["name"]: node["namespace"] for node in report["inspect"]["nodes"]
            }
            for interface in ("all", "default", "eth0", "eth1"):
                self.run(self.ns("r1", "sysctl", "-w", f"net.ipv4.conf.{interface}.rp_filter=0"))
            namespace = int(
                self.run(self.ns("r1", "stat", "-Lc", "%i", "/proc/self/ns/net")).stdout
            )
            report["router_netns_inode"] = namespace
            if self.ping(10000).returncode:
                raise RuntimeError("initial connectivity failed")
            for index, scenario in enumerate(selected):
                try:
                    result = self.scenario(scenario, 20000 + index * 10, namespace)
                finally:
                    result_file = self.output / scenario / "result.json"
                    if result_file.exists():
                        report["scenarios"][index]["status"] = json.loads(result_file.read_text())[
                            "status"
                        ]
                print(f"{scenario}: {result['status']}", flush=True)
                self.save(report)
            report["status"] = (
                "observed"
                if all(item["status"] == "observed" for item in report["scenarios"])
                else "incomplete"
            )
        except KeyboardInterrupt as error:
            report.update(status="interrupted", error=str(error))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            report["error"] = str(error)
        finally:
            with signals(signal.SIG_IGN):
                errors = []
                for proc, argv, log in self.processes:
                    try:
                        stop(proc)
                    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                        errors.append(str(error))
                    self.commands.append(
                        {"argv": argv, "returncode": proc.returncode, "log": str(log)}
                    )
                for attempt in range(2):
                    try:
                        self.cli("destroy")
                        final = json.loads(self.cli("inspect", "--format", "json").stdout)
                        report["final_inspect"] = final
                        if final["status"] != "absent":
                            raise RuntimeError("deployment remains after destroy")
                        remaining = {
                            line.split()[0]
                            for line in self.run(["ip", "netns", "list"]).stdout.splitlines()
                        }
                        if remaining.intersection(self.namespaces.values()):
                            raise RuntimeError("namespace remains after destroy")
                        break
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                        if attempt:
                            errors.append(str(error))
                report["cleanup_errors"] = errors
                if errors:
                    report["status"] = "error"
                self.save(report)
        return report

    def save(self, report):
        report["commands"] = self.commands
        (self.output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("all", *SCENARIOS), default="all")
    parser.add_argument(
        "--output", type=Path, help="new output directory; default: /tmp/nslab-drop-results-*"
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
            output = Path(tempfile.mkdtemp(prefix="nslab-drop-results-", dir="/tmp"))
    except OSError as error:
        parser.error(str(error))
    print(f"results: {output}", flush=True)
    with signals(interrupt):
        result = Lab(output).execute(SCENARIOS if args.scenario == "all" else (args.scenario,))
    if result.get("error"):
        print(result["error"], file=sys.stderr)
    return 130 if result["status"] == "interrupted" else int(result["status"] != "observed")


if __name__ == "__main__":
    sys.exit(main())
