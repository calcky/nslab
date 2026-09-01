from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NslabError(Exception):
    """A domain error with a stable machine-readable code."""

    code: str
    message: str
    details: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class OperationCancelled(NslabError):
    """Raised when a lifecycle operation is interrupted."""

    def __init__(
        self,
        message: str = "operation cancelled",
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(
            code="OPERATION_CANCELLED",
            message=message,
            details={} if details is None else details,
        )
