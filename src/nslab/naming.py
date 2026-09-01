from __future__ import annotations

import hashlib


def namespace_name(deployment: str, node: str) -> str:
    identity = f"{deployment}:{node}".encode("utf-8")  # noqa: UP012
    digest = hashlib.blake2s(identity, digest_size=8).hexdigest()
    return f"nslab-{deployment}-{node}-{digest}"


def temporary_veth_names(deployment: str, link_index: int) -> tuple[str, str]:
    identity = f"{deployment}:{link_index}".encode("utf-8")  # noqa: UP012
    digest = hashlib.blake2s(identity, digest_size=5).hexdigest()
    return f"nl{digest}a", f"nl{digest}b"
