from __future__ import annotations

import json
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest

from nslab.backend.base import InterfaceInventory, LiveInventory
from nslab.backend.fake import FakeNetworkBackend
from nslab.errors import NslabError, OperationCancelled
from nslab.lifecycle import LifecycleResult, LifecycleService
from nslab.manifest import Manifest, normalized_manifest
from nslab.planner import LinkPlan, NodePlan, TopologyPlan, compile_plan
from nslab.state import StateSnapshot, StateStore


@pytest.fixture
def manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "version": 1,
            "name": "bridge-fdb",
            "topology": {
                "nodes": {
                    "h1": {
                        "kind": "linux",
                        "interfaces": {"eth0": {"addresses": ["10.10.0.1/24"]}},
                    },
                    "sw1": {
                        "kind": "bridge",
                        "bridge": {
                            "name": "br0",
                            "stp": False,
                            "vlan_filtering": False,
                        },
                    },
                    "h2": {
                        "kind": "linux",
                        "interfaces": {"eth0": {"addresses": ["10.10.0.2/24"]}},
                    },
                },
                "links": [
                    {"endpoints": ["h1:eth0", "sw1:swp1"], "mtu": 1500},
                    {"endpoints": ["h2:eth0", "sw1:swp2"], "mtu": 1400},
                ],
            },
        }
    )


@pytest.fixture
def plan(manifest: Manifest) -> TopologyPlan:
    return compile_plan(manifest)


class _Lock(AbstractContextManager[object]):
    def __init__(self, factory: _LockFactory, name: str) -> None:
        self.factory = factory
        self.name = name

    def __enter__(self) -> Self:
        assert not self.factory.held, "deployment lock was re-entered"
        self.factory.held = True
        self.factory.entries.append(self.name)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.factory.held = False


class _LockFactory:
    def __init__(self) -> None:
        self.held = False
        self.entries: list[str] = []

    def __call__(self, name: str) -> _Lock:
        return _Lock(self, name)


class _StateCheckingBackend(FakeNetworkBackend):
    def __init__(self, store: StateStore, name: str) -> None:
        super().__init__()
        self.store = store
        self.name = name

    def create_namespace(self, node: NodePlan) -> None:
        snapshot = self.store.load(self.name)
        assert snapshot is not None
        assert snapshot.status == "deploying"
        assert all(
            not isinstance(value, dict)
            or value.get("kind") != "veth"
            or value.get("link_id") is None
            for value in snapshot.to_dict()["interfaces"].values()  # type: ignore[union-attr]
        )
        super().create_namespace(node)


class _DestroyStateCheckingBackend(FakeNetworkBackend):
    def __init__(self, store: StateStore, name: str) -> None:
        super().__init__()
        self.store = store
        self.name = name
        self.destroying_seen = False

    def delete_namespace(self, namespace: str) -> None:
        snapshot = self.store.load(self.name)
        assert snapshot is not None
        assert snapshot.status == "destroying"
        assert snapshot.created_at == "2026-08-31T10:15:30+00:00"
        self.destroying_seen = True
        super().delete_namespace(namespace)


class _ControlledFailureBackend(FakeNetworkBackend):
    def __init__(self) -> None:
        super().__init__()
        self.fail_second_veth = False
        self.fail_delete: set[str] = set()
        self.fail_create_namespace = False
        self._veth_calls = 0

    def create_namespace(self, node: NodePlan) -> None:
        if self.fail_create_namespace:
            self._record("create_namespace", node.namespace)
            raise NslabError(
                code="BACKEND_FAILURE",
                message=f"injected create failure: {node.namespace}",
                details={"operation": "create_namespace", "resource": node.namespace},
            )
        super().create_namespace(node)

    def create_veth(self, link: LinkPlan) -> None:
        self._veth_calls += 1
        if self.fail_second_veth and self._veth_calls == 2:
            resource = f"{link.left.temporary_name}<->{link.right.temporary_name}"
            self._record("create_veth", resource)
            raise NslabError(
                code="BACKEND_FAILURE",
                message=f"injected veth failure: {resource}",
                details={"operation": "create_veth", "resource": resource},
            )
        super().create_veth(link)

    def delete_namespace(self, namespace: str) -> None:
        if namespace in self.fail_delete:
            self._record("delete_namespace", namespace)
            raise NslabError(
                code="BACKEND_FAILURE",
                message=f"injected delete failure: {namespace}",
                details={"operation": "delete_namespace", "resource": namespace},
            )
        super().delete_namespace(namespace)


class _RootArtifactRollbackBackend(_ControlledFailureBackend):
    def __init__(self) -> None:
        super().__init__()
        self.fail_second_veth = True

    def create_veth(self, link: LinkPlan) -> None:
        if self._veth_calls == 1:
            self.root_interfaces[link.left.temporary_name] = InterfaceInventory(
                name=link.left.temporary_name,
                kind="veth",
                ifindex=900,
                master=None,
                mtu=link.mtu,
                up=False,
                link_id="rollback-leftover",
            )
        super().create_veth(link)


class _InterruptingDeleteBackend(FakeNetworkBackend):
    def __init__(self, failure: OperationCancelled | KeyboardInterrupt) -> None:
        super().__init__()
        self.failure = failure
        self.interrupt_deletes = False

    def delete_namespace(self, namespace: str) -> None:
        if self.interrupt_deletes:
            self._record("delete_namespace", namespace)
            raise self.failure
        super().delete_namespace(namespace)


class _InventoryFailureBackend(FakeNetworkBackend):
    def __init__(self, fail_on_inventory: int) -> None:
        super().__init__()
        self.fail_on_inventory = fail_on_inventory
        self.inventory_calls = 0

    def inventory(self, plan: TopologyPlan) -> LiveInventory:
        self.inventory_calls += 1
        if self.inventory_calls == self.fail_on_inventory:
            self._record("inventory", plan.name)
            raise NslabError(
                code="BACKEND_FAILURE",
                message=f"injected inventory failure: {plan.name}",
                details={"operation": "inventory", "resource": plan.name},
            )
        return super().inventory(plan)


class _DriftingReconciliationBackend(FakeNetworkBackend):
    def __init__(self) -> None:
        super().__init__()
        self.inventory_calls = 0

    def inventory(self, plan: TopologyPlan) -> LiveInventory:
        self.inventory_calls += 1
        inventory = super().inventory(plan)
        if self.inventory_calls != 3:
            return inventory

        namespace_name = next(iter(inventory.namespaces))
        namespace = inventory.namespaces[namespace_name]
        interfaces = dict(namespace.interfaces)
        drifted_interface = next(name for name in interfaces if name != "lo")
        interfaces.pop(drifted_interface)
        namespaces = dict(inventory.namespaces)
        namespaces[namespace_name] = replace(namespace, interfaces=interfaces)
        return LiveInventory(
            namespaces=namespaces,
            root_interfaces=inventory.root_interfaces,
        )


class _MalformedAbsentBackend(FakeNetworkBackend):
    def __init__(self, field: str, malform_on_inventory: int) -> None:
        super().__init__()
        self.field = field
        self.malform_on_inventory = malform_on_inventory
        self.inventory_calls = 0

    def inventory(self, plan: TopologyPlan) -> LiveInventory:
        self.inventory_calls += 1
        inventory = super().inventory(plan)
        if self.inventory_calls != self.malform_on_inventory:
            return inventory

        namespace_name = next(iter(inventory.namespaces))
        observed = inventory.namespaces[namespace_name]
        if self.field == "node":
            malformed = replace(observed, node="wrong-node")
        elif self.field == "kind":
            wrong_kind = "bridge" if observed.kind == "linux" else "linux"
            malformed = replace(observed, kind=wrong_kind)
        else:
            malformed = replace(observed, namespace="wrong-namespace")
        namespaces = dict(inventory.namespaces)
        namespaces[namespace_name] = malformed
        return LiveInventory(
            namespaces=namespaces,
            root_interfaces=inventory.root_interfaces,
        )


