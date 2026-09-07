#!/usr/bin/env python3
"""Repeatable TCP mechanism experiments in disposable nslab namespaces."""

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

MSS = 512
PORT = 9100
SCENARIOS = (
    "sack",
    "no-sack",
    "dsack",
    "no-dsack",
    "rack",
    "rack-zero",
    "tlp",
    "no-tlp",
    "ecn",
    "no-ecn",
)
COUNTERS = (
    "TcpRetransSegs",
    "TcpExtTCPSackRecovery",
    "TcpExtTCPRenoRecovery",
    "TcpExtTCPOFOQueue",
    "TcpExtTCPDSACKOldSent",
    "TcpExtTCPDSACKRecv",
    "TcpExtTCPTimeouts",
    "TcpExtTCPLossProbes",
    "TcpExtTCPLossProbeRecovery",
    "TcpExtTCPDeliveredCE",
    "IpExtInCEPkts",
)
LOSS_TARGETS = {
    "sack": (2, 4),
    "no-sack": (2, 4),
    "rack": (1,),
    "rack-zero": (1,),
    "tlp": (2, 3),
    "no-tlp": (2, 3),
}


def interrupted(signum, frame):
    raise KeyboardInterrupt(f"interrupted by signal {signum}")


@contextmanager
def signals(handler):
    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, original in previous.items():
            signal.signal(sig, original)


def payload(chunks):
    return b"".join(
        index.to_bytes(4, "big") + bytes([65 + index]) * (MSS - 4) for index in range(chunks)
    )


def worker(role, chunks, rounds):
    body = payload(chunks) * rounds
    expected = hashlib.sha256(body).hexdigest()
    with socket.socket() as sock:
        sock.settimeout(20)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, MSS)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, b"reno")
        if role == "receiver":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("10.70.0.2", PORT))
            sock.listen(1)
            print("listening", flush=True)
            connection, _ = sock.accept()
            with connection:
                connection.settimeout(20)
                data = bytearray()
                while len(data) < len(body):
                    chunk = connection.recv(len(body) - len(data))
                    if not chunk:
                        raise RuntimeError("premature EOF")
                    data.extend(chunk)
                digest = hashlib.sha256(data).hexdigest()
                if digest != expected:
                    raise RuntimeError("receiver payload mismatch")
                connection.sendall((digest + "\n").encode())
            print(json.dumps({"received": len(data), "sha256": digest}), flush=True)
        else:
            sock.connect(("10.70.0.2", PORT))
            actual_mss = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG)
            if actual_mss != MSS:
                raise RuntimeError(f"expected MSS {MSS}, got {actual_mss}")
            start = time.monotonic()
            for iteration in range(rounds):
                if iteration:
                    time.sleep(0.2)
                sock.sendall(payload(chunks))
            # Do not send FIN into a tail-loss gap before recovery completes.
            response = bytearray()
            while not response.endswith(b"\n"):
                part = sock.recv(128)
                if not part:
                    raise RuntimeError("missing receiver digest")
                response.extend(part)
            if response.decode().strip() != expected:
                raise RuntimeError("receiver digest mismatch")
            print(
                json.dumps(
                    {
                        "sent": len(body),
                        "sha256": expected,
                        "mss": actual_mss,
                        "seconds": time.monotonic() - start,
                    }
                ),
                flush=True,
            )


def deltas(before, after):
    return {
        key: after[key] - before[key] if key in before and key in after else None
        for key in COUNTERS
    }


def observed(scenario, sender, receiver):
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario}")

    def positive(values, key):
        return values.get(key) is not None and values[key] > 0

    if scenario == "sack":
        return positive(sender, "TcpExtTCPSackRecovery")
    if scenario == "no-sack":
        return sender.get("TcpExtTCPSackRecovery") == 0 and positive(sender, "TcpRetransSegs")
    if scenario == "dsack":
        return positive(sender, "TcpExtTCPDSACKRecv") and positive(
            receiver, "TcpExtTCPDSACKOldSent"
        )
    if scenario == "no-dsack":
        return sender.get("TcpExtTCPDSACKRecv") == 0 and receiver.get("TcpExtTCPDSACKOldSent") == 0
    if scenario == "rack":
        return (
            positive(sender, "TcpRetransSegs")
            and sender.get("TcpExtTCPTimeouts") == 0
            and sender.get("TcpExtTCPLossProbes") == 0
        )
    if scenario == "rack-zero":
        return positive(sender, "TcpRetransSegs") and sender.get("TcpExtTCPLossProbes") == 0
    if scenario == "tlp":
        return positive(sender, "TcpExtTCPLossProbes")
    if scenario == "no-tlp":
        return sender.get("TcpExtTCPLossProbes") == 0 and positive(sender, "TcpExtTCPTimeouts")
    if scenario == "ecn":
        return positive(receiver, "IpExtInCEPkts") and positive(sender, "TcpExtTCPDeliveredCE")
    return receiver.get("IpExtInCEPkts") == 0 and sender.get("TcpExtTCPDeliveredCE") == 0


