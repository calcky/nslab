#!/usr/bin/env python3
"""Send enough data to exhaust the lab receiver's small TCP window."""

import socket
import time

with socket.create_connection(("10.70.0.2", 9000), timeout=45) as connection:
    started = time.monotonic()
    connection.sendall(b"x" * (8 * 1024 * 1024))
    connection.shutdown(socket.SHUT_WR)
    while connection.recv(1024):
        pass
    print(f"sent 8388608 bytes in {time.monotonic() - started:.2f}s")