class _InjectedStateStore(StateStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.calls: list[tuple[str, str]] = []
        self.save_failure: tuple[str, Exception | KeyboardInterrupt, bool] | None = None
        self.delete_failures: list[tuple[Exception | KeyboardInterrupt, bool]] = []

    def arm_save(
        self,
        status: str,
        error: Exception | KeyboardInterrupt,
        *,
        commit: bool,
    ) -> None:
        self.save_failure = (status, error, commit)

    def arm_delete(
        self,
        error: Exception | KeyboardInterrupt,
        *,
        commit: bool,
    ) -> None:
        self.delete_failures.append((error, commit))

    def load(self, name: str) -> StateSnapshot | None:
        self.calls.append(("load", name))
        return super().load(name)

    def save(self, snapshot: StateSnapshot) -> None:
        self.calls.append(("save", snapshot.status))
        failure = self.save_failure
        if failure is not None and failure[0] == snapshot.status:
            self.save_failure = None
            _, error, commit = failure
            if commit:
                super().save(snapshot)
            raise error
        super().save(snapshot)

    def delete(self, name: str) -> None:
        self.calls.append(("delete", name))
        if self.delete_failures:
            error, commit = self.delete_failures.pop(0)
            if commit:
                super().delete(name)
            raise error
        super().delete(name)


class _BrokenReconciliationStore(_InjectedStateStore):
    def __init__(self, root: Path, reconciliation_failure: Exception) -> None:
        super().__init__(root)
        self.reconciliation_failure = reconciliation_failure
        self.fail_next_load = False

    def load(self, name: str) -> StateSnapshot | None:
        if self.fail_next_load:
            self.fail_next_load = False
            self.calls.append(("load", name))
            raise self.reconciliation_failure
        return super().load(name)

    def save(self, snapshot: StateSnapshot) -> None:
        try:
            super().save(snapshot)
        except Exception:
            self.fail_next_load = True
            raise


def _uncertainty(code: str, operation: str) -> NslabError:
    return NslabError(
        code=code,
        message=f"injected uncertain state {operation}",
        details={"operation": operation, "committed": "unknown"},
    )


def _service(
    backend: FakeNetworkBackend,
    store: StateStore,
    locks: _LockFactory | None = None,
) -> LifecycleService:
    return LifecycleService(
        backend,
        store,
        locks if locks is not None else _LockFactory(),
        clock=lambda: datetime(2026, 8, 31, 10, 15, 30, tzinfo=UTC),
    )


def _snapshot(
    plan: TopologyPlan,
    manifest: Manifest,
    *,
    status: str = "deployed",
    fingerprint: str | None = None,
) -> StateSnapshot:
    interfaces: dict[str, object] = {}
    for node in plan.nodes.values():
        if node.kind == "bridge":
            assert node.bridge_name is not None
            interfaces[f"{node.name}:{node.bridge_name}"] = {
                "name": node.bridge_name,
                "kind": "bridge",
                "namespace": node.namespace,
                "ifindex": None,
            }
    for link in plan.links:
        link_id = f"snapshot-link-{link.index}" if status == "deployed" else None
        for endpoint in (link.left, link.right):
            interfaces[f"{endpoint.node}:{endpoint.interface}"] = {
                "name": endpoint.interface,
                "kind": "veth",
                "namespace": endpoint.namespace,
                "ifindex": None,
                "temporary_name": endpoint.temporary_name,
                "link_id": link_id,
            }
    return StateSnapshot(
        schema=1,
        name=plan.name,
        fingerprint=plan.fingerprint if fingerprint is None else fingerprint,
        manifest=normalized_manifest(manifest),
        namespaces={name: node.namespace for name, node in plan.nodes.items()},
        interfaces=interfaces,
        created_at="2026-08-31T10:15:30+00:00",
        status=status,  # type: ignore[arg-type]
    )


def _persisted_plan(store: StateStore, name: str) -> TopologyPlan:
    snapshot = store.load(name)
    assert snapshot is not None
    manifest = Manifest.model_validate(snapshot.to_dict()["manifest"])
    return compile_plan(manifest, name_override=name)


def test_first_deploy_orders_operations_persists_snapshot_and_second_is_noop(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
) -> None:
    store = StateStore(tmp_path)
    backend = _StateCheckingBackend(store, plan.name)
    locks = _LockFactory()
    service = _service(backend, store, locks)

    result = service.deploy(plan, manifest)

    h1, sw1, h2 = plan.nodes.values()
    first_link, second_link = plan.links
    assert backend.calls == [
        ("inventory", plan.name),
        ("create_namespace", h1.namespace),
        ("create_namespace", sw1.namespace),
        ("create_namespace", h2.namespace),
        ("create_bridge", f"{sw1.namespace}:br0"),
        (
            "create_veth",
            f"{first_link.left.temporary_name}<->{first_link.right.temporary_name}",
        ),
        (
            "create_veth",
            f"{second_link.left.temporary_name}<->{second_link.right.temporary_name}",
        ),
        ("configure_node", h1.namespace),
        ("configure_node", sw1.namespace),
        ("configure_node", h2.namespace),
        ("inventory", plan.name),
    ]
    assert result == LifecycleResult(
        action="deploy",
        name=plan.name,
        changed=True,
        status="deployed",
        message=f"deployed topology: {plan.name}",
    )
    snapshot = store.load(plan.name)
    assert snapshot is not None
    assert snapshot.status == "deployed"
    assert snapshot.to_dict()["manifest"] == normalized_manifest(manifest)
    assert snapshot.namespaces == {name: node.namespace for name, node in plan.nodes.items()}
    assert snapshot.created_at == "2026-08-31T10:15:30+00:00"
    assert set(snapshot.interfaces) == {
        "h1:eth0",
        "sw1:br0",
        "sw1:swp1",
        "sw1:swp2",
        "h2:eth0",
    }
    assert all(
        isinstance(value, dict) and value["ifindex"] is not None
        for value in snapshot.to_dict()["interfaces"].values()  # type: ignore[union-attr]
    )
    ownership = snapshot.to_dict()["interfaces"]
    assert isinstance(ownership, dict)
    first_link_id = ownership["h1:eth0"]["link_id"]  # type: ignore[index]
    second_link_id = ownership["h2:eth0"]["link_id"]  # type: ignore[index]
    assert first_link_id
    assert first_link_id == ownership["sw1:swp1"]["link_id"]  # type: ignore[index]
    assert second_link_id
    assert second_link_id == ownership["sw1:swp2"]["link_id"]  # type: ignore[index]
    assert first_link_id != second_link_id
    state_bytes = (tmp_path / f"{plan.name}.json").read_bytes()
    backend.calls.clear()

    no_op = service.deploy(plan, manifest)

    assert no_op.changed is False
    assert backend.calls == [("inventory", plan.name)]
    assert (tmp_path / f"{plan.name}.json").read_bytes() == state_bytes
    assert locks.entries == [plan.name, plan.name]


@pytest.mark.parametrize("replacement", ["new", "swapped"])
def test_existing_deploy_requires_live_link_ids_to_match_recorded_snapshot(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    replacement: str,
) -> None:
    store = StateStore(tmp_path)
    backend = FakeNetworkBackend()
    service = _service(backend, store)
    service.deploy(plan, manifest)
    snapshot = store.load(plan.name)
    assert snapshot is not None
    recorded = snapshot.to_dict()["interfaces"]
    assert isinstance(recorded, dict)
    first_link, second_link = plan.links
    if replacement == "new":
        identities = ("replacement-link-1", "replacement-link-2")
    else:
        identities = (
            recorded[f"{second_link.left.node}:{second_link.left.interface}"]["link_id"],
            recorded[f"{first_link.left.node}:{first_link.left.interface}"]["link_id"],
        )

    for link, link_id in zip(plan.links, identities, strict=True):
        for endpoint in (link.left, link.right):
            state = backend.namespaces[endpoint.namespace]
            state.interfaces[endpoint.interface] = replace(
                state.interfaces[endpoint.interface],
                link_id=link_id,
            )
    backend.calls.clear()

    with pytest.raises(NslabError) as caught:
        service.deploy(plan, manifest)

    assert caught.value.code == "DEPLOYMENT_DRIFT"
    assert backend.calls == [("inventory", plan.name)]


def test_legacy_deployed_snapshot_without_link_ids_is_unknown_not_invalid(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
) -> None:
    store = StateStore(tmp_path)
    backend = FakeNetworkBackend()
    service = _service(backend, store)
    service.deploy(plan, manifest)
    snapshot = store.load(plan.name)
    assert snapshot is not None
    document = snapshot.to_dict()
    interfaces = document["interfaces"]
    assert isinstance(interfaces, dict)
    for value in interfaces.values():
        assert isinstance(value, dict)
        if value.get("kind") == "veth":
            value.pop("link_id")
    store.save(StateSnapshot.from_dict(document))
    backend.calls.clear()

    with pytest.raises(NslabError) as caught:
        service.deploy(plan, manifest)

    assert caught.value.code == "DEPLOYMENT_DRIFT"
    assert backend.calls == [("inventory", plan.name)]


def test_deployed_snapshot_cannot_mix_complete_and_legacy_link_identity(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
) -> None:
    store = StateStore(tmp_path)
    backend = FakeNetworkBackend()
    service = _service(backend, store)
    service.deploy(plan, manifest)
    snapshot = store.load(plan.name)
    assert snapshot is not None
    document = snapshot.to_dict()
    interfaces = document["interfaces"]
    assert isinstance(interfaces, dict)
    second = plan.links[1]
    for endpoint in (second.left, second.right):
        record = interfaces[f"{endpoint.node}:{endpoint.interface}"]
        assert isinstance(record, dict)
        record.pop("link_id")
    store.save(StateSnapshot.from_dict(document))
    backend.calls.clear()

    with pytest.raises(NslabError) as caught:
        service.deploy(plan, manifest)

    assert caught.value.code == "STATE_INVALID"
    assert backend.calls == []


@pytest.mark.parametrize(
    "invalidity",
    ["empty", "partial_missing", "mismatch", "reused"],
)
def test_explicit_invalid_deployed_snapshot_link_ids_are_state_invalid(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    invalidity: str,
) -> None:
    store = StateStore(tmp_path)
    backend = FakeNetworkBackend()
    service = _service(backend, store)
    service.deploy(plan, manifest)
    snapshot = store.load(plan.name)
    assert snapshot is not None
    document = snapshot.to_dict()
    interfaces = document["interfaces"]
    assert isinstance(interfaces, dict)
    first_link, second_link = plan.links
    first_left = interfaces[f"{first_link.left.node}:{first_link.left.interface}"]
    first_right = interfaces[f"{first_link.right.node}:{first_link.right.interface}"]
    second_left = interfaces[f"{second_link.left.node}:{second_link.left.interface}"]
    second_right = interfaces[f"{second_link.right.node}:{second_link.right.interface}"]
    assert all(
        isinstance(value, dict) for value in (first_left, first_right, second_left, second_right)
    )
    if invalidity == "empty":
        first_left["link_id"] = ""  # type: ignore[index]
    elif invalidity == "partial_missing":
        first_left.pop("link_id")  # type: ignore[union-attr]
    elif invalidity == "mismatch":
        first_right["link_id"] = "different"  # type: ignore[index]
    else:
        first_id = first_left["link_id"]  # type: ignore[index]
        second_left["link_id"] = first_id  # type: ignore[index]
        second_right["link_id"] = first_id  # type: ignore[index]
    store.save(StateSnapshot.from_dict(document))
    backend.calls.clear()

    with pytest.raises(NslabError) as caught:
        service.deploy(plan, manifest)

    assert caught.value.code == "STATE_INVALID"
    assert backend.calls == []


@pytest.mark.parametrize(
    "invalidity",
    ["partial_missing", "mixed_value", "pair_missing", "complete_pair"],
)
def test_mixed_transitional_snapshot_link_id_states_are_invalid(
    manifest: Manifest,
    plan: TopologyPlan,
    invalidity: str,
) -> None:
    snapshot = _snapshot(plan, manifest, status="deploying")
    document = snapshot.to_dict()
    interfaces = document["interfaces"]
    assert isinstance(interfaces, dict)
    link = plan.links[0]
    left = interfaces[f"{link.left.node}:{link.left.interface}"]
    right = interfaces[f"{link.right.node}:{link.right.interface}"]
    assert isinstance(left, dict)
    assert isinstance(right, dict)
    if invalidity == "partial_missing":
        left.pop("link_id")
    elif invalidity == "mixed_value":
        left["link_id"] = "partial-link-id"
    elif invalidity == "pair_missing":
        left.pop("link_id")
        right.pop("link_id")
    else:
        left["link_id"] = "complete-link-id"
        right["link_id"] = "complete-link-id"

    with pytest.raises(NslabError) as caught:
        LifecycleService._plan_from_snapshot(StateSnapshot.from_dict(document))

    assert caught.value.code == "STATE_INVALID"


@pytest.mark.parametrize("status", ["deploying", "destroying"])
def test_transient_or_conflicting_state_blocks_before_inventory(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    status: str,
) -> None:
    store = StateStore(tmp_path)
    store.save(_snapshot(plan, manifest, status=status))
    backend = FakeNetworkBackend()

    with pytest.raises(NslabError) as interrupted:
        _service(backend, store).deploy(plan, manifest)

    assert interrupted.value.code == "DEPLOYMENT_INTERRUPTED"
    assert backend.calls == []

    store.save(_snapshot(plan, manifest, fingerprint="f" * 64))
    with pytest.raises(NslabError) as conflict:
        _service(backend, store).deploy(plan, manifest)

    assert conflict.value.code == "DEPLOYMENT_CONFLICT"
    assert backend.calls == []


def test_second_veth_failure_rolls_back_namespaces_in_reverse_order(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
) -> None:
    store = StateStore(tmp_path)
    backend = FakeNetworkBackend(fail_on_call=7)

    with pytest.raises(NslabError) as caught:
        _service(backend, store).deploy(plan, manifest)

    namespaces = [node.namespace for node in plan.nodes.values()]
    assert caught.value.code == "BACKEND_FAILURE"
    assert caught.value.details["rollback"] == [
        {"namespace": namespace, "result": "deleted"} for namespace in reversed(namespaces)
    ]
    assert backend.calls[-4:] == [
        *(("delete_namespace", namespace) for namespace in reversed(namespaces)),
        ("inventory", plan.name),
    ]
    assert backend.namespaces == {}
    assert store.load(plan.name) is None


def test_rollback_retains_state_when_exact_temporary_root_artifact_remains(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
) -> None:
    store = StateStore(tmp_path)
    backend = _RootArtifactRollbackBackend()

    with pytest.raises(NslabError) as caught:
        _service(backend, store).deploy(plan, manifest)

    assert caught.value.code == "BACKEND_FAILURE"
    assert caught.value.details["rollback_complete"] is False
    snapshot = store.load(plan.name)
    assert snapshot is not None
    assert snapshot.status == "deploying"
    assert backend.root_interfaces


def test_lifecycle_result_is_frozen() -> None:
    result = LifecycleResult(
        action="deploy",
        name="bridge-fdb",
        changed=True,
        status="deployed",
        message="deployed topology: bridge-fdb",
    )

    with pytest.raises(FrozenInstanceError):
        result.changed = False  # type: ignore[misc]


def test_rollback_failure_continues_cleanup_and_retains_deploying_state(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
) -> None:
    store = StateStore(tmp_path)
    backend = _ControlledFailureBackend()
    namespaces = [node.namespace for node in plan.nodes.values()]
    backend.fail_second_veth = True
    backend.fail_delete.add(namespaces[1])

    with pytest.raises(NslabError) as caught:
        _service(backend, store).deploy(plan, manifest)

    rollback = caught.value.details["rollback"]
    assert isinstance(rollback, list)
    assert caught.value.code == "BACKEND_FAILURE"
    assert [entry["namespace"] for entry in rollback] == list(reversed(namespaces))
    assert caught.value.details["rollback_complete"] is False
    assert backend.calls[-4:] == [
        *(("delete_namespace", namespace) for namespace in reversed(namespaces)),
        ("inventory", plan.name),
    ]
    snapshot = store.load(plan.name)
    assert snapshot is not None
    assert snapshot.status == "deploying"
    assert set(backend.namespaces) == {namespaces[1]}


def test_destroy_persists_destroying_before_reverse_delete_and_repeats_with_plan(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
) -> None:
    store = StateStore(tmp_path)
    backend = _DestroyStateCheckingBackend(store, plan.name)
    service = _service(backend, store)
    service.deploy(plan, manifest)
    state_plan = _persisted_plan(store, plan.name)
    backend.calls.clear()

    result = service.destroy(plan, plan.name)

    namespaces = [node.namespace for node in state_plan.nodes.values()]
    assert backend.destroying_seen
    assert result == LifecycleResult(
        action="destroy",
        name=plan.name,
        changed=True,
        status="absent",
        message=f"destroyed topology: {plan.name}",
    )
    assert backend.calls == [
        *(("delete_namespace", namespace) for namespace in reversed(namespaces)),
        ("inventory", plan.name),
    ]
    assert store.load(plan.name) is None

    backend.calls.clear()
    repeated = service.destroy(plan, plan.name)
    assert repeated.changed is False
    assert repeated.status == "absent"
    assert backend.calls == [("inventory", plan.name)]

    backend.calls.clear()
    with pytest.raises(NslabError) as unknown:
        service.destroy(None, plan.name)
    assert unknown.value.code == "OWNERSHIP_UNKNOWN"
    assert backend.calls == []


def test_destroy_does_not_remove_state_when_temporary_root_artifact_remains(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
) -> None:
    store = StateStore(tmp_path)
    backend = FakeNetworkBackend()
    service = _service(backend, store)
    service.deploy(plan, manifest)
    temporary_name = plan.links[0].left.temporary_name
    backend.root_interfaces[temporary_name] = InterfaceInventory(
        name=temporary_name,
        kind="veth",
        ifindex=901,
        master=None,
        mtu=plan.links[0].mtu,
        up=False,
        link_id="destroy-leftover",
    )

    with pytest.raises(NslabError) as caught:
        service.destroy(plan, plan.name)

    assert caught.value.code == "DESTROY_INCOMPLETE"
    snapshot = store.load(plan.name)
    assert snapshot is not None
    assert snapshot.status == "destroying"
    assert backend.root_interfaces == {temporary_name: backend.root_interfaces[temporary_name]}


def test_destroy_without_state_never_deletes_unowned_live_resources(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
) -> None:
    store = StateStore(tmp_path)
    backend = FakeNetworkBackend()
    first_node = next(iter(plan.nodes.values()))
    backend.create_namespace(first_node)
    backend.calls.clear()

    with pytest.raises(NslabError) as caught:
        _service(backend, store).destroy(plan, plan.name)

    assert caught.value.code == "OWNERSHIP_UNKNOWN"
    assert backend.calls == [("inventory", plan.name)]
    assert first_node.namespace in backend.namespaces

    other_plan = compile_plan(manifest, name_override="other")
    backend.calls.clear()
    with pytest.raises(NslabError) as mismatch:
        _service(backend, store).destroy(other_plan, plan.name)
    assert mismatch.value.code == "PLAN_NAME_MISMATCH"
    assert backend.calls == []


def test_destroy_failure_continues_and_retains_destroying_state(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
) -> None:
    store = StateStore(tmp_path)
    backend = _ControlledFailureBackend()
    service = _service(backend, store)
    service.deploy(plan, manifest)
    state_plan = _persisted_plan(store, plan.name)
    namespaces = [node.namespace for node in state_plan.nodes.values()]
    backend.fail_delete.add(namespaces[-1])
    backend.calls.clear()

    with pytest.raises(NslabError) as caught:
        service.destroy(plan, plan.name)

    assert caught.value.code == "BACKEND_FAILURE"
    assert backend.calls == [
        *(("delete_namespace", namespace) for namespace in reversed(namespaces)),
        ("inventory", plan.name),
    ]
    snapshot = store.load(plan.name)
    assert snapshot is not None
    assert snapshot.status == "destroying"
    assert set(backend.namespaces) == {namespaces[-1]}


def _replacement(manifest: Manifest) -> tuple[Manifest, TopologyPlan]:
    document = normalized_manifest(manifest)
    topology = document["topology"]
    assert isinstance(topology, dict)
    links = topology["links"]
    assert isinstance(links, list)
    first_link = links[0]
    assert isinstance(first_link, dict)
    first_link["mtu"] = 1600
    replacement = Manifest.model_validate(document)
    return replacement, compile_plan(replacement)


def test_redeploy_validates_new_plan_then_uses_one_non_reentrant_lock(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
) -> None:
    store = StateStore(tmp_path)
    backend = FakeNetworkBackend()
    _service(backend, store).deploy(plan, manifest)
    replacement_manifest, replacement_plan = _replacement(manifest)
    locks = _LockFactory()
    service = _service(backend, store, locks)
    backend.calls.clear()

    with pytest.raises(NslabError) as mismatch:
        service.redeploy(replacement_plan, manifest)
    assert mismatch.value.code == "PLAN_MANIFEST_MISMATCH"
    assert backend.calls == []
    assert store.load(plan.name) is not None

    result = service.redeploy(replacement_plan, replacement_manifest)

    assert result.action == "redeploy"
    assert result.changed is True
    assert locks.entries == [plan.name, plan.name]
    snapshot = store.load(plan.name)
    assert snapshot is not None
    assert snapshot.status == "deployed"
    assert snapshot.fingerprint == replacement_plan.fingerprint


def test_redeploy_new_deploy_failure_leaves_old_and_new_topologies_absent(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
) -> None:
    store = StateStore(tmp_path)
    backend = _ControlledFailureBackend()
    _service(backend, store).deploy(plan, manifest)
    replacement_manifest, replacement_plan = _replacement(manifest)
    locks = _LockFactory()
    backend.fail_create_namespace = True
    backend.calls.clear()

    with pytest.raises(NslabError) as caught:
        _service(backend, store, locks).redeploy(
            replacement_plan,
            replacement_manifest,
        )

    assert caught.value.code == "BACKEND_FAILURE"
    assert locks.entries == [plan.name]
    assert not any(operation == "create_bridge" for operation, _ in backend.calls)
    assert backend.namespaces == {}
    assert store.load(plan.name) is None


@pytest.mark.parametrize(
    ("code", "committed"),
    [
        ("STATE_COMMIT_OUTCOME_UNKNOWN", False),
        ("STATE_DURABILITY_UNCERTAIN", True),
    ],
)
def test_initial_deploy_uncertainty_reconciles_without_network_mutation(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    code: str,
    committed: bool,
) -> None:
    store = _InjectedStateStore(tmp_path)
    backend = FakeNetworkBackend()
    failure = _uncertainty(code, "save")
    store.arm_save("deploying", failure, commit=committed)

    with pytest.raises(NslabError) as caught:
        _service(backend, store).deploy(plan, manifest)

    assert caught.value is failure
    assert store.calls == [
        ("load", plan.name),
        ("save", "deploying"),
        ("load", plan.name),
    ]
    assert backend.calls == [
        ("inventory", plan.name),
        ("inventory", plan.name),
    ]
    assert not any(operation.startswith("create_") for operation, _ in backend.calls)
    persisted = StateStore(tmp_path).load(plan.name)
    assert (persisted is not None) is committed
    if persisted is not None:
        assert persisted.status == "deploying"
    assert "reconciliation" in failure.details


@pytest.mark.parametrize(
    "failure",
    [
        OperationCancelled("cancelled immediately after deploying save returned"),
        KeyboardInterrupt("interrupted immediately after deploying save returned"),
    ],
    ids=["operation-cancelled", "keyboard-interrupt"],
)
def test_raw_interrupt_after_initial_save_runs_load_inventory_gate(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    failure: Exception | KeyboardInterrupt,
) -> None:
    store = _InjectedStateStore(tmp_path)
    backend = FakeNetworkBackend()
    store.arm_save("deploying", failure, commit=True)

    with pytest.raises(type(failure)) as caught:
        _service(backend, store).deploy(plan, manifest)

    assert caught.value is failure
    assert store.calls[-2:] == [
        ("save", "deploying"),
        ("load", plan.name),
    ]
    assert backend.calls == [
        ("inventory", plan.name),
        ("inventory", plan.name),
    ]
    assert not any(operation.startswith("create_") for operation, _ in backend.calls)
    persisted = StateStore(tmp_path).load(plan.name)
    assert persisted is not None
    assert persisted.status == "deploying"


@pytest.mark.parametrize(
    ("code", "committed"),
    [
        ("STATE_COMMIT_OUTCOME_UNKNOWN", False),
        ("STATE_COMMIT_OUTCOME_UNKNOWN", True),
        ("STATE_DURABILITY_UNCERTAIN", True),
    ],
)
def test_initial_destroy_uncertainty_reconciles_without_namespace_delete(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    code: str,
    committed: bool,
) -> None:
    store = _InjectedStateStore(tmp_path)
    backend = FakeNetworkBackend()
    service = _service(backend, store)
    service.deploy(plan, manifest)
    failure = _uncertainty(code, "save")
    store.arm_save("destroying", failure, commit=committed)
    store.calls.clear()
    backend.calls.clear()

    with pytest.raises(NslabError) as caught:
        service.destroy(plan, plan.name)

    assert caught.value is failure
    assert store.calls == [
        ("load", plan.name),
        ("save", "destroying"),
        ("load", plan.name),
    ]
    assert backend.calls == [("inventory", plan.name)]
    assert not any(operation == "delete_namespace" for operation, _ in backend.calls)
    persisted = StateStore(tmp_path).load(plan.name)
    assert persisted is not None
    assert persisted.status == ("destroying" if committed else "deployed")


@pytest.mark.parametrize(
    "failure",
    [
        OperationCancelled("cancelled immediately after destroying save returned"),
        KeyboardInterrupt("interrupted immediately after destroying save returned"),
    ],
    ids=["operation-cancelled", "keyboard-interrupt"],
)
def test_raw_interrupt_after_destroying_save_runs_gate_without_namespace_delete(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    failure: Exception | KeyboardInterrupt,
) -> None:
    store = _InjectedStateStore(tmp_path)
    backend = FakeNetworkBackend()
    service = _service(backend, store)
    service.deploy(plan, manifest)
    store.arm_save("destroying", failure, commit=True)
    store.calls.clear()
    backend.calls.clear()

    with pytest.raises(type(failure)) as caught:
        service.destroy(plan, plan.name)

    assert caught.value is failure
    assert store.calls == [
        ("load", plan.name),
        ("save", "destroying"),
        ("load", plan.name),
    ]
    assert backend.calls == [("inventory", plan.name)]
    assert not any(operation == "delete_namespace" for operation, _ in backend.calls)
    persisted = StateStore(tmp_path).load(plan.name)
    assert persisted is not None
    assert persisted.status == "destroying"


@pytest.mark.parametrize(
    ("code", "committed"),
    [
        ("STATE_COMMIT_OUTCOME_UNKNOWN", False),
        ("STATE_COMMIT_OUTCOME_UNKNOWN", True),
        ("STATE_DURABILITY_UNCERTAIN", True),
    ],
)
def test_final_deployed_save_uncertainty_reconciles_before_rollback_decision(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    code: str,
    committed: bool,
) -> None:
    store = _InjectedStateStore(tmp_path)
    backend = FakeNetworkBackend()
    failure = _uncertainty(code, "save")
    original_message = failure.message
    store.arm_save("deployed", failure, commit=committed)

    with pytest.raises(NslabError) as caught:
        _service(backend, store).deploy(plan, manifest)

    assert caught.value is failure
    assert caught.value.code == code
    assert caught.value.message == original_message
    reconciliation_load = ("load", plan.name)
    assert store.calls.count(reconciliation_load) >= 2
    assert backend.calls.count(("inventory", plan.name)) >= 3
    assert "reconciliation" in failure.details
    assert json.dumps(failure.details, sort_keys=True)

    persisted = StateStore(tmp_path).load(plan.name)
    if committed:
        assert persisted is not None
        assert persisted.status == "deployed"
        assert set(backend.namespaces) == {node.namespace for node in plan.nodes.values()}
        assert not any(operation == "delete_namespace" for operation, _ in backend.calls)
        assert "rollback" not in failure.details
    else:
        assert persisted is None
        assert backend.namespaces == {}
        assert failure.details["rollback_complete"] is True
        assert len(failure.details["rollback"]) == len(plan.nodes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "failure",
    [
        OperationCancelled("cancelled immediately after deployed save returned"),
        KeyboardInterrupt("interrupted immediately after deployed save returned"),
    ],
    ids=["operation-cancelled", "keyboard-interrupt"],
)
def test_raw_interrupt_after_final_deployed_save_never_rolls_back_verified_state(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    failure: Exception | KeyboardInterrupt,
) -> None:
    store = _InjectedStateStore(tmp_path)
    backend = FakeNetworkBackend()
    store.arm_save("deployed", failure, commit=True)

    with pytest.raises(type(failure)) as caught:
        _service(backend, store).deploy(plan, manifest)

    assert caught.value is failure
    assert store.calls[-1] == ("load", plan.name)
    assert backend.calls[-1] == ("inventory", plan.name)
    assert not any(operation == "delete_namespace" for operation, _ in backend.calls)
    persisted = StateStore(tmp_path).load(plan.name)
    assert persisted is not None
    assert persisted.status == "deployed"
    assert set(backend.namespaces) == {node.namespace for node in plan.nodes.values()}


@pytest.mark.parametrize(
    ("code", "committed", "resolved"),
    [
        ("STATE_COMMIT_OUTCOME_UNKNOWN", False, True),
        ("STATE_COMMIT_OUTCOME_UNKNOWN", True, False),
        ("STATE_DURABILITY_UNCERTAIN", True, False),
    ],
)
def test_final_delete_uncertainty_reconciles_without_redeleting_namespaces(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    code: str,
    committed: bool,
    resolved: bool,
) -> None:
    store = _InjectedStateStore(tmp_path)
    backend = FakeNetworkBackend()
    service = _service(backend, store)
    service.deploy(plan, manifest)
    state_plan = _persisted_plan(store, plan.name)
    namespace_count = len(state_plan.nodes)
    failure = _uncertainty(code, "delete")
    store.arm_delete(failure, commit=committed)
    store.calls.clear()
    backend.calls.clear()

    if resolved:
        result = service.destroy(plan, plan.name)
        assert result.changed is True
        assert store.calls.count(("delete", plan.name)) == 2
    else:
        with pytest.raises(NslabError) as caught:
            service.destroy(plan, plan.name)
        assert caught.value is failure
        assert "reconciliation" in failure.details
        assert store.calls.count(("delete", plan.name)) == 1

    assert sum(operation == "delete_namespace" for operation, _ in backend.calls) == namespace_count
    assert backend.calls[-1] == ("inventory", plan.name)
    assert store.load(plan.name) is None


@pytest.mark.parametrize(
    "failure",
    [
        OperationCancelled("cancelled immediately after state delete returned"),
        KeyboardInterrupt("interrupted immediately after state delete returned"),
    ],
    ids=["operation-cancelled", "keyboard-interrupt"],
)
def test_raw_interrupt_after_final_delete_runs_gate_and_always_propagates(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    failure: Exception | KeyboardInterrupt,
) -> None:
    store = _InjectedStateStore(tmp_path)
    backend = FakeNetworkBackend()
    service = _service(backend, store)
    service.deploy(plan, manifest)
    state_plan = _persisted_plan(store, plan.name)
    store.arm_delete(failure, commit=True)
    store.calls.clear()
    backend.calls.clear()

    with pytest.raises(type(failure)) as caught:
        service.destroy(plan, plan.name)

    assert caught.value is failure
    assert store.calls.count(("delete", plan.name)) == 1
    assert store.calls[-1] == ("load", plan.name)
    assert backend.calls[-1] == ("inventory", plan.name)
    assert sum(operation == "delete_namespace" for operation, _ in backend.calls) == len(
        state_plan.nodes
    )
    assert StateStore(tmp_path).load(plan.name) is None


def test_real_state_store_replace_oserror_is_reconciled_before_mutation(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path)
    backend = FakeNetworkBackend()
    failure = OSError("injected replace failure")

    def fail_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise failure

    monkeypatch.setattr("nslab.state.os.replace", fail_replace)

    with pytest.raises(NslabError) as caught:
        _service(backend, store).deploy(plan, manifest)

    assert caught.value.code == "STATE_COMMIT_OUTCOME_UNKNOWN"
    assert caught.value.__cause__ is failure
    assert "reconciliation" in caught.value.details
    assert backend.calls == [
        ("inventory", plan.name),
        ("inventory", plan.name),
    ]
    assert not any(operation.startswith("create_") for operation, _ in backend.calls)
    assert store.load(plan.name) is None


def test_real_state_store_unlink_oserror_is_retried_only_after_reconciliation(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path)
    backend = FakeNetworkBackend()
    service = _service(backend, store)
    service.deploy(plan, manifest)
    state_plan = _persisted_plan(store, plan.name)
    real_unlink = Path.unlink
    unlink_calls = 0

    def fail_first_unlink(path: Path, missing_ok: bool = False) -> None:
        nonlocal unlink_calls
        unlink_calls += 1
        if unlink_calls == 1:
            raise OSError("injected unlink failure")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_first_unlink)
    backend.calls.clear()

    result = service.destroy(plan, plan.name)

    assert result.changed is True
    assert unlink_calls == 2
    assert sum(operation == "delete_namespace" for operation, _ in backend.calls) == len(
        state_plan.nodes
    )
    assert backend.calls[-1] == ("inventory", plan.name)
    assert store.load(plan.name) is None


