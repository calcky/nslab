#!/usr/bin/env python3
"""Advertise a small receive window, pause reads, then drain the connection."""

import argparse
import socket
import time

parser = argparse.ArgumentParser()
parser.add_argument("--pause", type=float, default=10)
args = parser.parse_args()
if args.pause < 0 or args.pause > 60:
    parser.error("--pause must be between 0 and 60 seconds")

with socket.socket() as server:
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    server.bind(("10.70.0.2", 9000))
    server.listen(1)
    server.settimeout(60)
    print("listening on 10.70.0.2:9000", flush=True)
    connection, _ = server.accept()
    with connection:
        connection.settimeout(30)
        print(f"connected; pausing reads for {args.pause:g}s", flush=True)
        time.sleep(args.pause)
        total = 0
        while data := connection.recv(65536):
            total += len(data)
        print(f"received {total} bytes", flush=True)
