from __future__ import annotations

import re
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_COMMIT_LENGTH = 12
_COMMIT_PATTERN = re.compile(r"[0-9a-fA-F]{7,64}\Z")
_COMMIT_FILE = Path(__file__).with_name("_commit")
_SOURCE_ROOT = Path(__file__).resolve().parents[2]

try:
    __version__ = version("nslab")
except PackageNotFoundError:
    __version__ = "0+unknown"


def _normalize_commit(value: str) -> str | None:
    candidate = value.strip()
    if _COMMIT_PATTERN.fullmatch(candidate) is None:
        return None
    return candidate.lower()[:_COMMIT_LENGTH]


def _embedded_commit() -> str | None:
    try:
        return _normalize_commit(_COMMIT_FILE.read_text(encoding="ascii"))
    except OSError:
        return None


def _source_commit() -> str | None:
    if not (_SOURCE_ROOT / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ("git", "-C", str(_SOURCE_ROOT), "rev-parse", "--verify", "HEAD"),
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _normalize_commit(result.stdout)


def commit_hash() -> str:
    return _embedded_commit() or _source_commit() or "unknown"


def version_text() -> str:
    return f"nslab {__version__} (commit {commit_hash()})"