@pytest.mark.parametrize(
    ("code", "committed"),
    [
        ("STATE_COMMIT_OUTCOME_UNKNOWN", False),
        ("STATE_DURABILITY_UNCERTAIN", True),
    ],
)
def test_rollback_state_delete_uncertainty_uses_load_inventory_gate(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    code: str,
    committed: bool,
) -> None:
    store = _InjectedStateStore(tmp_path)
    backend = FakeNetworkBackend(fail_on_call=7)
    state_failure = _uncertainty(code, "delete")
    store.arm_delete(state_failure, commit=committed)

    with pytest.raises(NslabError) as caught:
        _service(backend, store).deploy(plan, manifest)

    assert caught.value.code == "BACKEND_FAILURE"
    assert caught.value is not state_failure
    assert caught.value.details["rollback_complete"] is not committed
    reconciliation = caught.value.details["rollback_state_reconciliation"]
    assert isinstance(reconciliation, dict)
    assert store.calls.count(("delete", plan.name)) == (1 if committed else 2)
    assert store.calls.count(("load", plan.name)) >= 2
    assert backend.calls[-1] == ("inventory", plan.name)
    assert backend.calls.count(("inventory", plan.name)) >= 3
    assert StateStore(tmp_path).load(plan.name) is None


def test_reconciliation_failures_do_not_mask_original_or_guess_cleanup(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
) -> None:
    load_failure = RuntimeError("injected reconciliation load failure")
    store = _BrokenReconciliationStore(tmp_path, load_failure)
    backend = FakeNetworkBackend(fail_on_call=2)
    original = _uncertainty("STATE_COMMIT_OUTCOME_UNKNOWN", "save")
    store.arm_save("deploying", original, commit=True)

    with pytest.raises(NslabError) as caught:
        _service(backend, store).deploy(plan, manifest)

    assert caught.value is original
    reconciliation = original.details["reconciliation"]
    assert isinstance(reconciliation, dict)
    assert reconciliation["state_error"]["type"] == "RuntimeError"  # type: ignore[index]
    assert reconciliation["inventory_error"]["code"] == "BACKEND_FAILURE"  # type: ignore[index]
    assert backend.calls == [
        ("inventory", plan.name),
        ("inventory", plan.name),
    ]
    assert not any(
        operation in {"create_namespace", "delete_namespace"} for operation, _ in backend.calls
    )
    persisted = StateStore(tmp_path).load(plan.name)
    assert persisted is not None
    assert persisted.status == "deploying"


