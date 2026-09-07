from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK = Path(__file__).resolve().parents[2] / "examples/dhcpv6/lease-script.sh"


@pytest.fixture
def hook_env(tmp_path: Path) -> dict[str, str]:
    # Capture network commands instead of touching the test runner's interfaces.
    ip = tmp_path / "ip"
    ip.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['IP_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    ip.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
        "IP_LOG": str(tmp_path / "ip.jsonl"),
        "interface": "eth0",
        "ipv6": "2001:db8:60::123",
        "lease": "300",
    }


@pytest.mark.parametrize("event", ["bound", "renew"])
def test_lease_event_applies_address_and_lifetime_only(
    hook_env: dict[str, str], event: str
) -> None:
    subprocess.run(["sh", str(_HOOK), event], env=hook_env, check=True)
    calls = Path(hook_env["IP_LOG"]).read_text().splitlines()
    assert len(calls) == 1
    assert json.loads(calls[0]) == [
        "-6",
        "addr",
        "replace",
        "2001:db8:60::123/128",
        "dev",
        "eth0",
        "valid_lft",
        "300",
        "preferred_lft",
        "300",
    ]


def test_deconfig_is_scoped_to_lab_prefix(hook_env: dict[str, str]) -> None:
    subprocess.run(["sh", str(_HOOK), "deconfig"], env=hook_env, check=True)
    assert json.loads(Path(hook_env["IP_LOG"]).read_text()) == [
        "-6",
        "addr",
        "flush",
        "dev",
        "eth0",
        "to",
        "2001:db8:60::/64",
    ]


def test_hook_rejects_other_interfaces(hook_env: dict[str, str]) -> None:
    hook_env["interface"] = "eth1"
    result = subprocess.run(["sh", str(_HOOK), "deconfig"], env=hook_env, check=False)
    assert result.returncode != 0
    assert not Path(hook_env["IP_LOG"]).exists()


def test_hook_rejects_missing_lease_address(hook_env: dict[str, str]) -> None:
    hook_env.pop("ipv6")
    result = subprocess.run(["sh", str(_HOOK), "bound"], env=hook_env, check=False)
    assert result.returncode != 0
    assert not Path(hook_env["IP_LOG"]).exists()
