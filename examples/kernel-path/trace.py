#!/usr/bin/env python3
"""Trace lab ICMP paths or drops from the host, filtered by the target namespace."""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import paths


def reason_names(text):
    if not re.search(r"field:[^;]*\breason;", text):
        raise ValueError("kfree_skb has no reason field; requires Linux 5.17+ with drop reasons")
    names = {
        int(number, 0): name
        for number, name in re.findall(
            r'\{\s*(0x[0-9a-fA-F]+|[0-9]+)\s*,\s*"([A-Z0-9_]+)"\s*\}', text
        )
    }
    if not names:
        raise ValueError("kernel trace format has no symbolic drop reason table")
    return names


def render(template, namespace, seconds):
    if not 1 <= namespace < 2**64 or not 1 <= seconds <= 300:
        raise ValueError("invalid namespace inode or duration")
    return template.replace("__NETNS__", str(namespace)).replace("__SECONDS__", str(seconds))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seconds", type=int, default=20, help="trace duration, 1..300 (default 20)"
    )
    parser.add_argument("--name", default="kernel-path", help="nslab deployment name")
    parser.add_argument("--mode", choices=("path", "drop"), default="path")
    parser.add_argument("--stacks", action="store_true", help="collect kernel stacks in path mode")
    parser.add_argument("--node", default="r1", help="target node (default r1)")
    parser.add_argument(
        "--netns", type=Path, help="explicit namespace handle instead of nslab lookup"
    )
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300:
        parser.error("--seconds must be 1..300")
    if os.geteuid() != 0:
        parser.error("requires root")
    binary = shutil.which(os.environ.get("BPFTRACE_BIN", "bpftrace"))
    roots = (
        Path("/sys/kernel/tracing"),
        Path("/sys/kernel/debug/tracing"),
    )
    try:
        if not binary:
            raise ValueError("bpftrace not found; install it separately or set BPFTRACE_BIN")
        if not Path("/sys/kernel/btf/vmlinux").exists():
            raise ValueError("kernel BTF is unavailable")
        root = next(
            (path for path in roots if (path / "available_filter_functions").exists()), None
        )
        if root is None:
            raise ValueError("tracing filesystem unavailable; tracefs must already be mounted")
        if args.mode == "drop":
            names = reason_names((root / "events/skb/kfree_skb/format").read_text())
        else:
            probes, missing = paths.discover(root)
    except (OSError, ValueError) as error:
        print(f"skipped: {error}", file=sys.stderr)
        return 77
    try:
        handle = args.netns
        if handle is None:
            result = subprocess.run(
                [
                    os.environ.get("NSLAB_BIN", "nslab"),
                    "inspect",
                    "--name",
                    args.name,
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            report = json.loads(result.stdout)
            node = next(node for node in report["nodes"] if node["name"] == args.node)
            handle = Path("/run/netns") / node["namespace"]
        namespace = handle.stat().st_ino
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as error:
        parser.error(f"cannot resolve deployed target namespace: {error}")
    if args.mode == "drop":
        program = render(Path(__file__).with_name("drops.bt").read_text(), namespace, args.seconds)
        print("reason_names=" + json.dumps(names, sort_keys=True), flush=True)
    else:
        program = paths.program(namespace, args.seconds, probes, args.stacks)
        print(
            "probes="
            + json.dumps({"enabled": [probe[0] for probe in probes], "unavailable": missing}),
            flush=True,
        )
    # exec preserves stdout/stderr and signals. bpftrace owns its BPF links;
    # process exit releases them without touching global tracefs settings.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.execv(binary, [binary, "-q", "-B", "line", "-e", program])


if __name__ == "__main__":
    sys.exit(main())