@pytest.mark.parametrize("failure_point", ["load", "inventory"])
def test_final_save_reconciliation_failure_never_guesses_rollback(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    failure_point: str,
) -> None:
    reconciliation_failure = RuntimeError(
        f"injected final-save reconciliation {failure_point} failure"
    )
    if failure_point == "load":
        store: _InjectedStateStore = _BrokenReconciliationStore(
            tmp_path,
            reconciliation_failure,
        )
        backend = FakeNetworkBackend()
    else:
        store = _InjectedStateStore(tmp_path)
        backend = _InventoryFailureBackend(fail_on_inventory=3)
    original = _uncertainty("STATE_COMMIT_OUTCOME_UNKNOWN", "save")
    store.arm_save("deployed", original, commit=True)

    with pytest.raises(NslabError) as caught:
        _service(backend, store).deploy(plan, manifest)

    assert caught.value is original
    reconciliation = original.details["reconciliation"]
    assert isinstance(reconciliation, dict)
    diagnostic_key = "state_error" if failure_point == "load" else "inventory_error"
    assert diagnostic_key in reconciliation
    assert not any(operation == "delete_namespace" for operation, _ in backend.calls)
    persisted = StateStore(tmp_path).load(plan.name)
    assert persisted is not None
    assert persisted.status == "deployed"
    assert set(backend.namespaces) == {node.namespace for node in plan.nodes.values()}


