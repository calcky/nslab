"""Additional opt-in root checks, called by check_redirect.py --advanced."""

import json
import os
import re
import signal
import subprocess
import time

from check_redirect import run, stop


def cpu_samples(output, stage):
    samples = re.findall(rf"stage={stage} cpu=(\d+) packets=(\d+)", output)
    return {int(cpu): int(packets) for cpu, packets in samples}


def xsk_sample(output):
    samples = re.findall(r"rx=(\d+) tx=(\d+) completed=(\d+) rejected=(\d+)", output)
    if not samples:
        raise RuntimeError(f"missing AF_XDP counters: {output}")
    return tuple(map(int, samples[-1]))


def check_extra(root, work, namespaces, processes):
    def ns(node, *args):
        return ["ip", "netns", "exec", namespaces[node], *args]

    def launch(command, filename, marker):
        path = work / filename
        with path.open("w") as log:
            process = subprocess.Popen(command, stdout=log, stderr=log)
        processes.append(process)
        deadline = time.monotonic() + 15
        while marker not in path.read_text():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError(path.read_text())
            time.sleep(0.1)
        return process, path

    def detached():
        link = json.loads(run(ns("r1", "ip", "-j", "-d", "link", "show", "dev", "eth0")).stdout)[0]
        if link.get("xdp", {}).get("attached") or link.get("xdp", {}).get("prog"):
            raise RuntimeError("XDP attachment remains")

    cpus = sorted(os.sched_getaffinity(0))
    target = cpus[1] if len(cpus) > 1 else cpus[0]
    command = ns("r1", str(root / "cpu_lab"), "eth0", str(target), str(root / "cpu_lab.bpf.o"))
    process, path = launch(command, "cpumap.log", "attached cpumap native")
    if run(command, check=False).returncode != 1:
        raise RuntimeError("duplicate CPUMAP attachment was not refused")
    with (work / "cpumap-ttl.log").open("w") as log:
        capture = subprocess.Popen(
            ns("h2", "tcpdump", "-nn", "-v", "-i", "eth0", "-c", "3", "icmp and icmp[0] = 8"),
            stdout=log,
            stderr=log,
        )
    processes.append(capture)
    time.sleep(0.5)
    run(["taskset", "-c", str(cpus[0]), *ns("h1", "ping", "-c", "3", "-W", "2", "10.72.2.2")])
    if capture.wait(timeout=10):
        raise RuntimeError("CPUMAP receiver capture failed")
    time.sleep(0.2)
    stop(process)
    if process.returncode or cpu_samples(path.read_text(), "remote") != {target: 3}:
        raise RuntimeError(path.read_text())
    ingress = cpu_samples(path.read_text(), "ingress")
    if sum(ingress.values()) != 3 or (len(cpus) > 1 and not set(ingress) - {target}):
        raise RuntimeError(f"CPU handoff not observed: {path.read_text()}")
    if (work / "cpumap-ttl.log").read_text().count("ttl 63,") != 3:
        raise RuntimeError("CPUMAP changed routing TTL")
    detached()
    print(f"cpumap: PASS (native XDP, target CPU {target}, stack resume, TTL, detach)", flush=True)

    command = ns("r1", str(root / "xsk_lab"), "eth0", "10.72.1.254", str(root / "xsk_lab.bpf.o"))
    process, path = launch(command, "af-xdp.log", "mode=copy zero_copy=no")
    if run(command, check=False).returncode != 1:
        raise RuntimeError("duplicate AF_XDP attachment was not refused")
    # A stopped userspace receiver must not be silently replaced by kernel ICMP.
    process.send_signal(signal.SIGSTOP)
    try:
        if run(ns("h1", "ping", "-c", "1", "-W", "1", "10.72.1.254"), check=False).returncode != 1:
            raise RuntimeError("ping unexpectedly succeeded with AF_XDP userspace paused")
    finally:
        process.send_signal(signal.SIGCONT)
    run(ns("h1", "ping", "-c", "300", "-i", "0.01", "-W", "2", "10.72.1.254"))
    run(ns("h1", "ping", "-s", "57", "-c", "3", "-W", "2", "10.72.1.254"))
    run(ns("h1", "ping", "-s", "1472", "-c", "3", "-W", "2", "10.72.1.254"))
    run(ns("h1", "ping", "-c", "3", "-W", "2", "10.72.2.2"))
    time.sleep(0.3)
    stop(process)
    rx, tx, completed, rejected = xsk_sample(path.read_text())
    if process.returncode or rx < 306 or tx != rx or completed != tx or rejected:
        raise RuntimeError(path.read_text())
    detached()
    print(
        "af_xdp: PASS (copy mode, userspace RX/TX, UMEM recycling, checksums, detach)", flush=True
    )
