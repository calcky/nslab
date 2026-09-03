#!/usr/bin/env python3
from __future__ import annotations

import argparse
import socket
import time
from ipaddress import IPv4Address


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send UDP to an IPv4 multicast group")
    parser.add_argument("interface_address", type=IPv4Address)
    parser.add_argument("--group", type=IPv4Address, default=IPv4Address("239.1.1.1"))
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--count", type=positive_int, default=10)
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--ttl", type=int, default=16)
    parser.add_argument("--delay", type=float, default=5.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.group.is_multicast:
        raise SystemExit(f"group is not multicast: {args.group}")
    if not 1 <= args.port <= 65_535:
        raise SystemExit(f"port is outside 1..65535: {args.port}")
    if not 1 <= args.ttl <= 255:
        raise SystemExit(f"TTL is outside 1..255: {args.ttl}")
    if args.interval < 0 or args.delay < 0:
        raise SystemExit("interval and delay must not be negative")

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_MULTICAST_IF,
            socket.inet_aton(str(args.interface_address)),
        )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, args.ttl)
        time.sleep(args.delay)
        for sequence in range(1, args.count + 1):
            payload = f"pim-{sequence}".encode()
            sock.sendto(payload, (str(args.group), args.port))
            time.sleep(args.interval)
    print(f"sent {args.count} packets to {args.group}:{args.port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