@pytest.mark.parametrize(
    "failure",
    [
        OperationCancelled("cancelled during first namespace delete"),
        KeyboardInterrupt("interrupted during first namespace delete"),
    ],
    ids=["operation-cancelled", "keyboard-interrupt"],
)
def test_destroy_interrupt_stops_further_namespace_deletes(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    failure: OperationCancelled | KeyboardInterrupt,
) -> None:
    store = StateStore(tmp_path)
    backend = _InterruptingDeleteBackend(failure)
    service = _service(backend, store)
    service.deploy(plan, manifest)
    backend.interrupt_deletes = True
    backend.calls.clear()

    with pytest.raises(type(failure)) as caught:
        service.destroy(plan, plan.name)

    assert caught.value is failure
    assert sum(operation == "delete_namespace" for operation, _ in backend.calls) == 1
    assert backend.calls[-1] == ("inventory", plan.name)
    persisted = store.load(plan.name)
    assert persisted is not None
    assert persisted.status == "destroying"


@pytest.mark.parametrize(
    "interrupt",
    [
        OperationCancelled("cancelled after final delete retry returned"),
        KeyboardInterrupt("interrupted after final delete retry returned"),
    ],
    ids=["operation-cancelled", "keyboard-interrupt"],
)
def test_raw_interrupt_from_final_delete_retry_is_reconciled_and_propagated(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    interrupt: OperationCancelled | KeyboardInterrupt,
) -> None:
    store = _InjectedStateStore(tmp_path)
    backend = FakeNetworkBackend()
    service = _service(backend, store)
    service.deploy(plan, manifest)
    state_plan = _persisted_plan(store, plan.name)
    original = _uncertainty("STATE_COMMIT_OUTCOME_UNKNOWN", "delete")
    store.arm_delete(original, commit=False)
    store.arm_delete(interrupt, commit=True)
    store.calls.clear()
    backend.calls.clear()

    with pytest.raises(type(interrupt)) as caught:
        service.destroy(plan, plan.name)

    assert caught.value is interrupt
    assert store.calls.count(("delete", plan.name)) == 2
    assert store.calls[-1] == ("load", plan.name)
    assert sum(operation == "delete_namespace" for operation, _ in backend.calls) == len(
        state_plan.nodes
    )
    assert backend.calls[-1] == ("inventory", plan.name)
    assert StateStore(tmp_path).load(plan.name) is None


