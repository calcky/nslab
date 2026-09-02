from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Never, cast

from pydantic import ValidationError

from nslab.errors import NslabError
from nslab.manifest import Manifest
from nslab.planner import TopologyPlan, VlanDevicePlan, VrfDevicePlan, compile_plan
from nslab.state import StateSnapshot


@dataclass(frozen=True, slots=True)
class SnapshotValidation:
    snapshot: StateSnapshot
    plan: TopologyPlan
    link_ids_complete: bool


def _state_invalid(snapshot: StateSnapshot, reason: str, **details: object) -> Never:
    raise NslabError(
        code="STATE_INVALID",
        message=f"stored deployment state is inconsistent: {snapshot.name}",
        details={"name": snapshot.name, "reason": reason, **details},
    )


def _snapshot_plan(snapshot: StateSnapshot) -> TopologyPlan:
    try:
        manifest = Manifest.model_validate(dict(snapshot.manifest))
    except ValidationError as error:
        raise NslabError(
            code="STATE_INVALID",
            message=f"stored deployment state is inconsistent: {snapshot.name}",
            details={"name": snapshot.name, "reason": "manifest", "error": str(error)},
        ) from error

    plan = compile_plan(manifest, name_override=snapshot.name)
    expected_namespaces = {name: node.namespace for name, node in plan.nodes.items()}
    if plan.fingerprint != snapshot.fingerprint:
        _state_invalid(
            snapshot,
            "fingerprint",
            snapshot_fingerprint=snapshot.fingerprint,
            manifest_fingerprint=plan.fingerprint,
        )
    if dict(snapshot.namespaces) != expected_namespaces:
        _state_invalid(snapshot, "namespaces")
    return plan


def _expected_ownership(plan: TopologyPlan) -> dict[str, dict[str, object]]:
    expected: dict[str, dict[str, object]] = {}
    for node in plan.nodes.values():
        if node.kind == "bridge":
            assert node.bridge_name is not None
            expected[f"{node.name}:{node.bridge_name}"] = {
                "name": node.bridge_name,
                "kind": "bridge",
                "namespace": node.namespace,
            }
        for device in node.devices.values():
            identity: dict[str, object] = {
                "name": device.name,
                "namespace": node.namespace,
            }
            if isinstance(device, VlanDevicePlan):
                identity.update(
                    kind="vlan",
                    parent=device.link,
                    vlan_id=device.vlan_id,
                )
            else:
                assert isinstance(device, VrfDevicePlan)
                identity.update(kind="vrf", vrf_table=device.table)
            expected[f"{node.name}:{device.name}"] = identity
    for link in plan.links:
        for endpoint in (link.left, link.right):
            expected[f"{endpoint.node}:{endpoint.interface}"] = {
                "name": endpoint.interface,
                "kind": "veth",
                "namespace": endpoint.namespace,
                "temporary_name": endpoint.temporary_name,
            }
    return expected


def _validate_interfaces(snapshot: StateSnapshot, plan: TopologyPlan) -> bool:
    expected = _expected_ownership(plan)
    if set(snapshot.interfaces) != set(expected):
        _state_invalid(snapshot, "interface_set")

    ownership_by_key: dict[str, Mapping[str, object]] = {}
    veth_keys: list[str] = []
    for key, desired in expected.items():
        value = snapshot.interfaces[key]
        if not isinstance(value, Mapping):
            _state_invalid(snapshot, "interface_record", interface=key)
        ownership = cast(Mapping[str, object], value)
        ownership_by_key[key] = ownership
        required_fields = {*desired, "ifindex"}
        allowed_fields = set(required_fields)
        if desired["kind"] == "veth":
            allowed_fields.add("link_id")
            veth_keys.append(key)
        if not required_fields <= set(ownership) or not set(ownership) <= allowed_fields:
            _state_invalid(snapshot, "interface_fields", interface=key)
        if any(ownership.get(field) != expected_value for field, expected_value in desired.items()):
            _state_invalid(snapshot, "interface_identity", interface=key)
        ifindex = ownership.get("ifindex")
        if ifindex is not None and (type(ifindex) is not int or ifindex <= 0):
            _state_invalid(snapshot, "ifindex", interface=key)

    if not veth_keys:
        return True

    link_id_fields = tuple("link_id" in ownership_by_key[key] for key in veth_keys)
    if not any(link_id_fields):
        return False
    if not all(link_id_fields):
        _state_invalid(snapshot, "link_id_schema")

    link_id_values = tuple(ownership_by_key[key]["link_id"] for key in veth_keys)
    if all(value is None for value in link_id_values):
        if snapshot.status == "deployed":
            _state_invalid(snapshot, "link_id", interface=veth_keys[0])
        return False
    if any(value is None for value in link_id_values):
        _state_invalid(snapshot, "link_id_schema")

    seen_link_ids: dict[str, int] = {}
    for link in plan.links:
        keys = tuple(
            f"{endpoint.node}:{endpoint.interface}" for endpoint in (link.left, link.right)
        )
        left_id, right_id = (ownership_by_key[key]["link_id"] for key in keys)
        if not isinstance(left_id, str) or not left_id:
            _state_invalid(snapshot, "link_id", interface=keys[0])
        if not isinstance(right_id, str) or not right_id:
            _state_invalid(snapshot, "link_id", interface=keys[1])
        if left_id != right_id:
            _state_invalid(snapshot, "link_id_pair", link=link.index)
        previous_link = seen_link_ids.get(left_id)
        if previous_link is not None and previous_link != link.index:
            _state_invalid(snapshot, "link_id_reused", link=link.index)
        seen_link_ids[left_id] = link.index
    return True


def validate_snapshot(snapshot: StateSnapshot) -> SnapshotValidation:
    """Validate a schema-1 snapshot and reconstruct its immutable topology plan."""

    plan = _snapshot_plan(snapshot)
    return SnapshotValidation(
        snapshot=snapshot,
        plan=plan,
        link_ids_complete=_validate_interfaces(snapshot, plan),
    )
