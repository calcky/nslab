from __future__ import annotations

import hashlib

from nslab.naming import namespace_name, temporary_veth_names


def test_temporary_veth_names_use_exact_deterministic_blake2s_identity() -> None:
    left1, right1 = temporary_veth_names("bridge-fdb", 0)
    left2, right2 = temporary_veth_names("bridge-fdb", 0)
    digest = hashlib.blake2s(b"bridge-fdb:0", digest_size=5).hexdigest()

    assert (left1, right1) == (left2, right2)
    assert (left1, right1) == (f"nl{digest}a", f"nl{digest}b")
    assert left1 != right1
    assert len(left1.encode("utf-8")) <= 15
    assert len(right1.encode("utf-8")) <= 15
    assert temporary_veth_names("bridge-fdb", 1) != (left1, right1)


def test_namespace_name_includes_effective_deployment_and_node() -> None:
    digest = hashlib.blake2s(b"bridge-fdb:h1", digest_size=8).hexdigest()

    first = namespace_name("bridge-fdb", "h1")
    second = namespace_name("bridge-fdb", "h1")

    assert first == second
    assert first == f"nslab-bridge-fdb-h1-{digest}"


def test_namespace_name_distinguishes_ambiguous_readable_prefix_pairs() -> None:
    assert namespace_name("a-b", "c") != namespace_name("a", "b-c")


def test_namespace_name_preserves_maximum_valid_names() -> None:
    deployment = "d" * 32
    node = "n" * 32

    result = namespace_name(deployment, node)

    identity = f"{deployment}:{node}".encode("utf-8")  # noqa: UP012
    digest = hashlib.blake2s(identity, digest_size=8).hexdigest()
    assert result == f"nslab-{deployment}-{node}-{digest}"
    assert len(result.encode("utf-8")) <= 255