def test_deploy_without_state_refuses_live_resources_without_write_or_delete(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
) -> None:
    store = _InjectedStateStore(tmp_path)
    backend = FakeNetworkBackend()
    first_node = next(iter(plan.nodes.values()))
    backend.create_namespace(first_node)
    backend.calls.clear()

    with pytest.raises(NslabError) as caught:
        _service(backend, store).deploy(plan, manifest)

    assert caught.value.code == "OWNERSHIP_UNKNOWN"
    assert store.calls == [("load", plan.name)]
    assert backend.calls == [("inventory", plan.name)]
    assert first_node.namespace in backend.namespaces
    assert StateStore(tmp_path).load(plan.name) is None


def test_same_fingerprint_live_drift_does_not_write_or_mutate(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
) -> None:
    store = _InjectedStateStore(tmp_path)
    backend = FakeNetworkBackend()
    service = _service(backend, store)
    service.deploy(plan, manifest)
    state_path = tmp_path / f"{plan.name}.json"
    state_bytes = state_path.read_bytes()
    backend.delete_namespace(next(iter(plan.nodes.values())).namespace)
    backend.calls.clear()
    store.calls.clear()

    with pytest.raises(NslabError) as caught:
        service.deploy(plan, manifest)

    assert caught.value.code == "DEPLOYMENT_DRIFT"
    assert store.calls == [("load", plan.name)]
    assert backend.calls == [("inventory", plan.name)]
    assert state_path.read_bytes() == state_bytes


