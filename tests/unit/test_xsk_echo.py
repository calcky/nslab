import ctypes
import importlib
import shutil
import socket
import struct
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_LOCAL = socket.inet_aton("10.72.1.254")
_SOURCE = socket.inet_aton("10.72.1.1")


def checksum(data):
    if len(data) % 2:
        data += b"\x00"
    value = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while value >> 16:
        value = (value & 0xFFFF) + (value >> 16)
    return ~value & 0xFFFF


def request(payload=b"example"):
    icmp = struct.pack("!BBHHH", 8, 0, 0, 123, 456) + payload
    icmp = icmp[:2] + struct.pack("!H", checksum(icmp)) + icmp[4:]
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(icmp), 1, 0x4000, 17, 1, 0, _SOURCE, _LOCAL)
    ip = ip[:10] + struct.pack("!H", checksum(ip)) + ip[12:]
    return bytes.fromhex("0200000000020200000000010800") + ip + icmp


@pytest.fixture(scope="module")
def echo_library(tmp_path_factory):
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("C compiler required for nonprivileged packet tests")
    library = tmp_path_factory.mktemp("xsk-echo") / "echo.so"
    subprocess.run(
        [
            compiler,
            "-shared",
            "-fPIC",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            str(_ROOT / "examples/bpf-path"),
            str(_ROOT / "tests/fixtures/xsk_echo_wrapper.c"),
            "-o",
            str(library),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    loaded = ctypes.CDLL(str(library))
    loaded.make_reply.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32]
    loaded.make_reply.restype = ctypes.c_size_t
    return loaded


def reply(library, packet):
    buffer = ctypes.create_string_buffer(packet)
    size = library.make_reply(buffer, len(packet), int.from_bytes(_LOCAL, sys.byteorder))
    return size, buffer.raw[: len(packet)]


@pytest.mark.parametrize("length", [0, 1, 7, 56, 57, 1472])
def test_reply_preserves_payload_and_valid_checksums(echo_library, length):
    payload = bytes(index % 256 for index in range(length))
    packet = request(payload)
    size, result = reply(echo_library, packet)
    assert size == len(packet)
    assert result[:6] == packet[6:12]
    assert result[6:12] == packet[:6]
    assert result[26:30] == _LOCAL
    assert result[30:34] == _SOURCE
    assert result[22] == 64
    assert result[34] == 0
    assert result[38:] == packet[38:]
    assert checksum(result[14:34]) == 0
    assert checksum(result[34:]) == 0


@pytest.mark.parametrize("length", [0, 1, 13, 14, 33, 41, 45])
def test_truncated_frames_are_not_modified(echo_library, length):
    packet = request()[:length]
    assert reply(echo_library, packet) == (0, packet)


@pytest.mark.parametrize(
    "offset,value",
    [
        (12, 0x86),
        (14, 0x46),
        (20, 0x20),
        (21, 1),
        (22, 0),
        (23, 17),
        (24, 0),
        (30, 192),
        (34, 0),
        (35, 1),
        (36, 0),
        (42, 255),
    ],
)
def test_unsupported_or_corrupt_frames_are_not_modified(echo_library, offset, value):
    packet = bytearray(request())
    assert packet[offset] != value
    packet[offset] = value
    packet = bytes(packet)
    assert reply(echo_library, packet) == (0, packet)


def test_ethernet_padding_is_not_transmitted_as_ip_payload(echo_library):
    packet = request(b"a")
    size, result = reply(echo_library, packet + b"\x00" * 20)
    assert size == len(packet)
    assert result[size:] == b"\x00" * 20
    assert checksum(result[34:size]) == 0


def test_cpu_and_xsk_counter_parsers(monkeypatch):
    monkeypatch.syspath_prepend(str(_ROOT / "examples/bpf-path"))
    checks = importlib.import_module("check_cpu_xsk")
    assert checks.cpu_samples(
        "stage=remote cpu=1 packets=2\nstage=remote cpu=1 packets=3", "remote"
    ) == {1: 3}
    assert checks.cpu_samples("stage=ingress cpu=0 packets=3", "remote") == {}
    assert checks.xsk_sample("rx=307 tx=307 completed=307 rejected=0") == (307, 307, 307, 0)
    with pytest.raises(RuntimeError, match="missing AF_XDP counters"):
        checks.xsk_sample("attach failed")
