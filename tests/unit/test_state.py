from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from nslab.errors import NslabError, OperationCancelled
from nslab.state import DeploymentLock, StateSnapshot, StateStore


@pytest.fixture
def snapshot() -> StateSnapshot:
    return StateSnapshot(
        schema=1,
        name="bridge-fdb",
        fingerprint="a" * 64,
        manifest={
            "version": 1,
            "name": "bridge-fdb",
            "topology": {
                "nodes": {
                    "h1": {
                        "kind": "linux",
                        "interfaces": {
                            "eth0": {"addresses": ["10.10.0.1/24"]},
                        },
                    },
                    "sw1": {
                        "kind": "bridge",
                        "bridge": {
                            "name": "br0",
                            "stp": False,
                            "vlan_filtering": False,
                        },
                    },
                },
                "links": [{"kind": "veth", "endpoints": ["h1:eth0", "sw1:swp1"]}],
            },
        },
        namespaces={
            "h1": "nslab-bridge-fdb-h1-0123456789abcdef",
            "sw1": "nslab-bridge-fdb-sw1-fedcba9876543210",
        },
        interfaces={
            "h1:eth0": {"name": "eth0", "ifindex": 7},
            "sw1:swp1": {"name": "swp1", "ifindex": 8},
            "sw1:br0": {"name": "br0", "ifindex": 3},
        },
        created_at="2026-08-31T10:15:30+00:00",
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _temporary_paths(root: Path, name: str) -> list[Path]:
    return list(root.glob(f"{name}.json.tmp-*"))


def test_snapshot_round_trip_is_frozen_deterministic_json_with_exact_modes(
    tmp_path: Path, snapshot: StateSnapshot
) -> None:
    root = tmp_path / "nested" / "state"
    store = StateStore(root)

    old_umask = os.umask(0o077)
    try:
        store.save(snapshot)
    finally:
        os.umask(old_umask)

    path = root / "bridge-fdb.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    expected_text = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"

    assert store.load("bridge-fdb") == snapshot
    assert snapshot.status == "deployed"
    assert document["status"] == "deployed"
    assert set(document) == {
        "schema",
        "name",
        "fingerprint",
        "manifest",
        "namespaces",
        "interfaces",
        "created_at",
        "status",
    }
    assert path.read_text(encoding="utf-8") == expected_text
    assert document["manifest"]["topology"]["nodes"]["h1"]["kind"] == "linux"
    assert _mode(root) == 0o755
    assert _mode(path) == 0o644
    with pytest.raises(FrozenInstanceError):
        snapshot.name = "changed"  # type: ignore[misc]


def test_replace_oserror_is_unknown_even_when_previous_snapshot_remains(
    tmp_path: Path,
    snapshot: StateSnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path)
    store.save(snapshot)
    path = tmp_path / "bridge-fdb.json"
    previous = path.read_bytes()
    replacement = StateSnapshot(
        schema=snapshot.schema,
        name=snapshot.name,
        fingerprint="b" * 64,
        manifest=snapshot.manifest,
        namespaces=snapshot.namespaces,
        interfaces=snapshot.interfaces,
        created_at="2026-08-31T10:16:00+00:00",
    )

    failure = OSError("injected replace failure")

    def fail_replace(source: Path, destination: Path) -> None:
        raise failure

    monkeypatch.setattr("nslab.state.os.replace", fail_replace)

    with pytest.raises(NslabError) as caught:
        store.save(replacement)

    assert caught.value.code == "STATE_COMMIT_OUTCOME_UNKNOWN"
    assert caught.value.details == {
        "path": str(path),
        "operation": "save",
        "committed": "unknown",
        "cause_type": "OSError",
        "cause_message": "injected replace failure",
    }
    assert caught.value.__cause__ is failure
    assert path.read_bytes() == previous
    assert _temporary_paths(tmp_path, snapshot.name) == []


def test_save_file_fsync_failure_keeps_previous_snapshot_and_removes_temp(
    tmp_path: Path,
    snapshot: StateSnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path)
    store.save(snapshot)
    path = tmp_path / "bridge-fdb.json"
    previous = path.read_bytes()

    def fail_fsync(file_descriptor: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr("nslab.state.os.fsync", fail_fsync)

    with pytest.raises(NslabError) as caught:
        store.save(snapshot)

    assert caught.value.code == "STATE_WRITE_FAILED"
    assert path.read_bytes() == previous
    assert _temporary_paths(tmp_path, snapshot.name) == []


def test_directory_fsync_failure_leaves_complete_replacement_and_no_temp(
    tmp_path: Path,
    snapshot: StateSnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path)
    store.save(snapshot)
    replacement = StateSnapshot(
        schema=snapshot.schema,
        name=snapshot.name,
        fingerprint="b" * 64,
        manifest=snapshot.manifest,
        namespaces=snapshot.namespaces,
        interfaces=snapshot.interfaces,
        created_at="2026-08-31T10:16:00+00:00",
    )
    real_fsync = os.fsync
    fsync_calls = 0

    def fail_directory_fsync(file_descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            real_fsync(file_descriptor)
            return
        raise OSError("injected directory fsync failure")

    monkeypatch.setattr("nslab.state.os.fsync", fail_directory_fsync)

    with pytest.raises(NslabError) as caught:
        store.save(replacement)

    assert caught.value.code == "STATE_DURABILITY_UNCERTAIN"
    assert caught.value.details["path"] == str(tmp_path / "bridge-fdb.json")
    assert caught.value.details["operation"] == "save"
    assert caught.value.details["committed"] is True
    assert caught.value.details["cause_type"] == "OSError"
    assert caught.value.details["cause_message"] == "injected directory fsync failure"
    assert store.load(snapshot.name) == replacement
    assert _temporary_paths(tmp_path, snapshot.name) == []


def test_replace_commit_then_cancellation_reports_unknown_outcome(
    tmp_path: Path,
    snapshot: StateSnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path)
    store.save(snapshot)
    replacement = replace(
        snapshot,
        fingerprint="b" * 64,
        created_at="2026-08-31T10:16:00+00:00",
    )
    real_replace = os.replace
    cancellation = OperationCancelled("cancelled after replace committed")

    def replace_then_cancel(source: Path, destination: Path) -> None:
        real_replace(source, destination)
        raise cancellation

    monkeypatch.setattr("nslab.state.os.replace", replace_then_cancel)

    with pytest.raises(NslabError) as caught:
        store.save(replacement)

    assert caught.value.code == "STATE_COMMIT_OUTCOME_UNKNOWN"
    assert caught.value.details == {
        "path": str(tmp_path / "bridge-fdb.json"),
        "operation": "save",
        "committed": "unknown",
        "cause_type": "OperationCancelled",
        "cause_message": "cancelled after replace committed",
    }
    assert caught.value.__cause__ is cancellation
    assert store.load(snapshot.name) == replacement
    assert _temporary_paths(tmp_path, snapshot.name) == []


def test_replace_commit_then_oserror_reports_unknown_outcome(
    tmp_path: Path,
    snapshot: StateSnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path)
    store.save(snapshot)
    replacement = replace(
        snapshot,
        fingerprint="b" * 64,
        created_at="2026-08-31T10:16:00+00:00",
    )
    real_replace = os.replace
    failure = OSError("replace reported failure after commit")

    def replace_then_fail(source: Path, destination: Path) -> None:
        real_replace(source, destination)
        raise failure

    monkeypatch.setattr("nslab.state.os.replace", replace_then_fail)

    with pytest.raises(NslabError) as caught:
        store.save(replacement)

    assert caught.value.code == "STATE_COMMIT_OUTCOME_UNKNOWN"
    assert caught.value.details == {
        "path": str(tmp_path / "bridge-fdb.json"),
        "operation": "save",
        "committed": "unknown",
        "cause_type": "OSError",
        "cause_message": "replace reported failure after commit",
    }
    assert caught.value.__cause__ is failure
    assert store.load(snapshot.name) == replacement
    assert _temporary_paths(tmp_path, snapshot.name) == []


def test_cancellation_after_replace_is_durability_uncertain(
    tmp_path: Path,
    snapshot: StateSnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path)
    store.save(snapshot)
    replacement = replace(
        snapshot,
        fingerprint="b" * 64,
        created_at="2026-08-31T10:16:00+00:00",
    )
    cancellation = OperationCancelled("cancelled during directory fsync")

    def cancel_directory_fsync() -> None:
        raise cancellation

    monkeypatch.setattr(store, "_fsync_directory", cancel_directory_fsync)

    with pytest.raises(NslabError) as caught:
        store.save(replacement)

    assert caught.value.code == "STATE_DURABILITY_UNCERTAIN"
    assert caught.value.details == {
        "path": str(tmp_path / "bridge-fdb.json"),
        "error": "cancelled during directory fsync",
        "operation": "save",
        "committed": True,
        "cause_type": "OperationCancelled",
        "cause_message": "cancelled during directory fsync",
    }
    assert caught.value.__cause__ is cancellation
    assert store.load(snapshot.name) == replacement
    assert _temporary_paths(tmp_path, snapshot.name) == []


def test_delete_directory_fsync_failure_reports_committed_but_uncertain(
    tmp_path: Path,
    snapshot: StateSnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path)
    store.save(snapshot)

    def fail_directory_fsync() -> None:
        raise OSError("injected directory fsync failure")

    monkeypatch.setattr(store, "_fsync_directory", fail_directory_fsync)

    with pytest.raises(NslabError) as caught:
        store.delete(snapshot.name)

    assert caught.value.code == "STATE_DURABILITY_UNCERTAIN"
    assert caught.value.details == {
        "path": str(tmp_path / "bridge-fdb.json"),
        "error": "injected directory fsync failure",
        "operation": "delete",
        "committed": True,
        "cause_type": "OSError",
        "cause_message": "injected directory fsync failure",
    }
    assert not (tmp_path / "bridge-fdb.json").exists()


def test_unlink_commit_then_cancellation_reports_unknown_outcome(
    tmp_path: Path,
    snapshot: StateSnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path)
    store.save(snapshot)
    real_unlink = Path.unlink
    cancellation = OperationCancelled("cancelled after unlink committed")

    def unlink_then_cancel(path: Path, missing_ok: bool = False) -> None:
        real_unlink(path, missing_ok=missing_ok)
        raise cancellation

    monkeypatch.setattr(Path, "unlink", unlink_then_cancel)

    with pytest.raises(NslabError) as caught:
        store.delete(snapshot.name)

    assert caught.value.code == "STATE_COMMIT_OUTCOME_UNKNOWN"
    assert caught.value.details == {
        "path": str(tmp_path / "bridge-fdb.json"),
        "operation": "delete",
        "committed": "unknown",
        "cause_type": "OperationCancelled",
        "cause_message": "cancelled after unlink committed",
    }
    assert caught.value.__cause__ is cancellation
    assert not (tmp_path / "bridge-fdb.json").exists()


def test_unlink_commit_then_oserror_reports_unknown_outcome(
    tmp_path: Path,
    snapshot: StateSnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path)
    store.save(snapshot)
    real_unlink = Path.unlink
    failure = OSError("unlink reported failure after commit")

    def unlink_then_fail(path: Path, missing_ok: bool = False) -> None:
        real_unlink(path, missing_ok=missing_ok)
        raise failure

    monkeypatch.setattr(Path, "unlink", unlink_then_fail)

    with pytest.raises(NslabError) as caught:
        store.delete(snapshot.name)

    assert caught.value.code == "STATE_COMMIT_OUTCOME_UNKNOWN"
    assert caught.value.details == {
        "path": str(tmp_path / "bridge-fdb.json"),
        "operation": "delete",
        "committed": "unknown",
        "cause_type": "OSError",
        "cause_message": "unlink reported failure after commit",
    }
    assert caught.value.__cause__ is failure
    assert not (tmp_path / "bridge-fdb.json").exists()


def test_delete_cancellation_after_unlink_is_durability_uncertain(
    tmp_path: Path,
    snapshot: StateSnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path)
    store.save(snapshot)
    cancellation = OperationCancelled("cancelled during delete directory fsync")

    def cancel_directory_fsync() -> None:
        raise cancellation

    monkeypatch.setattr(store, "_fsync_directory", cancel_directory_fsync)

    with pytest.raises(NslabError) as caught:
        store.delete(snapshot.name)

    assert caught.value.code == "STATE_DURABILITY_UNCERTAIN"
    assert caught.value.details == {
        "path": str(tmp_path / "bridge-fdb.json"),
        "error": "cancelled during delete directory fsync",
        "operation": "delete",
        "committed": True,
        "cause_type": "OperationCancelled",
        "cause_message": "cancelled during delete directory fsync",
    }
    assert caught.value.__cause__ is cancellation
    assert not (tmp_path / "bridge-fdb.json").exists()


def test_unlink_oserror_is_unknown_even_when_state_file_remains(
    tmp_path: Path,
    snapshot: StateSnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path)
    store.save(snapshot)

    failure = OSError("injected unlink failure")

    def fail_unlink(path: Path, missing_ok: bool = False) -> None:
        raise failure

    monkeypatch.setattr(Path, "unlink", fail_unlink)

    with pytest.raises(NslabError) as caught:
        store.delete(snapshot.name)

    assert caught.value.code == "STATE_COMMIT_OUTCOME_UNKNOWN"
    assert caught.value.details == {
        "path": str(tmp_path / "bridge-fdb.json"),
        "operation": "delete",
        "committed": "unknown",
        "cause_type": "OSError",
        "cause_message": "injected unlink failure",
    }
    assert caught.value.__cause__ is failure
    assert (tmp_path / "bridge-fdb.json").exists()


def test_missing_load_and_repeated_delete_are_idempotent(
    tmp_path: Path, snapshot: StateSnapshot
) -> None:
    store = StateStore(tmp_path)

    assert store.load(snapshot.name) is None
    store.delete(snapshot.name)
    store.save(snapshot)
    store.delete(snapshot.name)
    store.delete(snapshot.name)

    assert store.load(snapshot.name) is None


@pytest.mark.parametrize("status", ["deploying", "deployed", "destroying"])
def test_snapshot_status_round_trips(
    tmp_path: Path, snapshot: StateSnapshot, status: str
) -> None:
    candidate = replace(snapshot, status=status)  # type: ignore[arg-type]

    StateStore(tmp_path).save(candidate)

    loaded = StateStore(tmp_path).load(snapshot.name)
    assert loaded is not None
    assert loaded.status == status
    assert json.loads((tmp_path / "bridge-fdb.json").read_text())["status"] == status


def test_invalid_snapshot_status_is_a_domain_error(tmp_path: Path, snapshot: StateSnapshot) -> None:
    document = snapshot.to_dict()
    document["status"] = "failed"
    path = tmp_path / "bridge-fdb.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(NslabError) as caught:
        StateStore(tmp_path).load(snapshot.name)

    assert caught.value.code == "STATE_INVALID"
    assert caught.value.details["path"] == str(path)
    assert "status" in str(caught.value.details["error"])


def test_schema_one_snapshot_without_status_migrates_to_deployed(
    tmp_path: Path, snapshot: StateSnapshot
) -> None:
    document = snapshot.to_dict()
    del document["status"]
    path = tmp_path / "bridge-fdb.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    loaded = StateStore(tmp_path).load(snapshot.name)

    assert loaded is not None
    assert loaded.status == "deployed"
    StateStore(tmp_path).save(loaded)
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "deployed"


@pytest.mark.parametrize(
    "content",
    [
        "{not valid json}\n",
        json.dumps({"schema": 1, "name": "bridge-fdb"}),
        json.dumps(
            {
                "schema": 2,
                "name": "bridge-fdb",
                "fingerprint": "a" * 64,
                "manifest": {},
                "namespaces": {},
                "interfaces": {},
                "created_at": "2026-08-31T10:15:30+00:00",
            }
        ),
    ],
    ids=["invalid-json", "missing-fields", "unsupported-schema"],
)
def test_malformed_snapshot_is_a_domain_error_with_path(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "bridge-fdb.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(NslabError) as caught:
        StateStore(tmp_path).load("bridge-fdb")

    assert caught.value.code == "STATE_INVALID"
    assert caught.value.details["path"] == str(path)
    assert caught.value.details["error"]


@pytest.mark.parametrize("field", ["manifest", "interfaces"])
def test_snapshot_object_fields_reject_non_string_keys(
    snapshot: StateSnapshot, field: str
) -> None:
    document = snapshot.to_dict()
    document[field] = {1: "must not be dropped"}

    with pytest.raises(ValueError, match=rf"{field} keys must be strings"):
        StateSnapshot.from_dict(document)


@pytest.mark.parametrize("operation", ["load", "delete", "lock"])
def test_deployment_name_is_validated_before_deriving_a_path(
    tmp_path: Path, operation: str
) -> None:
    invalid_name = "../outside"
    outside = tmp_path.parent / "outside.json"

    with pytest.raises(NslabError) as caught:
        if operation == "lock":
            with DeploymentLock(tmp_path, invalid_name):
                pass
        else:
            getattr(StateStore(tmp_path), operation)(invalid_name)

    assert caught.value.code == "DEPLOYMENT_NAME_INVALID"
    assert caught.value.details == {"name": invalid_name}
    assert not outside.exists()


def test_save_validates_snapshot_name_before_deriving_a_path(
    tmp_path: Path, snapshot: StateSnapshot
) -> None:
    invalid = StateSnapshot(
        schema=snapshot.schema,
        name="../outside",
        fingerprint=snapshot.fingerprint,
        manifest=snapshot.manifest,
        namespaces=snapshot.namespaces,
        interfaces=snapshot.interfaces,
        created_at=snapshot.created_at,
    )

    with pytest.raises(NslabError) as caught:
        StateStore(tmp_path).save(invalid)

    assert caught.value.code == "DEPLOYMENT_NAME_INVALID"
    assert not (tmp_path.parent / "outside.json").exists()


@pytest.mark.parametrize(
    "failure",
    [
        OperationCancelled("injected cancellation"),
        KeyboardInterrupt("injected interrupt"),
        RuntimeError("injected unexpected failure"),
    ],
    ids=["operation-cancelled", "keyboard-interrupt", "unexpected-exception"],
)
def test_save_cleans_temp_and_closes_fd_without_masking_pre_fdopen_failure(
    tmp_path: Path,
    snapshot: StateSnapshot,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    real_mkstemp = tempfile.mkstemp
    captured: dict[str, object] = {}

    def recording_mkstemp(*, prefix: str, dir: Path) -> tuple[int, str]:
        file_descriptor, temporary_name = real_mkstemp(prefix=prefix, dir=dir)
        captured["file_descriptor"] = file_descriptor
        captured["temporary_name"] = temporary_name
        return file_descriptor, temporary_name

    def fail_fchmod(file_descriptor: int, mode: int) -> None:
        raise failure

    monkeypatch.setattr("nslab.state.tempfile.mkstemp", recording_mkstemp)
    monkeypatch.setattr("nslab.state.os.fchmod", fail_fchmod)

    with pytest.raises(type(failure)) as caught:
        StateStore(tmp_path).save(snapshot)

    assert caught.value is failure
    file_descriptor = captured["file_descriptor"]
    temporary_name = captured["temporary_name"]
    assert isinstance(file_descriptor, int)
    assert isinstance(temporary_name, str)
    with pytest.raises(OSError) as closed:
        os.fstat(file_descriptor)
    assert closed.value.errno == errno.EBADF
    assert not Path(temporary_name).exists()


def test_unpaired_surrogate_is_a_write_error_and_leaves_no_temp(
    tmp_path: Path, snapshot: StateSnapshot
) -> None:
    invalid_text = replace(snapshot, manifest={"invalid": "\ud800"})

    with pytest.raises(NslabError) as caught:
        StateStore(tmp_path).save(invalid_text)

    assert caught.value.code == "STATE_WRITE_FAILED"
    assert caught.value.details["path"] == str(tmp_path / "bridge-fdb.json")
    assert _temporary_paths(tmp_path, snapshot.name) == []


def test_stale_fixed_pid_temp_does_not_block_save_or_get_deleted(
    tmp_path: Path, snapshot: StateSnapshot
) -> None:
    stale = tmp_path / f"bridge-fdb.json.tmp-{os.getpid()}"
    stale.write_text("stale but not proven inactive", encoding="utf-8")

    StateStore(tmp_path).save(snapshot)

    assert StateStore(tmp_path).load(snapshot.name) == snapshot
    assert stale.read_text(encoding="utf-8") == "stale but not proven inactive"
    assert set(_temporary_paths(tmp_path, snapshot.name)) == {stale}


def test_lock_contention_times_out_then_succeeds_after_release(tmp_path: Path) -> None:
    first = DeploymentLock(tmp_path, "bridge-fdb")

    with first:
        with (
            pytest.raises(NslabError) as caught,
            DeploymentLock(tmp_path, "bridge-fdb", timeout=0.05),
        ):
            pass

        assert caught.value.code == "DEPLOYMENT_LOCKED"
        assert caught.value.details == {
            "name": "bridge-fdb",
            "path": str(tmp_path / "bridge-fdb.lock"),
            "timeout": 0.05,
        }

    with DeploymentLock(tmp_path, "bridge-fdb", timeout=0.05):
        pass

    assert (tmp_path / "bridge-fdb.lock").exists()


def test_lock_file_is_exactly_private_under_restrictive_umask(tmp_path: Path) -> None:
    root = tmp_path / "locks"
    old_umask = os.umask(0o777)
    try:
        with DeploymentLock(root, "bridge-fdb"):
            pass
    finally:
        os.umask(old_umask)

    assert _mode(root) == 0o755
    assert _mode(root / "bridge-fdb.lock") == 0o600


def test_zero_timeout_attempts_once_without_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    def always_busy(file_descriptor: int, operation: int) -> None:
        nonlocal attempts
        attempts += 1
        raise BlockingIOError

    def unexpected_sleep(seconds: float) -> None:
        pytest.fail(f"zero-timeout lock slept for {seconds}")

    monkeypatch.setattr("nslab.state.fcntl.flock", always_busy)
    monkeypatch.setattr("nslab.state.time.sleep", unexpected_sleep)

    with (
        pytest.raises(NslabError) as caught,
        DeploymentLock(tmp_path, "bridge-fdb", timeout=0),
    ):
        pass

    assert caught.value.code == "DEPLOYMENT_LOCKED"
    assert attempts == 1


def test_lock_is_released_when_context_body_raises(tmp_path: Path) -> None:
    with (
        pytest.raises(RuntimeError, match="body failed"),
        DeploymentLock(tmp_path, "bridge-fdb"),
    ):
        raise RuntimeError("body failed")

    with DeploymentLock(tmp_path, "bridge-fdb", timeout=0):
        pass


def test_directory_fsync_error_is_not_masked_by_close_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_close = os.close

    def fail_fsync(file_descriptor: int) -> None:
        raise OSError("primary fsync failure")

    def close_then_fail(file_descriptor: int) -> None:
        real_close(file_descriptor)
        raise OSError("secondary close failure")

    monkeypatch.setattr("nslab.state.os.fsync", fail_fsync)
    monkeypatch.setattr("nslab.state.os.close", close_then_fail)

    with pytest.raises(OSError, match="primary fsync failure"):
        StateStore(tmp_path)._fsync_directory()


def test_directory_close_only_error_is_suppressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_close = os.close

    def close_then_fail(file_descriptor: int) -> None:
        real_close(file_descriptor)
        raise OSError("close-only failure")

    monkeypatch.setattr("nslab.state.os.close", close_then_fail)

    StateStore(tmp_path)._fsync_directory()