def test_destroy_by_name_rebuilds_name_override_plan_from_stored_manifest(
    tmp_path: Path,
    manifest: Manifest,
) -> None:
    plan = compile_plan(manifest, name_override="demo")
    store = StateStore(tmp_path)
    backend = FakeNetworkBackend()
    service = _service(backend, store)
    service.deploy(plan, manifest)
    assert all("demo" in namespace for namespace in backend.namespaces)
    backend.calls.clear()

    result = service.destroy(None, "demo")

    assert result.changed is True
    assert result.name == "demo"
    assert backend.namespaces == {}
    assert store.load("demo") is None
    assert all(
        "demo" in resource
        for operation, resource in backend.calls
        if operation == "delete_namespace"
    )


@pytest.mark.parametrize("status", ["deploying", "destroying"])
@pytest.mark.parametrize("operation", ["destroy", "redeploy"])
def test_transient_state_blocks_destroy_and_redeploy_without_network_mutation(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    status: str,
    operation: str,
) -> None:
    store = StateStore(tmp_path)
    store.save(_snapshot(plan, manifest, status=status))
    backend = FakeNetworkBackend()
    service = _service(backend, store)
    state_path = tmp_path / f"{plan.name}.json"
    state_bytes = state_path.read_bytes()

    with pytest.raises(NslabError) as caught:
        if operation == "destroy":
            service.destroy(None, plan.name)
        else:
            service.redeploy(plan, manifest)

    assert caught.value.code == "DEPLOYMENT_INTERRUPTED"
    assert backend.calls == []
    assert state_path.read_bytes() == state_bytes


@pytest.mark.parametrize(
    "failure",
    [
        _uncertainty("STATE_COMMIT_OUTCOME_UNKNOWN", "save"),
        OperationCancelled("cancelled after drifted deployed save returned"),
        KeyboardInterrupt("interrupted after drifted deployed save returned"),
    ],
    ids=["structured-uncertainty", "operation-cancelled", "keyboard-interrupt"],
)
def test_committed_deployed_snapshot_blocks_rollback_even_when_inventory_drifts(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    failure: Exception | KeyboardInterrupt,
) -> None:
    store = _InjectedStateStore(tmp_path)
    backend = _DriftingReconciliationBackend()
    store.arm_save("deployed", failure, commit=True)

    with pytest.raises(type(failure)) as caught:
        _service(backend, store).deploy(plan, manifest)

    assert caught.value is failure
    assert backend.inventory_calls == 3
    assert not any(operation == "delete_namespace" for operation, _ in backend.calls)
    assert set(backend.namespaces) == {node.namespace for node in plan.nodes.values()}
    snapshot = StateStore(tmp_path).load(plan.name)
    assert snapshot is not None
    assert snapshot.name == plan.name
    assert snapshot.fingerprint == plan.fingerprint
    assert snapshot.status == "deployed"


