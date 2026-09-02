from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Never, cast

from nslab.backend.base import (
    LiveInventory,
    NetworkBackend,
    inventory_matches_plan,
    recorded_link_ids_match_inventory,
)
from nslab.errors import NslabError, OperationCancelled
from nslab.manifest import Manifest, manifest_fingerprint, normalized_manifest
from nslab.planner import TopologyPlan, VlanDevicePlan, VrfDevicePlan, compile_plan
from nslab.snapshot import validate_snapshot
from nslab.state import DeploymentLock, SnapshotStatus, StateSnapshot, StateStore

type LockFactory = Callable[[str], AbstractContextManager[object]]
type Clock = Callable[[], datetime]

_UNCERTAIN_STATE_CODES = frozenset(
    {
        "STATE_COMMIT_OUTCOME_UNKNOWN",
        "STATE_DURABILITY_UNCERTAIN",
    }
)


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    action: str
    name: str
    changed: bool
    status: str
    message: str


@dataclass(frozen=True, slots=True)
class _Reconciliation:
    snapshot: StateSnapshot | None
    inventory: LiveInventory | None
    diagnostics: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _DeleteGate:
    reconciliation: _Reconciliation
    conclusive: bool
    live_absent: bool
    expected_snapshot: bool

    @property
    def retry_safe(self) -> bool:
        return self.conclusive and self.expected_snapshot and self.live_absent


def _default_clock() -> datetime:
    return datetime.now(tz=UTC)


def _error_payload(error: Exception | KeyboardInterrupt) -> dict[str, object]:
    if isinstance(error, NslabError):
        payload = _json_safe(error.as_dict())
        assert isinstance(payload, dict)
        return payload
    return {
        "type": type(error).__name__,
        "message": str(error),
    }


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _raise_original(error: Exception | KeyboardInterrupt) -> Never:
    raise error


