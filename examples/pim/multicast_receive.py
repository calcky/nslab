#!/usr/bin/env python3
from __future__ import annotations

import argparse
import socket
from ipaddress import IPv4Address


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Join an IPv4 multicast group and receive UDP")
    parser.add_argument("interface_address", type=IPv4Address)
    parser.add_argument("--group", type=IPv4Address, default=IPv4Address("239.1.1.1"))
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--count", type=positive_int, default=3)
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.group.is_multicast:
        raise SystemExit(f"group is not multicast: {args.group}")
    if not 1 <= args.port <= 65_535:
        raise SystemExit(f"port is outside 1..65535: {args.port}")
    if args.timeout <= 0:
        raise SystemExit("timeout must be greater than zero")

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", args.port))
        membership = socket.inet_aton(str(args.group)) + socket.inet_aton(
            str(args.interface_address)
        )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.settimeout(args.timeout)
        print(
            f"joined {args.group}:{args.port} on {args.interface_address}",
            flush=True,
        )
        for _ in range(args.count):
            payload, peer = sock.recvfrom(65_535)
            print(f"{payload.decode(errors='replace')} from {peer[0]}:{peer[1]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
