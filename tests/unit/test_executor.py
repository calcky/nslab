from __future__ import annotations

from collections.abc import Sequence
from typing import cast
from unittest.mock import Mock

import pytest

from nslab.backend.base import ExecResult, NetworkBackend
from nslab.errors import NslabError
from nslab.executor import execute_in_node
from nslab.manifest import Manifest
from nslab.planner import TopologyPlan, compile_plan


class _LegacyExecuteBackend:
    def __init__(self, result: ExecResult) -> None:
        self.result = result
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def execute(self, namespace: str, argv: Sequence[str]) -> ExecResult:
        self.calls.append((namespace, tuple(argv)))
        return self.result


@pytest.fixture
def plan() -> TopologyPlan:
    manifest = Manifest.model_validate(
        {
            "version": 1,
            "name": "exec-lab",
            "topology": {
                "nodes": {"h1": {"kind": "linux"}},
                "links": [],
            },
        }
    )
    return compile_plan(manifest)


def test_unknown_node_is_rejected_before_backend_execution(plan: TopologyPlan) -> None:
    backend = Mock(spec=NetworkBackend)

    with pytest.raises(NslabError) as caught:
        execute_in_node(backend, plan, "missing", ("true",))

    assert caught.value.code == "NODE_NOT_FOUND"
    assert caught.value.details == {"name": plan.name, "node": "missing"}
    backend.execute.assert_not_called()


def test_empty_argv_is_rejected_before_backend_execution(plan: TopologyPlan) -> None:
    backend = Mock(spec=NetworkBackend)

    with pytest.raises(NslabError) as caught:
        execute_in_node(backend, plan, "h1", ())

    assert caught.value.code == "EXEC_COMMAND_REQUIRED"
    assert caught.value.details == {"name": plan.name, "node": "h1"}
    backend.execute.assert_not_called()


@pytest.mark.parametrize(
    ("argv", "index"),
    [
        (("bad\0command",), 0),
        (("printf", "bad\0argument"), 1),
    ],
)
def test_nul_character_is_rejected_with_argument_index(
    plan: TopologyPlan,
    argv: tuple[str, ...],
    index: int,
) -> None:
    backend = Mock(spec=NetworkBackend)

    with pytest.raises(NslabError) as caught:
        execute_in_node(backend, plan, "h1", argv)

    assert caught.value.code == "EXEC_ARGUMENT_INVALID"
    assert caught.value.details == {
        "name": plan.name,
        "node": "h1",
        "argument_index": index,
    }
    backend.execute.assert_not_called()


def test_valid_argv_is_immutable_and_result_is_preserved(plan: TopologyPlan) -> None:
    result = ExecResult(
        argv=("ping", "-c", "1", "10.10.0.2"),
        returncode=7,
        stdout="out\n",
        stderr="err\n",
    )
    backend = Mock(spec=NetworkBackend)
    backend.execute.return_value = result
    argv = ["ping", "-c", "1", "10.10.0.2"]

    observed = execute_in_node(backend, plan, "h1", argv)

    assert observed is result
    backend.execute.assert_called_once_with(
        plan.nodes["h1"].namespace,
        ("ping", "-c", "1", "10.10.0.2"),
    )


def test_default_capture_is_compatible_with_two_argument_legacy_backend(
    plan: TopologyPlan,
) -> None:
    result = ExecResult(
        argv=("printf", "hello"),
        returncode=0,
        stdout="hello",
        stderr="",
    )
    backend = _LegacyExecuteBackend(result)

    observed = execute_in_node(
        cast(NetworkBackend, backend),
        plan,
        "h1",
        ("printf", "hello"),
    )

    assert observed is result
    assert backend.calls == [
        (plan.nodes["h1"].namespace, ("printf", "hello")),
    ]


def test_passthrough_choice_is_forwarded_to_backend(plan: TopologyPlan) -> None:
    result = ExecResult(argv=("iperf3", "-s"), returncode=0, stdout="", stderr="")
    backend = Mock(spec=NetworkBackend)
    backend.execute.return_value = result

    observed = execute_in_node(
        backend,
        plan,
        "h1",
        ("iperf3", "-s"),
        capture_output=False,
    )

    assert observed is result
    backend.execute.assert_called_once_with(
        plan.nodes["h1"].namespace,
        ("iperf3", "-s"),
        capture_output=False,
    )