def injection_observed(scenario, filters):
    actions = [action for rule in filters for action in rule.get("options", {}).get("actions", [])]
    if scenario in LOSS_TARGETS:
        return sum(
            action.get("kind") == "gact" and action.get("stats", {}).get("drops", 0) > 0
            for action in actions
        ) == len(LOSS_TARGETS[scenario])
    if scenario in ("ecn", "no-ecn"):
        markers = [action for action in actions if action.get("kind") == "pedit"]
        return bool(markers) and all(
            (action.get("stats", {}).get("packets", 0) > 0) == (scenario == "ecn")
            for action in markers
        )
    return True


class Experiment:
    def __init__(self, directory, scenario):
        self.directory = directory
        self.scenario = scenario
        self.name = f"tcp-{scenario}-{uuid.uuid4().hex[:8]}"
        self.binary = os.environ.get("NSLAB_BIN", "nslab")
        self.source = Path(__file__).resolve()
        self.commands = []
        self.processes = []
        self.namespaces = {}

    def run(self, command, timeout=45):
        started = time.monotonic()
        try:
            with subprocess.Popen(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            ) as process:
                try:
                    stdout, stderr = process.communicate(timeout=timeout)
                except (KeyboardInterrupt, subprocess.TimeoutExpired) as error:
                    with signals(signal.SIG_IGN):
                        os.killpg(process.pid, signal.SIGINT)
                        try:
                            stdout, stderr = process.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            stdout, stderr = process.communicate(timeout=5)
                    error.stdout, error.stderr = stdout, stderr
                    raise
                result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:

            def decoded(value):
                return value.decode(errors="replace") if isinstance(value, bytes) else value or ""

            self.commands.append(
                {
                    "argv": command,
                    "returncode": None,
                    "seconds": time.monotonic() - started,
                    "stdout": decoded(getattr(error, "stdout", "")),
                    "stderr": decoded(getattr(error, "stderr", "")),
                    "error": str(error),
                }
            )
            raise
        self.commands.append(
            {
                "argv": command,
                "returncode": result.returncode,
                "seconds": time.monotonic() - started,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        if result.returncode:
            raise RuntimeError(f"{command}: {result.returncode}\n{result.stdout}\n{result.stderr}")
        return result.stdout

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
            # Interrupting nslab during its state transaction can prevent destroy.
            pending = []
            with signals(lambda signum, frame: pending.append(signum)):
                output = self.run(argv, timeout=60)
            if pending:
                raise KeyboardInterrupt(f"interrupted by signal {pending[0]} after deploy")
            return output
        return self.run(argv, timeout=60)

    def ns(self, node, *args):
        return ["ip", "netns", "exec", self.namespaces[node], *args]

    def spawn(self, command, log):
        with log.open("w") as output:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=output, stderr=output
            )
        self.processes.append((process, command, log))
        return process

    def wait_ready(self, process, path, text):
        deadline = time.monotonic() + 10
        while text not in path.read_text():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError(f"process did not become ready:\n{path.read_text()}")
            time.sleep(0.05)

    def stats(self, node):
        return json.loads(self.run(self.ns(node, "nstat", "-aszj")))["kernel"]

    def setup(self):
        self.cli("deploy")
        report = json.loads(self.cli("inspect", "--format", "json"))
        self.namespaces = {node["name"]: node["namespace"] for node in report["nodes"]}
        for node in ("h1", "h2"):
            self.run(
                self.ns(
                    node,
                    "ethtool",
                    "-K",
                    "eth0",
                    "tso",
                    "off",
                    "gso",
                    "off",
                    "gro",
                    "off",
                    "tx",
                    "off",
                    "rx",
                    "off",
                )
            )
            settings = {
                "tcp_timestamps": 0,
                "tcp_sack": int(self.scenario != "no-sack"),
                "tcp_dsack": int(self.scenario != "no-dsack"),
                "tcp_recovery": int(self.scenario != "rack-zero"),
                "tcp_early_retrans": 0 if self.scenario in ("rack", "rack-zero", "no-tlp") else 3,
                "tcp_ecn": int(self.scenario == "ecn"),
            }
            for key, value in settings.items():
                self.run(self.ns(node, "sysctl", "-w", f"net.ipv4.{key}={value}"))
            netem = ["tc", "qdisc", "add", "dev", "eth0", "root", "netem", "delay", "20ms"]
            if node == "h1" and self.scenario in ("dsack", "no-dsack"):
                netem += ["duplicate", "100%"]
            self.run(self.ns(node, *netem))
        self.run(self.ns("h2", "tc", "qdisc", "add", "dev", "eth0", "clsact"))
        # Receiver ingress avoids local TX-drop feedback masking network loss.
        targets = LOSS_TARGETS.get(self.scenario, ())
        for number, target in enumerate(targets, 1):
            # With timestamps/offloads disabled, IPv4 and data TCP headers are 20 bytes each.
            self.run(
                self.ns(
                    "h2",
                    "tc",
                    "filter",
                    "add",
                    "dev",
                    "eth0",
                    "ingress",
                    "protocol",
                    "ip",
                    "pref",
                    str(number),
                    "u32",
                    "match",
                    "ip",
                    "protocol",
                    "6",
                    "0xff",
                    "match",
                    "ip",
                    "dport",
                    str(PORT),
                    "0xffff",
                    "match",
                    "u8",
                    "0x10",
                    "0x12",
                    "at",
                    "33",
                    "match",
                    "u32",
                    hex(target),
                    "0xffffffff",
                    "at",
                    "40",
                    "action",
                    "gact",
                    "drop",
                    "random",
                    "determ",
                    "pass",
                    "2",
                )
            )
        if self.scenario in ("ecn", "no-ecn"):
            self.run(
                self.ns(
                    "h2",
                    "tc",
                    "filter",
                    "add",
                    "dev",
                    "eth0",
                    "ingress",
                    "protocol",
                    "ip",
                    "pref",
                    "1",
                    "u32",
                    "match",
                    "ip",
                    "protocol",
                    "6",
                    "0xff",
                    "match",
                    "ip",
                    "dport",
                    str(PORT),
                    "0xffff",
                    "match",
                    "u8",
                    "0x02",
                    "0x03",
                    "at",
                    "1",
                    "action",
                    "pedit",
                    "ex",
                    "munge",
                    "ip",
                    "dsfield",
                    "set",
                    "0x03",
                    "retain",
                    "0x03",
                    "pipe",
                    "action",
                    "csum",
                    "ip4h",
                )
            )
        return report

    def execute(self):
        result = {"scenario": self.scenario, "deployment": self.name, "status": "error"}
        (self.directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        try:
            result["inspect"] = self.setup()
            for node in ("h1", "h2"):
                log = self.directory / f"{node}-capture.log"
                process = self.spawn(
                    self.ns(
                        node,
                        "tcpdump",
                        "-n",
                        "-U",
                        "-s",
                        "160",
                        "-Z",
                        "root",
                        "-i",
                        "eth0",
                        "-w",
                        str(self.directory / f"{node}.pcap"),
                        f"tcp port {PORT}",
                    ),
                    log,
                )
                self.wait_ready(process, log, "listening on")
            before = {node: self.stats(node) for node in ("h1", "h2")}
            result["before"] = before
            chunks = (
                3
                if self.scenario in ("rack", "rack-zero")
                else 4
                if self.scenario in ("tlp", "no-tlp")
                else 8
            )
            rounds = 2 if self.scenario in ("ecn", "no-ecn") else 1
            arguments = [str(self.source), "--chunks", str(chunks), "--rounds", str(rounds)]
            log = self.directory / "receiver.log"
            receiver = self.spawn(self.ns("h2", "python3", *arguments, "--worker", "receiver"), log)
            self.wait_ready(receiver, log, "listening")
            result["transfer"] = json.loads(
                self.run(self.ns("h1", "python3", *arguments, "--worker", "sender"), timeout=30)
            )
            if receiver.wait(timeout=5):
                raise RuntimeError(log.read_text())
            time.sleep(0.3)
            after = {node: self.stats(node) for node in ("h1", "h2")}
            result["before"] = before
            result["after"] = after
            result["delta"] = {node: deltas(before[node], after[node]) for node in before}
            result["filters"] = json.loads(
                self.run(
                    self.ns("h2", "tc", "-s", "-j", "filter", "show", "dev", "eth0", "ingress")
                )
            )
            result["injection_observed"] = injection_observed(self.scenario, result["filters"])
            result["status"] = (
                "observed"
                if result["injection_observed"]
                and observed(self.scenario, result["delta"]["h1"], result["delta"]["h2"])
                else "inconclusive"
            )
            if self.scenario == "rack":
                result["note"] = (
                    "Non-RTO recovery with RACK enabled; not a function-level RACK trace."
                )
            if self.scenario == "rack-zero":
                result["note"] = (
                    "tcp_recovery=0 parameter control, NOT proof that RACK is disabled. "
                    "Recent kernels ignore bit 0; consult the running kernel documentation."
                )
            result["recovery"] = (
                "RTO observed"
                if result["delta"]["h1"].get("TcpExtTCPTimeouts", 0)
                else "no RTO observed"
            )
        except KeyboardInterrupt as error:
            result["status"] = "interrupted"
            result["interrupted"] = True
            result["error"] = str(error) or "interrupted"
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, KeyError) as error:
            result["error"] = str(error)
        finally:
            with signals(signal.SIG_IGN):
                self.cleanup(result)
        return result

    def cleanup(self, result):
        cleanup_errors = []
        for process, command, log in reversed(self.processes):
            try:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                        cleanup_errors.append("process required SIGKILL")
                self.commands.append(
                    {"argv": command, "returncode": process.returncode, "log": str(log)}
                )
            except (OSError, subprocess.SubprocessError) as error:
                cleanup_errors.append(str(error))
        try:
            for node in ("h1", "h2"):
                pcap = self.directory / f"{node}.pcap"
                if pcap.exists():
                    decoded = self.run(["tcpdump", "-nn", "-tt", "-S", "-v", "-r", str(pcap)])
                    (self.directory / f"{node}-packets.txt").write_text(decoded)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            cleanup_errors.append(str(error))
        for attempt in range(2):
            try:
                self.cli("destroy")
                if json.loads(self.cli("inspect", "--format", "json"))["status"] != "absent":
                    raise RuntimeError("topology not absent after destroy")
                remaining = {
                    line.split()[0] for line in self.run(["ip", "netns", "list"]).splitlines()
                }
                if remaining.intersection(self.namespaces.values()):
                    raise RuntimeError("namespace remains after destroy")
                break
            except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
                if attempt:
                    cleanup_errors.append(str(error))
        result["cleanup_errors"] = cleanup_errors
        if cleanup_errors:
            result["status"] = "error"
        result["commands"] = self.commands
        (self.directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("all", *SCENARIOS), default="all")
    parser.add_argument(
        "--output", type=Path, help="new output directory; default: /tmp/nslab-tcp-results-*"
    )
    parser.add_argument("--worker", choices=("receiver", "sender"), help=argparse.SUPPRESS)
    parser.add_argument("--chunks", type=int, default=8, help=argparse.SUPPRESS)
    parser.add_argument("--rounds", type=int, default=1, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        if not 1 <= args.chunks <= 32 or not 1 <= args.rounds <= 8:
            parser.error("worker chunks must be 1..32 and rounds 1..8")
        worker(args.worker, args.chunks, args.rounds)
        return 0
    if os.geteuid() != 0:
        parser.error("requires root; creates isolated temporary topologies")
    os.environ["LC_ALL"] = "C"
    os.umask(0o077)
    if args.output:
        output = args.output.resolve()
        try:
            output.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            parser.error(str(error))
    else:
        output = Path(tempfile.mkdtemp(prefix="nslab-tcp-results-", dir="/tmp"))
    print(f"results: {output}", flush=True)
    source = Path(__file__).resolve()
    version = subprocess.run(
        [os.environ.get("NSLAB_BIN", "nslab"), "--version"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    summary = {
        "schema_version": 1,
        "kernel": os.uname().release,
        "architecture": os.uname().machine,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "nslab_version": version.stdout.strip(),
        "version_returncode": version.returncode,
        "manifest": source.with_name("nslab.yaml").read_text(),
        "manifest_sha256": hashlib.sha256(source.with_name("nslab.yaml").read_bytes()).hexdigest(),
        "collector_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "scenarios": [],
    }
    for scenario in SCENARIOS if args.scenario == "all" else (args.scenario,):
        directory = output / scenario
        directory.mkdir()
        with signals(interrupted):
            result = Experiment(directory, scenario).execute()
        summary["scenarios"].append({"scenario": scenario, "status": result["status"]})
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(f"{scenario}: {result['status']}", flush=True)
        if result.get("interrupted"):
            return 130
    return int(any(item["status"] != "observed" for item in summary["scenarios"]))


if __name__ == "__main__":
    sys.exit(main())
