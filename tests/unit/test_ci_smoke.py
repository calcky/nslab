from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_BGP_SUMMARY_CHECK = _ROOT / "scripts" / "ci" / "bgp-summary-ready.awk"


@pytest.mark.parametrize(
    ("summary", "expected_returncode"),
    [
        (
            """\
Neighbor V AS MsgRcvd MsgSent TblVer InQ OutQ Up/Down State/PfxRcd PfxSnt Desc
10.1.12.1 4 65001 6 6 0 0 0 00:00:10 1 4 N/A
10.1.23.2 4 65003 6 6 0 0 0 00:00:10 1 4 N/A
""",
            0,
        ),
        (
            """\
Neighbor V AS MsgRcvd MsgSent TblVer InQ OutQ Up/Down State/PfxRcd PfxSnt Desc
10.1.12.1 4 65001 6 6 0 0 0 00:00:10 1 4 N/A
10.1.23.2 4 65003 0 0 0 0 0 never Active 0 N/A
""",
            1,
        ),
    ],
    ids=["established-with-desc-column", "peer-active"],
)
def test_bgp_summary_readiness_check(summary: str, expected_returncode: int) -> None:
    result = subprocess.run(
        ("awk", "-f", str(_BGP_SUMMARY_CHECK)),
        input=summary,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == expected_returncode
    assert result.stdout == ""
    assert result.stderr == ""
