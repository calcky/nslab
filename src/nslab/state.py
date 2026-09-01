from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Literal, Self, cast

from nslab.errors import NslabError
from nslab.manifest import NAME_PATTERN

_DIRECTORY_MODE = 0o755
_STATE_FILE_MODE = 0o644
_LOCK_MODE = 0o600
_LOCK_POLL_INTERVAL = 0.01
_SNAPSHOT_STATUSES = frozenset({"deploying", "deployed", "destroying"})
_SNAPSHOT_FIELDS = frozenset(
    {
        "schema",
        "name",
        "fingerprint",
        "manifest",
        "namespaces",
        "interfaces",
        "created_at",
        "status",
    }
)

type SnapshotStatus = Literal["deploying", "deployed", "destroying"]


def _freeze_json(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            frozen[key] = _freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise ValueError(f"value is not JSON serializable: {type(value).__name__}")


def _freeze_mapping(value: Mapping[str, object], label: str) -> Mapping[str, object]:
    frozen = _freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise ValueError(f"{label} must be an object")
    return frozen


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def _deployment_name_error(name: str) -> NslabError:
    return NslabError(
        code="DEPLOYMENT_NAME_INVALID",
        message=f"invalid deployment name: {name!r}",
        details={"name": name},
    )


def _validate_deployment_name(name: str) -> str:
    if NAME_PATTERN.fullmatch(name) is None:
        raise _deployment_name_error(name)
    return name


def _error_details(path: Path, error: BaseException) -> dict[str, object]:
    details: dict[str, object] = {"path": str(path), "error": str(error)}
    error_number = getattr(error, "errno", None)
    if isinstance(error_number, int):
        details["errno"] = error_number
    return details


def _state_error(code: str, action: str, path: Path, error: BaseException) -> NslabError:
    return NslabError(
        code=code,
        message=f"failed to {action} deployment state: {path}",
        details=_error_details(path, error),
    )


def _cause_details(error: BaseException) -> dict[str, object]:
    return {
        "cause_type": type(error).__name__,
        "cause_message": str(error),
    }


def _durability_error(path: Path, operation: str, error: BaseException) -> NslabError:
    details = _error_details(path, error)
    details.update(
        {
            "operation": operation,
            "committed": True,
            **_cause_details(error),
        }
    )
    return NslabError(
        code="STATE_DURABILITY_UNCERTAIN",
        message=f"deployment state {operation} committed but durability is uncertain: {path}",
        details=details,
    )


def _commit_outcome_unknown(path: Path, operation: str, error: BaseException) -> NslabError:
    return NslabError(
        code="STATE_COMMIT_OUTCOME_UNKNOWN",
        message=f"deployment state {operation} outcome is unknown: {path}",
        details={
            "path": str(path),
            "operation": operation,
            "committed": "unknown",
            **_cause_details(error),
        },
    )


def _ensure_directory(root: Path, *, code: str, action: str) -> None:
    try:
        root.mkdir(parents=True, mode=_DIRECTORY_MODE, exist_ok=True)
        os.chmod(root, _DIRECTORY_MODE)
    except OSError as error:
        raise _state_error(code, action, root, error) from error


def _validate_string_mapping(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError(f"{label} keys and values must be strings")
        result[key] = item
    return result


def _validate_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{label} keys must be strings")
        result[key] = item
    return result


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """The durable identity and normalized desired state for one deployment."""

    schema: int
    name: str
    fingerprint: str
    manifest: Mapping[str, object]
    namespaces: Mapping[str, str]
    interfaces: Mapping[str, object]
    created_at: str
    status: SnapshotStatus = "deployed"

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest", _freeze_mapping(self.manifest, "manifest"))
        object.__setattr__(self, "namespaces", MappingProxyType(dict(self.namespaces)))
        object.__setattr__(
            self,
            "interfaces",
            _freeze_mapping(self.interfaces, "interfaces"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "name": self.name,
            "fingerprint": self.fingerprint,
            "manifest": _thaw_json(self.manifest),
            "namespaces": dict(self.namespaces),
            "interfaces": _thaw_json(self.interfaces),
            "created_at": self.created_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, document: object) -> Self:
        if not isinstance(document, dict):
            raise ValueError("snapshot must be a JSON object")

        fields = set(document)
        required_fields = _SNAPSHOT_FIELDS - {"status"}
        missing = sorted(required_fields - fields)
        unexpected = sorted(fields - _SNAPSHOT_FIELDS)
        if missing:
            raise ValueError(f"snapshot is missing fields: {', '.join(missing)}")
        if unexpected:
            raise ValueError(f"snapshot has unknown fields: {', '.join(unexpected)}")

        schema = document["schema"]
        if type(schema) is not int or schema != 1:
            raise ValueError(f"unsupported snapshot schema: {schema!r}")

        name = document["name"]
        if not isinstance(name, str) or NAME_PATTERN.fullmatch(name) is None:
            raise ValueError(f"invalid snapshot deployment name: {name!r}")

        fingerprint = document["fingerprint"]
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ValueError("snapshot fingerprint must be a lowercase SHA-256 digest")

        created_at = document["created_at"]
        if not isinstance(created_at, str) or not created_at:
            raise ValueError("snapshot creation timestamp must be a non-empty string")

        if "status" in document:
            status_value = document["status"]
            if not isinstance(status_value, str) or status_value not in _SNAPSHOT_STATUSES:
                raise ValueError(f"invalid snapshot status: {status_value!r}")
            status = cast(SnapshotStatus, status_value)
        else:
            status = "deployed"

        manifest = _validate_object(document["manifest"], "manifest")
        namespaces = _validate_string_mapping(document["namespaces"], "namespaces")
        interfaces = _validate_object(document["interfaces"], "interfaces")
        _freeze_json(manifest)
        _freeze_json(interfaces)

        return cls(
            schema=schema,
            name=name,
            fingerprint=fingerprint,
            manifest=manifest,
            namespaces=namespaces,
            interfaces=interfaces,
            created_at=created_at,
            status=status,
        )


class StateStore:
    """Read and atomically replace per-deployment state snapshots."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, name: str) -> Path:
        validated_name = _validate_deployment_name(name)
        return self.root / f"{validated_name}.json"

    def load(self, name: str) -> StateSnapshot | None:
        path = self._path(name)
        try:
            with path.open(encoding="utf-8") as state_file:
                document = json.load(
                    state_file,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(f"invalid JSON number: {value}")
                    ),
                )
        except FileNotFoundError:
            return None
        except (UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise NslabError(
                code="STATE_INVALID",
                message=f"invalid deployment state: {path}",
                details=_error_details(path, error),
            ) from error
        except OSError as error:
            raise _state_error("STATE_READ_FAILED", "read", path, error) from error

        try:
            return StateSnapshot.from_dict(document)
        except (TypeError, ValueError) as error:
            raise NslabError(
                code="STATE_INVALID",
                message=f"invalid deployment state: {path}",
                details=_error_details(path, error),
            ) from error

    def save(self, snapshot: StateSnapshot) -> None:
        path = self._path(snapshot.name)
        try:
            validated = StateSnapshot.from_dict(snapshot.to_dict())
            serialized = json.dumps(
                validated.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            ) + "\n"
        except (TypeError, ValueError) as error:
            raise NslabError(
                code="STATE_INVALID",
                message=f"invalid deployment state: {path}",
                details=_error_details(path, error),
            ) from error

        _ensure_directory(self.root, code="STATE_WRITE_FAILED", action="write")
        temporary_path: Path | None = None
        replaced = False
        file_descriptor: int | None = None

        try:
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f"{path.name}.tmp-{os.getpid()}-",
                dir=self.root,
            )
            temporary_path = Path(temporary_name)
            os.fchmod(file_descriptor, _STATE_FILE_MODE)

            state_file = os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n")
            file_descriptor = None
            try:
                state_file.write(serialized)
                state_file.flush()
                os.fsync(state_file.fileno())
            finally:
                active_error = sys.exception()
                try:
                    state_file.close()
                except BaseException:
                    if active_error is None:
                        raise

            try:
                os.replace(temporary_path, path)
                replaced = True
                self._fsync_directory()
            except OSError as error:
                if not replaced:
                    raise _commit_outcome_unknown(path, "save", error) from error
                raise _durability_error(path, "save", error) from error
            except BaseException as error:
                if not replaced:
                    raise _commit_outcome_unknown(path, "save", error) from error
                raise _durability_error(path, "save", error) from error
        except (OSError, UnicodeError) as error:
            raise _state_error("STATE_WRITE_FAILED", "write", path, error) from error
        finally:
            self._cleanup_temporary(
                temporary_path,
                replaced=replaced,
                file_descriptor=file_descriptor,
            )

    def delete(self, name: str) -> None:
        path = self._path(name)
        unlinked = False
        try:
            path.unlink()
            unlinked = True
            self._fsync_directory()
        except FileNotFoundError as error:
            if not unlinked:
                return
            raise _durability_error(path, "delete", error) from error
        except OSError as error:
            if not unlinked:
                raise _commit_outcome_unknown(path, "delete", error) from error
            raise _durability_error(path, "delete", error) from error
        except BaseException as error:
            if not unlinked:
                raise _commit_outcome_unknown(path, "delete", error) from error
            raise _durability_error(path, "delete", error) from error

    def _fsync_directory(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(self.root, flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            with suppress(OSError):
                os.close(directory_descriptor)

    @staticmethod
    def _cleanup_temporary(
        temporary_path: Path | None,
        *,
        replaced: bool,
        file_descriptor: int | None,
    ) -> None:
        active_error = sys.exception()
        cleanup_error: BaseException | None = None
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except BaseException as error:
                cleanup_error = error

        if temporary_path is not None and not replaced:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error

        if cleanup_error is not None and active_error is None:
            raise cleanup_error


class DeploymentLock:
    """A non-blocking flock retried up to a monotonic deadline."""

    def __init__(self, root: Path, name: str, timeout: float = 5.0) -> None:
        self.root = root
        self.name = _validate_deployment_name(name)
        self.path = root / f"{self.name}.lock"
        self.timeout = max(0.0, float(timeout))
        self._file_descriptor: int | None = None

    def __enter__(self) -> Self:
        if self._file_descriptor is not None:
            raise RuntimeError("deployment lock is already held by this object")

        _ensure_directory(self.root, code="DEPLOYMENT_LOCK_FAILED", action="open lock for")
        file_descriptor: int | None = None
        acquired = False
        try:
            flags = os.O_CREAT | os.O_RDWR
            flags |= getattr(os, "O_CLOEXEC", 0)
            file_descriptor = os.open(self.path, flags, _LOCK_MODE)
            os.fchmod(file_descriptor, _LOCK_MODE)
            self._file_descriptor = file_descriptor
            deadline = time.monotonic() + self.timeout

            while True:
                try:
                    fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    return self
                except OSError as error:
                    if not self._is_contention(error):
                        raise NslabError(
                            code="DEPLOYMENT_LOCK_FAILED",
                            message=f"failed to acquire deployment lock: {self.path}",
                            details={
                                "name": self.name,
                                **_error_details(self.path, error),
                            },
                        ) from error

                    now = time.monotonic()
                    if now >= deadline:
                        raise NslabError(
                            code="DEPLOYMENT_LOCKED",
                            message=f"deployment is locked: {self.name}",
                            details={
                                "name": self.name,
                                "path": str(self.path),
                                "timeout": self.timeout,
                            },
                        ) from error
                    time.sleep(min(_LOCK_POLL_INTERVAL, deadline - now))
        except OSError as error:
            raise NslabError(
                code="DEPLOYMENT_LOCK_FAILED",
                message=f"failed to open deployment lock: {self.path}",
                details={"name": self.name, **_error_details(self.path, error)},
            ) from error
        finally:
            if not acquired and file_descriptor is not None:
                self._file_descriptor = None
                with suppress(OSError):
                    os.close(file_descriptor)

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del exception, traceback
        file_descriptor = self._file_descriptor
        self._file_descriptor = None
        if file_descriptor is None:
            return False

        release_error: OSError | None = None
        try:
            fcntl.flock(file_descriptor, fcntl.LOCK_UN)
        except OSError as error:
            release_error = error
        finally:
            try:
                os.close(file_descriptor)
            except OSError as error:
                if release_error is None:
                    release_error = error

        if release_error is not None and exception_type is None:
            raise NslabError(
                code="DEPLOYMENT_LOCK_FAILED",
                message=f"failed to release deployment lock: {self.path}",
                details={"name": self.name, **_error_details(self.path, release_error)},
            ) from release_error
        return False

    @staticmethod
    def _is_contention(error: OSError) -> bool:
        return isinstance(error, BlockingIOError) or error.errno in {
            errno.EACCES,
            errno.EAGAIN,
        }
