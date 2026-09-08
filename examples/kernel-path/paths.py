#!/usr/bin/env python3
"""Build bounded ICMP path probes and group their raw log by packet identity."""

import argparse
import ipaddress
import json
import re
import sys
from pathlib import Path

# Only stable, explicitly described entry arguments are used. Availability is
# checked before attachment; this is a set of observation points, not a call graph.
FUNCTIONS = {
    "ip_rcv": ("input", "arg0", "0"),
    "ip_forward": ("forward", "arg0", "0"),
    "ip_local_deliver": ("local-delivery", "arg0", "0"),
    "ip_local_out": ("local-output", "arg2", "arg0"),
    "__ip_local_out": ("local-output", "arg2", "arg0"),
    "ip_output": ("output", "arg2", "arg0"),
}
TRACEPOINTS = {"netif_receive_skb": "receive", "net_dev_queue": "transmit-queue"}
FLOWS = (("10.73.1.1", "10.73.2.2"), ("10.73.1.1", "10.73.1.254"), ("10.73.2.254", "10.73.2.2"))
EVENT = re.compile(
    r"^path ts=(\d+) cpu=(\d+) ns=(\d+) ifindex=(\d+) ifname=(\S+) skb=(0x[0-9a-f]+) "
    r"src=([\d.]+) dst=([\d.]+) type=(0|8) id=(\d+) seq=(\d+) stage=([\w-]+) hook=([\w:]+)$"
)


def discover(root):
    functions_file = root / "available_filter_functions"
    functions = {
        line.split()[0] for line in functions_file.read_text().splitlines() if line.strip()
    }
    probes, missing = [], []
    for name, stage in TRACEPOINTS.items():
        path = root / "events" / "net" / name / "format"
        hook = f"tracepoint:net:{name}"
        if path.exists() and re.search(r"field:[^;]*\bskbaddr;", path.read_text()):
            probes.append((hook, stage, "args->skbaddr", "0"))
        else:
            missing.append(hook)
    for name, (stage, skb, net) in FUNCTIONS.items():
        hook = f"kprobe:{name}"
        if name in functions:
            probes.append((hook, stage, skb, net))
        else:
            missing.append(hook)
    if not probes:
        raise ValueError("none of the supported IPv4 observation points are available")
    return probes, missing


def program(namespace, seconds, probes, stack=False):
    if not 1 <= namespace < 2**64 or not 1 <= seconds <= 300:
        raise ValueError("invalid namespace inode or duration")
    pairs = [
        pair
        for source, destination in FLOWS
        for pair in ((source, destination), (destination, source))
    ]
    predicate = " || ".join(
        f"($src == {int.from_bytes(ipaddress.IPv4Address(source).packed, sys.byteorder)} && "
        f"$dst == {int.from_bytes(ipaddress.IPv4Address(destination).packed, sys.byteorder)})"
        for source, destination in pairs
    )
    blocks = [f'BEGIN {{ printf("ready netns={namespace}\\n"); }}']
    for hook, stage, skb, net in probes:
        # net argument is essential for local OUTPUT, where skb->dev can be NULL.
        stack_format = r"\n%s" if stack else ""
        stack_arg = ", kstack(12)" if stack else ""
        fields = (
            "path ts=%llu cpu=%u ns=%u ifindex=%d ifname=%s skb=0x%lx "
            f"src=%s dst=%s type=%u id=%u seq=%u stage={stage} hook={hook}"
        )
        blocks.append(f"""
{hook}
{{
    $skb = (struct sk_buff *){skb};
    $dev = $skb->dev;
    $net = (struct net *){net};
    if ($net == 0 && $dev != 0) {{ $net = $dev->nd_net.net; }}
    if ($net != 0 && $net->ns.inum == {namespace} &&
        $skb->tail >= $skb->network_header + 28) {{
        $ip = (uint8 *)($skb->head + $skb->network_header);
        $src = *(uint32 *)($ip + 12);
        $dst = *(uint32 *)($ip + 16);
        if ($ip[0] == 0x45 && $ip[9] == 1 && ($ip[6] & 0x3f) == 0 && $ip[7] == 0 &&
            ($ip[20] == 0 || $ip[20] == 8) && $ip[21] == 0 && ({predicate})) {{
            $index = 0;
            $name = "-";
            if ($dev != 0) {{ $index = $dev->ifindex; $name = str($dev->name, 16); }}
            printf("{fields}{stack_format}\\n",
                   nsecs, cpu, $net->ns.inum, $index, $name, (uint64)$skb,
                   ntop($src), ntop($dst),
                   $ip[20], $ip[24] * 256 + $ip[25], $ip[26] * 256 + $ip[27]{stack_arg});
        }}
    }}
}}
""")
    blocks.append(f"interval:s:{seconds} {{ exit(); }}")
    return "\n".join(blocks)


def parse(text):
    events = []
    for line in text.splitlines():
        if line.startswith("path "):
            match = EVENT.fullmatch(line)
            if not match:
                raise ValueError(f"malformed path event: {line}")
            (
                ts,
                cpu,
                ns,
                index,
                name,
                skb,
                source,
                destination,
                kind,
                identifier,
                seq,
                stage,
                hook,
            ) = match.groups()
            events.append(
                {
                    "ts": int(ts),
                    "cpu": int(cpu),
                    "namespace": int(ns),
                    "ifindex": int(index),
                    "ifname": name,
                    "skb": skb,
                    "source": source,
                    "destination": destination,
                    "type": int(kind),
                    "id": int(identifier),
                    "seq": int(seq),
                    "stage": stage,
                    "hook": hook,
                    "stack": [],
                }
            )
        elif line[:1].isspace() and line.strip() and events:
            events[-1]["stack"].append(line.strip())
    return sorted(events, key=lambda event: event["ts"])


def group(events):
    packets = {}
    for event in sorted(events, key=lambda event: event["ts"]):
        identity = tuple(
            event[key] for key in ("namespace", "source", "destination", "type", "id", "seq")
        )
        packets.setdefault(identity, []).append(event)
    return list(packets.values())


def format_paths(events, stacks=False):
    lines = []
    for packet in group(events):
        first = packet[0]
        lines.append(
            f"{first['source']} -> {first['destination']} "
            f"ICMP {'request' if first['type'] == 8 else 'reply'} "
            f"id={first['id']} seq={first['seq']} netns={first['namespace']}"
        )
        for event in packet:
            elapsed = (event["ts"] - first["ts"]) / 1000
            lines.append(
                f"  +{elapsed:9.3f} us  {event['hook']:<36} "
                f"dev={event['ifname']}({event['ifindex']}) cpu={event['cpu']} skb={event['skb']}"
            )
            if stacks:
                lines.extend(f"      {frame}" for frame in event["stack"])
        lines.append("")
    return (
        "\n".join(lines)
        if lines
        else "No matching path events. This does not prove no packets passed.\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--stacks", action="store_true", help="show collected kernel stacks")
    parser.add_argument("--json", action="store_true", help="output structured packet groups")
    args = parser.parse_args()
    try:
        text = args.log.read_text()
        events = parse(text)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(group(events), indent=2) if args.json else format_paths(events, args.stacks))
    for line in text.splitlines():
        if line.startswith("probes="):
            missing = json.loads(line.removeprefix("probes=")).get("unavailable", [])
            if missing:
                print("Unavailable probes: " + ", ".join(missing), file=sys.stderr)
    if re.search(r"\b(lost|dropped)\s+[1-9]\d*\s+events\b", text, re.I):
        print("Warning: tracer reported lost events; paths are incomplete.", file=sys.stderr)


if __name__ == "__main__":
    main()
