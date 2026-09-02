from __future__ import annotations

from pathlib import Path

import pytest

import nslab.version as package_version

_FULL_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def test_commit_hash_prefers_embedded_build_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_file = tmp_path / "_commit"
    commit_file.write_text(f"{_FULL_COMMIT}\n", encoding="ascii")
    monkeypatch.setattr(package_version, "_COMMIT_FILE", commit_file)
    monkeypatch.setattr(
        package_version,
        "_source_commit",
        lambda: (_ for _ in ()).throw(AssertionError("queried source commit")),
    )

    assert package_version.commit_hash() == "0123456789ab"


def test_commit_hash_falls_back_to_source_then_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(package_version, "_embedded_commit", lambda: None)
    monkeypatch.setattr(package_version, "_source_commit", lambda: "abcdef012345")
    assert package_version.commit_hash() == "abcdef012345"

    monkeypatch.setattr(package_version, "_source_commit", lambda: None)
    assert package_version.commit_hash() == "unknown"


def test_version_text_includes_version_and_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(package_version, "commit_hash", lambda: "abcdef012345")

    assert package_version.version_text() == (
        f"nslab {package_version.__version__} (commit abcdef012345)"
    )