class LifecycleService:
    """Transactional topology deployment using durable ownership snapshots."""

    def __init__(
        self,
        backend: NetworkBackend,
        state_store: StateStore,
        lock_factory: LockFactory | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.backend = backend
        self.state_store = state_store
        self.lock_factory = (
            lock_factory
            if lock_factory is not None
            else lambda name: DeploymentLock(self.state_store.root, name)
        )
        self.clock = clock if clock is not None else _default_clock

    def deploy(self, plan: TopologyPlan, manifest: Manifest) -> LifecycleResult:
        with self.lock_factory(plan.name):
            return self._deploy_locked(plan, manifest)

    def destroy(self, plan: TopologyPlan | None, name: str) -> LifecycleResult:
        with self.lock_factory(name):
            return self._destroy_locked(plan, name)

    def redeploy(self, plan: TopologyPlan, manifest: Manifest) -> LifecycleResult:
        with self.lock_factory(plan.name):
            self._validate_plan_manifest(plan, manifest)
            destroyed = self._destroy_locked(plan, plan.name)
            deployed = self._deploy_locked(plan, manifest, validated=True)
            return LifecycleResult(
                action="redeploy",
                name=plan.name,
                changed=destroyed.changed or deployed.changed,
                status=deployed.status,
                message=f"redeployed topology: {plan.name}",
            )

    def _deploy_locked(
        self,
        plan: TopologyPlan,
        manifest: Manifest,
        *,
        validated: bool = False,
    ) -> LifecycleResult:
        if not validated:
            self._validate_plan_manifest(plan, manifest)

        current = self.state_store.load(plan.name)
        if current is not None:
            self._require_snapshot_identity(current, plan.name)
            self._require_stable_snapshot(current)
            if current.fingerprint != plan.fingerprint:
                raise NslabError(
                    code="DEPLOYMENT_CONFLICT",
                    message=f"deployment already exists with different topology: {plan.name}",
                    details={
                        "name": plan.name,
                        "expected_fingerprint": plan.fingerprint,
                        "actual_fingerprint": current.fingerprint,
                    },
                )

            self._plan_from_snapshot(current)
            inventory = self.backend.inventory(plan)
            if (
                inventory_matches_plan(
                    plan,
                    inventory,
                )
                and recorded_link_ids_match_inventory(
                    plan,
                    inventory,
                    current.interfaces,
                )
                and self._routing_ready(plan)
            ):
                return LifecycleResult(
                    action="deploy",
                    name=plan.name,
                    changed=False,
                    status="deployed",
                    message=f"topology already deployed: {plan.name}",
                )
            raise NslabError(
                code="DEPLOYMENT_DRIFT",
                message=f"deployed topology does not match live resources: {plan.name}",
                details={"name": plan.name, "fingerprint": plan.fingerprint},
            )

        preflight = self.backend.inventory(plan)
        if not self._inventory_is_absent(plan, preflight):
            raise self._ownership_unknown(plan.name)

        created_at = self._timestamp()
        deploying = self._snapshot_for_plan(
            plan,
            manifest,
            created_at=created_at,
            status="deploying",
            inventory=None,
        )
        self._save_initial_transition(deploying, plan)

        created_namespaces: list[str] = []
        try:
            for node in plan.nodes.values():
                self.backend.create_namespace(node)
                created_namespaces.append(node.namespace)

            for node in plan.nodes.values():
                if node.kind == "bridge":
                    self.backend.create_bridge(node)

            for link in plan.links:
                self.backend.create_veth(link)

            for node in plan.nodes.values():
                self.backend.configure_node(node, plan)

            self._start_routing(plan)

            inventory = self.backend.inventory(plan)
            if not inventory_matches_plan(plan, inventory) or not self._routing_ready(plan):
                raise NslabError(
                    code="DEPLOYMENT_VERIFICATION_FAILED",
                    message=f"deployed topology failed live verification: {plan.name}",
                    details={"name": plan.name, "fingerprint": plan.fingerprint},
                )
        except Exception as error:
            self._rollback_and_attach(error, plan, created_namespaces, deploying)
            raise
        except KeyboardInterrupt as error:
            self._rollback_and_attach(error, plan, created_namespaces, deploying)
            raise

        deployed = self._snapshot_for_plan(
            plan,
            manifest,
            created_at=created_at,
            status="deployed",
            inventory=inventory,
        )
        try:
            self.state_store.save(deployed)
        except Exception as error:
            self._handle_final_deploy_save_failure(
                error,
                plan,
                created_namespaces,
                deploying,
            )
            raise
        except KeyboardInterrupt as error:
            self._handle_final_deploy_save_failure(
                error,
                plan,
                created_namespaces,
                deploying,
            )
            raise

        return LifecycleResult(
            action="deploy",
            name=plan.name,
            changed=True,
            status="deployed",
            message=f"deployed topology: {plan.name}",
        )

    def _destroy_locked(
        self,
        plan: TopologyPlan | None,
        name: str,
    ) -> LifecycleResult:
        if plan is not None and plan.name != name:
            raise NslabError(
                code="PLAN_NAME_MISMATCH",
                message=f"destroy plan name does not match requested deployment: {name}",
                details={"name": name, "plan_name": plan.name},
            )

        current = self.state_store.load(name)
        if current is None:
            if plan is None:
                raise self._ownership_unknown(name)
            preflight_inventory = self.backend.inventory(plan)
            if not self._inventory_is_absent(plan, preflight_inventory):
                raise self._ownership_unknown(name)
            # Routing runtime files/processes are outside the network inventory.
            # Recover them even when the durable snapshot was removed after an
            # interrupted cleanup and the namespaces are already gone.
            self._stop_routing(plan)
            return LifecycleResult(
                action="destroy",
                name=name,
                changed=False,
                status="absent",
                message=f"topology already absent: {name}",
            )

        self._require_snapshot_identity(current, name)
        self._require_stable_snapshot(current)
        state_plan = self._plan_from_snapshot(current)
        destroying = replace(current, status="destroying")
        self._save_initial_transition(destroying, state_plan)

        cleanup: list[dict[str, object]] = []
        first_error: Exception | KeyboardInterrupt | None = None
        if self._has_routing(state_plan):
            try:
                self._stop_routing(state_plan)
                cleanup.append({"routing": "stopped"})
            except OperationCancelled as error:
                cleanup.append({"routing": "interrupted", "error": _error_payload(error)})
                first_error = error
            except NslabError as error:
                cleanup.append({"routing": "failed", "error": _error_payload(error)})
                first_error = error
            except Exception as error:
                cleanup.append({"routing": "failed", "error": _error_payload(error)})
                first_error = error
            except KeyboardInterrupt as error:
                cleanup.append({"routing": "interrupted", "error": _error_payload(error)})
                first_error = error
        for node in reversed(tuple(state_plan.nodes.values())):
            try:
                self.backend.delete_namespace(node.namespace)
                cleanup.append({"namespace": node.namespace, "result": "deleted"})
            except OperationCancelled as error:
                cleanup.append(
                    {
                        "namespace": node.namespace,
                        "result": "interrupted",
                        "error": _error_payload(error),
                    }
                )
                first_error = error
                break
            except NslabError as error:
                if error.code == "RESOURCE_MISSING":
                    cleanup.append({"namespace": node.namespace, "result": "absent"})
                    continue
                cleanup.append(
                    {
                        "namespace": node.namespace,
                        "result": "failed",
                        "error": _error_payload(error),
                    }
                )
                if first_error is None:
                    first_error = error
            except Exception as error:
                cleanup.append(
                    {
                        "namespace": node.namespace,
                        "result": "failed",
                        "error": _error_payload(error),
                    }
                )
                if first_error is None:
                    first_error = error
            except KeyboardInterrupt as error:
                cleanup.append(
                    {
                        "namespace": node.namespace,
                        "result": "interrupted",
                        "error": _error_payload(error),
                    }
                )
                first_error = error
                break

        final_inventory: LiveInventory | None
        try:
            final_inventory = self.backend.inventory(state_plan)
        except Exception as error:
            if first_error is None:
                first_error = error
            final_inventory = None
        except KeyboardInterrupt as error:
            if first_error is None:
                first_error = error
            final_inventory = None

        absent = final_inventory is not None and self._inventory_is_absent(
            state_plan,
            final_inventory,
        )
        if first_error is not None or not absent:
            if first_error is None:
                first_error = NslabError(
                    code="DESTROY_INCOMPLETE",
                    message=f"topology resources remain after destroy: {name}",
                    details={"name": name},
                )
            self._attach(first_error, "cleanup", cleanup)
            _raise_original(first_error)

        self._delete_final_state(state_plan, destroying)
        return LifecycleResult(
            action="destroy",
            name=name,
            changed=True,
            status="absent",
            message=f"destroyed topology: {name}",
        )

    @staticmethod
    def _validate_plan_manifest(plan: TopologyPlan, manifest: Manifest) -> None:
        expected = compile_plan(manifest, name_override=plan.name)
        fingerprint = manifest_fingerprint(manifest)
        if (
            plan.fingerprint != fingerprint
            or plan != expected
            or tuple(plan.nodes) != tuple(expected.nodes)
        ):
            raise NslabError(
                code="PLAN_MANIFEST_MISMATCH",
                message=f"topology plan does not match manifest: {plan.name}",
                details={
                    "name": plan.name,
                    "plan_fingerprint": plan.fingerprint,
                    "manifest_fingerprint": fingerprint,
                },
            )

    @staticmethod
    def _require_snapshot_identity(
        snapshot: StateSnapshot,
        requested_name: str,
    ) -> None:
        if snapshot.name == requested_name:
            return
        raise NslabError(
            code="STATE_INVALID",
            message=(
                f"deployment state identity does not match its requested path: {requested_name}"
            ),
            details={
                "requested_name": requested_name,
                "snapshot_name": snapshot.name,
            },
        )

    @staticmethod
    def _require_stable_snapshot(snapshot: StateSnapshot) -> None:
        if snapshot.status == "deployed":
            return
        raise NslabError(
            code="DEPLOYMENT_INTERRUPTED",
            message=(f"deployment has interrupted {snapshot.status} state: {snapshot.name}"),
            details={"name": snapshot.name, "status": snapshot.status},
        )

    @staticmethod
    def _ownership_unknown(name: str) -> NslabError:
        return NslabError(
            code="OWNERSHIP_UNKNOWN",
            message=f"cannot prove ownership of live resources: {name}",
            details={"name": name},
        )

    @staticmethod
    def _inventory_is_absent(plan: TopologyPlan, inventory: LiveInventory) -> bool:
        if inventory.root_interfaces:
            return False
        expected_namespaces = {node.namespace for node in plan.nodes.values()}
        if set(inventory.namespaces) != expected_namespaces:
            return False
        for node in plan.nodes.values():
            observed = inventory.namespaces[node.namespace]
            if observed.node != node.name:
                return False
            if observed.kind != node.kind:
                return False
            if observed.namespace != node.namespace:
                return False
            if observed.exists or observed.interfaces or observed.routes or observed.sysctls:
                return False
        return True

    def _timestamp(self) -> str:
        moment = self.clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat()

    def _snapshot_for_plan(
        self,
        plan: TopologyPlan,
        manifest: Manifest,
        *,
        created_at: str,
        status: SnapshotStatus,
        inventory: LiveInventory | None,
    ) -> StateSnapshot:
        return StateSnapshot(
            schema=1,
            name=plan.name,
            fingerprint=plan.fingerprint,
            manifest=normalized_manifest(manifest),
            namespaces={name: node.namespace for name, node in plan.nodes.items()},
            interfaces=self._interface_ownership(plan, inventory),
            created_at=created_at,
            status=status,
        )

    @staticmethod
    def _interface_ownership(
        plan: TopologyPlan,
        inventory: LiveInventory | None,
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        endpoints_by_node = {
            name: [
                endpoint
                for link in plan.links
                for endpoint in (link.left, link.right)
                if endpoint.node == name
            ]
            for name in plan.nodes
        }
        for node_name, node in plan.nodes.items():
            if node.kind == "bridge":
                bridge_name = cast(str, node.bridge_name)
                result[f"{node_name}:{bridge_name}"] = {
                    "name": bridge_name,
                    "kind": "bridge",
                    "namespace": node.namespace,
                    "ifindex": (
                        None
                        if inventory is None
                        else inventory.namespaces[node.namespace].interfaces[bridge_name].ifindex
                    ),
                }
            for device in node.devices.values():
                ownership: dict[str, object] = {
                    "name": device.name,
                    "namespace": node.namespace,
                    "ifindex": (
                        None
                        if inventory is None
                        else inventory.namespaces[node.namespace].interfaces[device.name].ifindex
                    ),
                }
                if isinstance(device, VlanDevicePlan):
                    ownership.update(
                        kind="vlan",
                        parent=device.link,
                        vlan_id=device.vlan_id,
                    )
                else:
                    assert isinstance(device, VrfDevicePlan)
                    ownership.update(kind="vrf", vrf_table=device.table)
                result[f"{node_name}:{device.name}"] = ownership
            for endpoint in endpoints_by_node[node_name]:
                result[f"{node_name}:{endpoint.interface}"] = {
                    "name": endpoint.interface,
                    "kind": "veth",
                    "namespace": endpoint.namespace,
                    "ifindex": (
                        None
                        if inventory is None
                        else inventory.namespaces[endpoint.namespace]
                        .interfaces[endpoint.interface]
                        .ifindex
                    ),
                    "temporary_name": endpoint.temporary_name,
                    "link_id": (
                        None
                        if inventory is None
                        else inventory.namespaces[endpoint.namespace]
                        .interfaces[endpoint.interface]
                        .link_id
                    ),
                }
        return result

    @staticmethod
    def _plan_from_snapshot(snapshot: StateSnapshot) -> TopologyPlan:
        return validate_snapshot(snapshot).plan

    def _save_initial_transition(
        self,
        snapshot: StateSnapshot,
        plan: TopologyPlan,
    ) -> None:
        try:
            self.state_store.save(snapshot)
        except Exception as error:
            if self._requires_reconciliation(error):
                reconciliation = self._reconcile(plan)
                self._attach(error, "reconciliation", reconciliation.diagnostics)
            raise
        except KeyboardInterrupt as error:
            reconciliation = self._reconcile(plan)
            self._attach(error, "reconciliation", reconciliation.diagnostics)
            raise

    @staticmethod
    def _requires_reconciliation(error: Exception | KeyboardInterrupt) -> bool:
        return isinstance(error, (OperationCancelled, KeyboardInterrupt)) or (
            isinstance(error, NslabError) and error.code in _UNCERTAIN_STATE_CODES
        )

    def _handle_final_deploy_save_failure(
        self,
        error: Exception | KeyboardInterrupt,
        plan: TopologyPlan,
        created_namespaces: list[str],
        deploying: StateSnapshot,
    ) -> None:
        if self._requires_reconciliation(error):
            reconciliation = self._reconcile(plan)
            self._attach(error, "reconciliation", reconciliation.diagnostics)
            if (
                "state_error" in reconciliation.diagnostics
                or "inventory_error" in reconciliation.diagnostics
            ):
                return
            snapshot = reconciliation.snapshot
            if (
                snapshot is not None
                and snapshot.name == plan.name
                and snapshot.fingerprint == plan.fingerprint
                and snapshot.status == "deployed"
            ):
                return
        self._rollback_and_attach(error, plan, created_namespaces, deploying)

    def _rollback_and_attach(
        self,
        error: Exception | KeyboardInterrupt,
        plan: TopologyPlan,
        created_namespaces: list[str],
        deploying: StateSnapshot,
    ) -> None:
        results: list[dict[str, object]] = []
        cleanup_failed = False
        if self._has_routing(plan):
            try:
                self._stop_routing(plan)
                results.append({"routing": "stopped"})
            except Exception as cleanup_error:
                cleanup_failed = True
                results.append(
                    {
                        "routing": "failed",
                        "error": _error_payload(cleanup_error),
                    }
                )
            except KeyboardInterrupt as cleanup_error:
                cleanup_failed = True
                results.append(
                    {
                        "routing": "interrupted",
                        "error": _error_payload(cleanup_error),
                    }
                )
        for namespace in reversed(created_namespaces):
            try:
                self.backend.delete_namespace(namespace)
                results.append({"namespace": namespace, "result": "deleted"})
            except NslabError as cleanup_error:
                if cleanup_error.code == "RESOURCE_MISSING":
                    results.append({"namespace": namespace, "result": "absent"})
                    continue
                cleanup_failed = True
                results.append(
                    {
                        "namespace": namespace,
                        "result": "failed",
                        "error": _error_payload(cleanup_error),
                    }
                )
            except Exception as cleanup_error:
                cleanup_failed = True
                results.append(
                    {
                        "namespace": namespace,
                        "result": "failed",
                        "error": _error_payload(cleanup_error),
                    }
                )
            except KeyboardInterrupt as cleanup_error:
                cleanup_failed = True
                results.append(
                    {
                        "namespace": namespace,
                        "result": "failed",
                        "error": _error_payload(cleanup_error),
                    }
                )

        inventory: LiveInventory | None
        try:
            inventory = self.backend.inventory(plan)
        except Exception as inventory_error:
            cleanup_failed = True
            inventory = None
            self._attach(error, "rollback_inventory", _error_payload(inventory_error))
        except KeyboardInterrupt as inventory_error:
            cleanup_failed = True
            inventory = None
            self._attach(error, "rollback_inventory", _error_payload(inventory_error))

        absent = inventory is not None and self._inventory_is_absent(plan, inventory)
        if not cleanup_failed and absent:
            cleanup_failed = not self._remove_rollback_state(
                error,
                plan,
                deploying,
            )

        self._attach(error, "rollback", results)
        self._attach(error, "rollback_complete", absent and not cleanup_failed)

    @staticmethod
    def _has_routing(plan: TopologyPlan) -> bool:
        return any(node.routing is not None for node in plan.nodes.values())

    def _start_routing(self, plan: TopologyPlan) -> None:
        if not self._has_routing(plan):
            return
        method = getattr(self.backend, "start_routing", None)
        if not callable(method):
            raise NslabError(
                code="ROUTING_UNSUPPORTED",
                message=f"network backend does not support dynamic routing: {plan.name}",
                details={"name": plan.name},
            )
        method(plan)

    def _stop_routing(self, plan: TopologyPlan) -> None:
        if not self._has_routing(plan):
            return
        method = getattr(self.backend, "stop_routing", None)
        if not callable(method):
            raise NslabError(
                code="ROUTING_UNSUPPORTED",
                message=f"network backend does not support dynamic routing: {plan.name}",
                details={"name": plan.name},
            )
        method(plan)

    def _routing_ready(self, plan: TopologyPlan) -> bool:
        if not self._has_routing(plan):
            return True
        method = getattr(self.backend, "routing_ready", None)
        if not callable(method):
            raise NslabError(
                code="ROUTING_UNSUPPORTED",
                message=f"network backend does not support dynamic routing: {plan.name}",
                details={"name": plan.name},
            )
        return bool(method(plan))

    def _remove_rollback_state(
        self,
        original: Exception | KeyboardInterrupt,
        plan: TopologyPlan,
        deploying: StateSnapshot,
    ) -> bool:
        try:
            current = self.state_store.load(plan.name)
        except Exception as state_error:
            self._attach(original, "rollback_state", _error_payload(state_error))
            return False
        except KeyboardInterrupt as state_error:
            self._attach(original, "rollback_state", _error_payload(state_error))
            return False

        if current is None:
            return True
        if current != deploying:
            self._attach(
                original,
                "rollback_state",
                {
                    "result": "retained",
                    "status": current.status,
                    "fingerprint": current.fingerprint,
                },
            )
            return False

        try:
            self.state_store.delete(plan.name)
            return True
        except Exception as state_error:
            return self._reconcile_rollback_state_delete(
                original,
                state_error,
                plan,
                deploying,
            )
        except KeyboardInterrupt as state_error:
            return self._reconcile_rollback_state_delete(
                original,
                state_error,
                plan,
                deploying,
            )

    def _reconcile_rollback_state_delete(
        self,
        original: Exception | KeyboardInterrupt,
        state_error: Exception | KeyboardInterrupt,
        plan: TopologyPlan,
        deploying: StateSnapshot,
    ) -> bool:
        self._attach(original, "rollback_state", _error_payload(state_error))
        if not self._requires_reconciliation(state_error):
            return False

        gate = self._delete_gate(plan, deploying)
        self._attach(
            original,
            "rollback_state_reconciliation",
            gate.reconciliation.diagnostics,
        )
        is_raw_interrupt = isinstance(
            state_error,
            (OperationCancelled, KeyboardInterrupt),
        )
        if is_raw_interrupt:
            return False
        if not gate.retry_safe:
            return False

        try:
            self.state_store.delete(plan.name)
        except OperationCancelled as retry_error:
            self._raise_delete_retry_interrupt(
                retry_error,
                previous_operation=original,
                previous_state_error=state_error,
                previous_gate=gate,
                plan=plan,
                expected=deploying,
            )
        except Exception as retry_error:
            self._attach(
                original,
                "rollback_state_delete_retry",
                _error_payload(retry_error),
            )
            if not self._requires_reconciliation(retry_error):
                return False
            retry_gate = self._delete_gate(plan, deploying)
            self._attach(
                original,
                "rollback_state_delete_retry_reconciliation",
                retry_gate.reconciliation.diagnostics,
            )
            return False
        except KeyboardInterrupt as retry_error:
            self._raise_delete_retry_interrupt(
                retry_error,
                previous_operation=original,
                previous_state_error=state_error,
                previous_gate=gate,
                plan=plan,
                expected=deploying,
            )
        return True

    def _delete_final_state(
        self,
        plan: TopologyPlan,
        destroying: StateSnapshot,
    ) -> None:
        try:
            self.state_store.delete(plan.name)
        except Exception as error:
            self._handle_final_delete_failure(error, plan, destroying)
        except KeyboardInterrupt as error:
            self._handle_final_delete_failure(error, plan, destroying)

    def _handle_final_delete_failure(
        self,
        error: Exception | KeyboardInterrupt,
        plan: TopologyPlan,
        destroying: StateSnapshot,
    ) -> None:
        if not self._requires_reconciliation(error):
            _raise_original(error)

        gate = self._delete_gate(plan, destroying)
        self._attach(error, "reconciliation", gate.reconciliation.diagnostics)
        is_raw_interrupt = isinstance(error, (OperationCancelled, KeyboardInterrupt))
        if not is_raw_interrupt and gate.retry_safe:
            try:
                self.state_store.delete(plan.name)
            except OperationCancelled as retry_error:
                self._raise_delete_retry_interrupt(
                    retry_error,
                    previous_operation=error,
                    previous_state_error=error,
                    previous_gate=gate,
                    plan=plan,
                    expected=destroying,
                )
            except Exception as retry_error:
                self._attach(error, "state_delete_retry", _error_payload(retry_error))
                if self._requires_reconciliation(retry_error):
                    retry_gate = self._delete_gate(plan, destroying)
                    self._attach(
                        error,
                        "state_delete_retry_reconciliation",
                        retry_gate.reconciliation.diagnostics,
                    )
            except KeyboardInterrupt as retry_error:
                self._raise_delete_retry_interrupt(
                    retry_error,
                    previous_operation=error,
                    previous_state_error=error,
                    previous_gate=gate,
                    plan=plan,
                    expected=destroying,
                )
            else:
                return
        _raise_original(error)

    def _raise_delete_retry_interrupt(
        self,
        interrupt: OperationCancelled | KeyboardInterrupt,
        *,
        previous_operation: Exception | KeyboardInterrupt,
        previous_state_error: Exception | KeyboardInterrupt,
        previous_gate: _DeleteGate,
        plan: TopologyPlan,
        expected: StateSnapshot,
    ) -> Never:
        retry_gate = self._delete_gate(plan, expected)
        self._attach(
            interrupt,
            "reconciliation",
            retry_gate.reconciliation.diagnostics,
        )
        self._attach(
            interrupt,
            "previous_operation_error",
            _error_payload(previous_operation),
        )
        self._attach(
            interrupt,
            "previous_state_delete_error",
            _error_payload(previous_state_error),
        )
        self._attach(
            interrupt,
            "previous_state_delete_reconciliation",
            previous_gate.reconciliation.diagnostics,
        )
        raise interrupt

    def _delete_gate(
        self,
        plan: TopologyPlan,
        expected: StateSnapshot,
    ) -> _DeleteGate:
        reconciliation = self._reconcile(plan)
        conclusive = (
            "state_error" not in reconciliation.diagnostics
            and "inventory_error" not in reconciliation.diagnostics
        )
        live_absent = reconciliation.inventory is not None and self._inventory_is_absent(
            plan, reconciliation.inventory
        )
        return _DeleteGate(
            reconciliation=reconciliation,
            conclusive=conclusive,
            live_absent=live_absent,
            expected_snapshot=reconciliation.snapshot == expected,
        )

    def _reconcile(self, plan: TopologyPlan) -> _Reconciliation:
        diagnostics: dict[str, object] = {}
        snapshot: StateSnapshot | None = None
        inventory: LiveInventory | None = None
        try:
            snapshot = self.state_store.load(plan.name)
            diagnostics["state"] = (
                None
                if snapshot is None
                else {
                    "name": snapshot.name,
                    "fingerprint": snapshot.fingerprint,
                    "status": snapshot.status,
                }
            )
        except Exception as error:
            diagnostics["state_error"] = _error_payload(error)
        except KeyboardInterrupt as error:
            diagnostics["state_error"] = _error_payload(error)

        try:
            inventory = self.backend.inventory(plan)
            diagnostics["inventory"] = {
                "absent": self._inventory_is_absent(plan, inventory),
                "matches": inventory_matches_plan(plan, inventory),
            }
        except Exception as error:
            diagnostics["inventory_error"] = _error_payload(error)
        except KeyboardInterrupt as error:
            diagnostics["inventory_error"] = _error_payload(error)

        return _Reconciliation(
            snapshot=snapshot,
            inventory=inventory,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _attach(
        error: Exception | KeyboardInterrupt,
        key: str,
        value: object,
    ) -> None:
        if isinstance(error, NslabError):
            error.details[key] = value
            return
        error.add_note(
            f"nslab {key}: {json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)}"
        )
