from __future__ import annotations

from collections.abc import Sequence

from nslab.backend.base import ExecResult, NetworkBackend
from nslab.errors import NslabError
from nslab.planner import TopologyPlan


def execute_in_node(
    backend: NetworkBackend,
    plan: TopologyPlan,
    node: str,
    argv: Sequence[str],
    *,
    capture_output: bool = True,
) -> ExecResult:
    """Execute an immutable argv directly in one planned node namespace."""

    planned_node = plan.nodes.get(node)
    if planned_node is None:
        raise NslabError(
            code="NODE_NOT_FOUND",
            message=f"topology node is not defined: {node}",
            details={"name": plan.name, "node": node},
        )

    immutable_argv = tuple(argv)
    if not immutable_argv:
        raise NslabError(
            code="EXEC_COMMAND_REQUIRED",
            message=f"an execution command is required for node: {node}",
            details={"name": plan.name, "node": node},
        )

    for index, argument in enumerate(immutable_argv):
        if "\0" in argument:
            raise NslabError(
                code="EXEC_ARGUMENT_INVALID",
                message=f"execution argument contains a NUL character for node: {node}",
                details={
                    "name": plan.name,
                    "node": node,
                    "argument_index": index,
                },
            )

    if capture_output:
        return backend.execute(planned_node.namespace, immutable_argv)
    return backend.execute(
        planned_node.namespace,
        immutable_argv,
        capture_output=False,
    )
