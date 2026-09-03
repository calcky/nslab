from __future__ import annotations

import pytest

from nslab.tc import format_rate, normalize_rate, parse_size


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10mbit", "10mbit"),
        ("1000kbit", "1mbit"),
        ("1GBIT", "1gbit"),
    ],
)
def test_normalize_rate_returns_stable_tc_text(value: str, expected: str) -> None:
    assert normalize_rate(value) == expected


@pytest.mark.parametrize("value", ["0bit", "7bit", "10mbps", "1.5mbit", 10])
def test_normalize_rate_rejects_unsupported_values(value: object) -> None:
    with pytest.raises(ValueError):
        normalize_rate(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(32768, 32768), ("32kb", 32768), ("1mb", 1024 * 1024)],
)
def test_parse_size_accepts_byte_suffixes(value: object, expected: int) -> None:
    assert parse_size(value) == expected


def test_format_rate_converts_kernel_bytes_per_second() -> None:
    assert format_rate(1_250_000) == "10mbit"
