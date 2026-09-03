"""Helpers for the small, declarative tc subset exposed by nslab."""

from __future__ import annotations

import re

_RATE_UNITS = {
    "bit": 1,
    "kbit": 1_000,
    "mbit": 1_000_000,
    "gbit": 1_000_000_000,
}
_SIZE_UNITS = {
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
}
_RATE_PATTERN = re.compile(r"^(?P<value>[0-9]+)(?P<unit>bit|kbit|mbit|gbit)$", re.IGNORECASE)
_SIZE_PATTERN = re.compile(r"^(?P<value>[0-9]+)(?P<unit>b|k|kb|m|mb|g|gb)$", re.IGNORECASE)
_MAX_RATE_BYTES_PER_SECOND = 0xFFFFFFFF
_MAX_SIZE_BYTES = 0xFFFFFFFF


def _format_rate_bytes(bytes_per_second: int) -> str:
    bits_per_second = bytes_per_second * 8
    for unit, multiplier in (
        ("gbit", 1_000_000_000),
        ("mbit", 1_000_000),
        ("kbit", 1_000),
        ("bit", 1),
    ):
        if bits_per_second % multiplier == 0:
            return f"{bits_per_second // multiplier}{unit}"
    raise ValueError("rate cannot be represented as a whole number of bits per second")


def normalize_rate(value: object) -> str:
    """Validate and canonicalize a tc rate such as ``10mbit``."""

    if not isinstance(value, str):
        raise ValueError("rate must be a string such as '10mbit'")
    match = _RATE_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError("rate must match '<integer>(bit|kbit|mbit|gbit)'")
    amount = int(match.group("value"))
    unit = match.group("unit").lower()
    bits_per_second = amount * _RATE_UNITS[unit]
    if bits_per_second < 8 or bits_per_second % 8:
        raise ValueError("rate must be at least 8bit and represent a whole number of bytes")
    bytes_per_second = bits_per_second // 8
    if bytes_per_second > _MAX_RATE_BYTES_PER_SECOND:
        raise ValueError("rate is too large for the supported tc netlink attributes")
    return _format_rate_bytes(bytes_per_second)


def rate_to_bytes_per_second(value: str) -> int:
    """Return the byte-per-second value expected by pyroute2."""

    canonical = normalize_rate(value)
    match = _RATE_PATTERN.fullmatch(canonical)
    assert match is not None
    return int(match.group("value")) * _RATE_UNITS[match.group("unit")] // 8


def format_rate(bytes_per_second: int) -> str:
    """Convert a kernel byte-per-second rate to canonical tc text."""

    if type(bytes_per_second) is not int or not 0 < bytes_per_second <= _MAX_RATE_BYTES_PER_SECOND:
        raise ValueError("kernel rate is outside the supported range")
    return _format_rate_bytes(bytes_per_second)


def parse_size(value: object) -> int:
    """Parse a positive byte count, accepting tc-style binary suffixes."""

    if type(value) is int:
        amount = value
    elif isinstance(value, str):
        match = _SIZE_PATTERN.fullmatch(value.strip())
        if match is None:
            raise ValueError("size must be an integer or use b/kb/mb/gb")
        amount = int(match.group("value")) * _SIZE_UNITS[match.group("unit").lower()]
    else:
        raise ValueError("size must be an integer or use b/kb/mb/gb")
    if amount <= 0 or amount > _MAX_SIZE_BYTES:
        raise ValueError("size must be in the range 1..4294967295 bytes")
    return amount


def format_size(value: int) -> str:
    """Render a kernel byte count with a compact binary suffix where possible."""

    if type(value) is not int or value <= 0:
        raise ValueError("size must be a positive integer")
    for unit, multiplier in (("gb", 1024**3), ("mb", 1024**2), ("kb", 1024), ("b", 1)):
        if value % multiplier == 0:
            return f"{value // multiplier}{unit}"
    raise ValueError("size cannot be represented")
