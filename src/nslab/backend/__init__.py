"""Network backend protocol, inventory values, and implementations."""

from nslab.backend.base import (
    ExecResult,
    InterfaceInventory,
    LiveInventory,
    NamespaceInventory,
    NetworkBackend,
    inventory_matches_plan,
)
from nslab.backend.fake import FakeNetworkBackend

__all__ = [
    "ExecResult",
    "FakeNetworkBackend",
    "InterfaceInventory",
    "LiveInventory",
    "NamespaceInventory",
    "NetworkBackend",
    "inventory_matches_plan",
]