@pytest.mark.parametrize("operation", ["deploy", "destroy"])
def test_loaded_snapshot_name_must_match_requested_state_path_before_mutation(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    operation: str,
) -> None:
    store = StateStore(tmp_path)
    other_plan = compile_plan(manifest, name_override="other")
    store.save(_snapshot(other_plan, manifest))
    other_path = tmp_path / "other.json"
    requested_path = tmp_path / f"{plan.name}.json"
    other_path.replace(requested_path)
    before = requested_path.read_bytes()
    backend = FakeNetworkBackend()
    service = _service(backend, store)

    with pytest.raises(NslabError) as caught:
        if operation == "deploy":
            service.deploy(plan, manifest)
        else:
            service.destroy(None, plan.name)

    assert caught.value.code == "STATE_INVALID"
    assert caught.value.details == {
        "requested_name": plan.name,
        "snapshot_name": other_plan.name,
    }
    assert backend.calls == []
    assert requested_path.read_bytes() == before
    assert not other_path.exists()


@pytest.mark.parametrize(
    ("retry_code", "retry_committed"),
    [
        ("STATE_COMMIT_OUTCOME_UNKNOWN", False),
        ("STATE_COMMIT_OUTCOME_UNKNOWN", True),
        ("STATE_DURABILITY_UNCERTAIN", True),
    ],
)
def test_rollback_delete_retry_reconciles_second_structured_uncertainty(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    retry_code: str,
    retry_committed: bool,
) -> None:
    store = _InjectedStateStore(tmp_path)
    backend = FakeNetworkBackend(fail_on_call=7)
    first = _uncertainty("STATE_COMMIT_OUTCOME_UNKNOWN", "delete")
    retry = _uncertainty(retry_code, "delete")
    store.arm_delete(first, commit=False)
    store.arm_delete(retry, commit=retry_committed)

    with pytest.raises(NslabError) as caught:
        _service(backend, store).deploy(plan, manifest)

    assert caught.value.code == "BACKEND_FAILURE"
    assert caught.value.details["rollback_complete"] is False
    retry_error = caught.value.details["rollback_state_delete_retry"]
    assert isinstance(retry_error, dict)
    assert retry_error["code"] == retry_code
    retry_reconciliation = caught.value.details["rollback_state_delete_retry_reconciliation"]
    assert isinstance(retry_reconciliation, dict)
    assert (retry_reconciliation["state"] is None) is retry_committed
    retry_inventory = retry_reconciliation["inventory"]
    assert isinstance(retry_inventory, dict)
    assert retry_inventory["absent"] is True
    assert store.calls.count(("delete", plan.name)) == 2
    persisted = StateStore(tmp_path).load(plan.name)
    assert (persisted is None) is retry_committed
    if persisted is not None:
        assert persisted.status == "deploying"


@pytest.mark.parametrize(
    "interrupt",
    [
        OperationCancelled("cancelled after rollback delete retry returned"),
        KeyboardInterrupt("interrupted after rollback delete retry returned"),
    ],
    ids=["operation-cancelled", "keyboard-interrupt"],
)
def test_rollback_delete_retry_raw_interrupt_reconciles_and_propagates(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    interrupt: OperationCancelled | KeyboardInterrupt,
) -> None:
    store = _InjectedStateStore(tmp_path)
    backend = FakeNetworkBackend(fail_on_call=7)
    first = _uncertainty("STATE_COMMIT_OUTCOME_UNKNOWN", "delete")
    store.arm_delete(first, commit=False)
    store.arm_delete(interrupt, commit=True)

    with pytest.raises(type(interrupt)) as caught:
        _service(backend, store).deploy(plan, manifest)

    assert caught.value is interrupt
    assert store.calls.count(("delete", plan.name)) == 2
    assert store.calls[-1] == ("load", plan.name)
    assert backend.calls[-1] == ("inventory", plan.name)
    assert StateStore(tmp_path).load(plan.name) is None
    if isinstance(interrupt, NslabError):
        assert "previous_operation_error" in interrupt.details
        assert "reconciliation" in interrupt.details
    else:
        assert any("previous_operation_error" in note for note in interrupt.__notes__)
        assert any("reconciliation" in note for note in interrupt.__notes__)


@pytest.mark.parametrize(
    ("retry_code", "retry_committed"),
    [
        ("STATE_COMMIT_OUTCOME_UNKNOWN", False),
        ("STATE_COMMIT_OUTCOME_UNKNOWN", True),
        ("STATE_DURABILITY_UNCERTAIN", True),
    ],
)
def test_final_destroy_reconciles_second_structured_delete_uncertainty(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    retry_code: str,
    retry_committed: bool,
) -> None:
    store = _InjectedStateStore(tmp_path)
    backend = FakeNetworkBackend()
    service = _service(backend, store)
    service.deploy(plan, manifest)
    first = _uncertainty("STATE_COMMIT_OUTCOME_UNKNOWN", "delete")
    retry = _uncertainty(retry_code, "delete")
    store.arm_delete(first, commit=False)
    store.arm_delete(retry, commit=retry_committed)
    store.calls.clear()
    backend.calls.clear()

    with pytest.raises(NslabError) as caught:
        service.destroy(plan, plan.name)

    assert caught.value is first
    retry_error = first.details["state_delete_retry"]
    assert isinstance(retry_error, dict)
    assert retry_error["code"] == retry_code
    retry_reconciliation = first.details["state_delete_retry_reconciliation"]
    assert isinstance(retry_reconciliation, dict)
    assert (retry_reconciliation["state"] is None) is retry_committed
    retry_inventory = retry_reconciliation["inventory"]
    assert isinstance(retry_inventory, dict)
    assert retry_inventory["absent"] is True
    assert store.calls.count(("delete", plan.name)) == 2
    persisted = StateStore(tmp_path).load(plan.name)
    assert (persisted is None) is retry_committed
    if persisted is not None:
        assert persisted.status == "destroying"


@pytest.mark.parametrize("field", ["node", "kind", "namespace"])
@pytest.mark.parametrize("operation", ["deploy", "destroy"])
def test_malformed_absent_inventory_never_passes_ownership_gate(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    field: str,
    operation: str,
) -> None:
    store = _InjectedStateStore(tmp_path)
    backend = _MalformedAbsentBackend(field, malform_on_inventory=1)
    service = _service(backend, store)

    with pytest.raises(NslabError) as caught:
        if operation == "deploy":
            service.deploy(plan, manifest)
        else:
            service.destroy(plan, plan.name)

    assert caught.value.code == "OWNERSHIP_UNKNOWN"
    assert store.calls == [("load", plan.name)]
    assert backend.calls == [("inventory", plan.name)]
    assert backend.namespaces == {}
    assert StateStore(tmp_path).load(plan.name) is None


@pytest.mark.parametrize("field", ["node", "kind", "namespace"])
def test_malformed_absent_inventory_blocks_final_state_delete_retry(
    tmp_path: Path,
    manifest: Manifest,
    plan: TopologyPlan,
    field: str,
) -> None:
    store = _InjectedStateStore(tmp_path)
    backend = _MalformedAbsentBackend(field, malform_on_inventory=99)
    service = _service(backend, store)
    service.deploy(plan, manifest)
    backend.inventory_calls = 0
    backend.malform_on_inventory = 2
    failure = _uncertainty("STATE_COMMIT_OUTCOME_UNKNOWN", "delete")
    store.arm_delete(failure, commit=False)
    store.calls.clear()
    backend.calls.clear()

    with pytest.raises(NslabError) as caught:
        service.destroy(plan, plan.name)

    assert caught.value is failure
    assert store.calls.count(("delete", plan.name)) == 1
    persisted = StateStore(tmp_path).load(plan.name)
    assert persisted is not None
    assert persisted.status == "destroying"
