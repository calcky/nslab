#!/usr/bin/env python3
"""Opt-in root smoke test for the redirect lab; never run by normal CI."""

import argparse
import json
import os
import re
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


def run(args, check=True, timeout=30):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{args}: {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result


def stop(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise RuntimeError("lab process required SIGKILL") from None


def read_counters(output):
    rows = re.findall(
        r"seen=(\d+) redirected=(\d+) map_miss=(\d+) fib_fallback=(\d+) passed=(\d+)",
        output,
    )
    if not rows:
        raise RuntimeError(f"no counter sample:\n{output}")
    return tuple(map(int, rows[-1]))


def check(root, work, advanced=False):
    binary = os.environ.get("NSLAB_BIN", "nslab")
    name = f"bpf-check-{uuid.uuid4().hex[:8]}"
    processes = []
    namespaces = {}

    def cli(command, *args):
        return run(
            [binary, command, "-t", str(root / "nslab.yaml"), "--name", name, *args], timeout=60
        )

    def ns(node, *args):
        return ["ip", "netns", "exec", namespaces[node], *args]

    try:
        print(cli("deploy").stdout, end="", flush=True)
        report = json.loads(cli("inspect", "--format", "json").stdout)
        namespaces = {node["name"]: node["namespace"] for node in report["nodes"]}
        run(ns("h1", "ping", "-c", "1", "-W", "2", "10.72.2.2"))
        for mode in ("devmap", "empty", "tc"):
            run(ns("r1", "ping", "-c", "1", "-W", "2", "10.72.2.2"))
            path = work / f"{mode}.log"
            command = ns(
                "r1",
                str(root / "redirect_lab"),
                mode,
                "eth0",
                "eth1",
                str(root / "redirect_lab.bpf.o"),
            )
            with path.open("w") as log:
                process = subprocess.Popen(command, stdout=log, stderr=log)
            processes.append(process)
            deadline = time.monotonic() + 15
            while f"attached {mode}" not in path.read_text():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError(path.read_text())
                time.sleep(0.1)
            again = run(command, check=False)
            if again.returncode != 1:
                raise RuntimeError(f"duplicate attachment not refused: {again.stdout}")
            with (work / f"{mode}-ingress.log").open("w") as log:
                ingress = subprocess.Popen(
                    ns("r1", "tcpdump", "-l", "-nn", "-i", "eth0", "icmp and icmp[0] = 8"),
                    stdout=log,
                    stderr=log,
                )
            processes.append(ingress)
            with (work / f"{mode}-ttl.log").open("w") as log:
                capture = subprocess.Popen(
                    ns(
                        "h2",
                        "tcpdump",
                        "-nn",
                        "-v",
                        "-i",
                        "eth0",
                        "-c",
                        "3",
                        "icmp and icmp[0] = 8",
                    ),
                    stdout=log,
                    stderr=log,
                )
            processes.append(capture)
            time.sleep(0.5)
            run(ns("h1", "ping", "-c", "3", "-W", "2", "10.72.2.2"))
            if capture.wait(timeout=10):
                raise RuntimeError("receiver capture failed")
            stop(ingress)
            ttl = run(ns("h1", "ping", "-t", "1", "-c", "1", "-W", "1", "10.72.2.2"), check=False)
            if "Time to live exceeded" not in ttl.stdout:
                raise RuntimeError(f"TTL expiry did not use Linux: {ttl.stdout}")
            stop(process)
            if process.returncode:
                raise RuntimeError(path.read_text())
            stats = read_counters(path.read_text())
            if stats[1:3] != ((0, 3) if mode == "empty" else (3, 0)):
                raise RuntimeError(f"unexpected counters: {stats}")
            if (work / f"{mode}-ttl.log").read_text().count("ttl 63,") != 3:
                raise RuntimeError("incorrect receiver TTL")
            requests = (work / f"{mode}-ingress.log").read_text().count("ICMP echo request")
            if requests != (0 if mode == "devmap" else 3):
                raise RuntimeError(f"unexpected ingress capture: {requests} requests")
            link = json.loads(
                run(ns("r1", "ip", "-j", "-d", "link", "show", "dev", "eth0")).stdout
            )[0]
            if link.get("xdp", {}).get("attached"):
                raise RuntimeError("XDP attachment remains")
            if "clsact" in run(ns("r1", "tc", "qdisc", "show", "dev", "eth0")).stdout:
                raise RuntimeError("clsact remains")
            print(
                f"{mode}: PASS (redirect/fallback, TTL, capture, duplicate refusal, detach)",
                flush=True,
            )
        if advanced:
            from check_cpu_xsk import check_extra

            check_extra(root, work, namespaces, processes)
    except BaseException:
        for path in sorted(work.glob("*.log")):
            print(f"{path.name}:\n{path.read_text()}")
        raise
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        errors = []
        for process in processes:
            try:
                stop(process)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                errors.append(str(error))
        print(cli("destroy").stdout, end="", flush=True)
        report = json.loads(cli("inspect", "--format", "json").stdout)
        if report["status"] != "absent":
            errors.append("topology cleanup incomplete")
        if errors:
            raise RuntimeError("; ".join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--advanced", action="store_true", help="also test CPUMAP and AF_XDP")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("run as root after make redirect; this creates its own temporary topology")
    os.environ["LC_ALL"] = "C"

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    with tempfile.TemporaryDirectory(prefix="nslab-bpf-check-", dir="/tmp") as directory:
        check(Path(__file__).resolve().parent, Path(directory), args.advanced)


if __name__ == "__main__":
    main()
