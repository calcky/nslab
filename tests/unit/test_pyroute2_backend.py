from __future__ import annotations

import errno
import os
import signal
import socket
import sys
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import replace
from inspect import signature
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from pathlib import Path
from subprocess import PIPE
from types import FrameType
from unittest.mock import Mock, PropertyMock, call

import pytest
from pyroute2.netlink.exceptions import NetlinkError

import nslab.backend.pyroute2 as pyroute2_backend
from nslab.backend.base import ExecResult, NetworkBackend
from nslab.backend.pyroute2 import Pyroute2Backend
from nslab.errors import NslabError, OperationCancelled
from nslab.planner import EndpointPlan, LinkPlan, NodePlan, RoutePlan, TopologyPlan

_OWNERSHIP_TOKEN = "nslab-owned-token"
_REAL_OS_CLOSE = os.close
_REAL_OS_OPEN = os.open


def _raise_on_next_line(
    frame: FrameType,
    failure: BaseException,
) -> Callable[[], None]:
    original_trace = sys.gettrace()
    original_frame_trace = frame.f_trace

    def restore_trace() -> None:
        frame.f_trace = original_frame_trace
        sys.settrace(original_trace)

    def raise_failure(traced_frame: FrameType, event: str, _arg: object):
        if traced_frame is frame and event == "line":
            restore_trace()
            raise failure
        return raise_failure

    frame.f_trace = raise_failure
    sys.settrace(raise_failure)
    return restore_trace


@pytest.fixture(autouse=True)
def _stub_exec_pidfds(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    opened_fds: set[int] = set()

    def pidfd_open(_pid: int, _flags: int = 0) -> int:
        pidfd = _REAL_OS_OPEN(os.devnull, os.O_RDONLY)
        opened_fds.add(pidfd)
        return pidfd

    def close(fd: int) -> None:
        opened_fds.discard(fd)
        _REAL_OS_CLOSE(fd)

    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", pidfd_open)
    monkeypatch.setattr(pyroute2_backend.os, "close", close)
    monkeypatch.setattr(pyroute2_backend.signal, "pidfd_send_signal", Mock())
    monkeypatch.setattr(
        pyroute2_backend.os,
        "kill",
        Mock(side_effect=AssertionError("raw PID signalling is forbidden")),
    )
    yield
    for pidfd in tuple(opened_fds):
        with suppress(OSError):
            _REAL_OS_CLOSE(pidfd)


@pytest.fixture
def linux_node() -> NodePlan:
    return NodePlan(
        name="h1",
        kind="linux",
        namespace="nslab-pyroute2-unit-h1",
        interfaces={"eth0": (IPv4Interface("10.10.0.1/24"),)},
        routes=(
            RoutePlan(
                dst=IPv4Network("0.0.0.0/0"),
                via=IPv4Address("10.10.0.254"),
                dev="eth0",
            ),
        ),
        sysctls={"net.ipv4.ip_forward": 1},
    )


@pytest.fixture
def bridge_node() -> NodePlan:
    return NodePlan(
        name="sw1",
        kind="bridge",
        namespace="nslab-pyroute2-unit-sw1",
        interfaces={},
        routes=(),
        sysctls={},
        bridge_name="br0",
        stp=True,
        vlan_filtering=False,
    )


@pytest.fixture
def veth_link(linux_node: NodePlan, bridge_node: NodePlan) -> LinkPlan:
    return LinkPlan(
        index=0,
        kind="veth",
        left=EndpointPlan(
            node=linux_node.name,
            interface="eth0",
            namespace=linux_node.namespace,
            temporary_name="nsl000000000a",
        ),
        right=EndpointPlan(
            node=bridge_node.name,
            interface="swp1",
            namespace=bridge_node.namespace,
            temporary_name="nsl000000000b",
        ),
        mtu=1450,
    )


@pytest.fixture
def topology_plan(
    linux_node: NodePlan,
    bridge_node: NodePlan,
    veth_link: LinkPlan,
) -> TopologyPlan:
    return TopologyPlan(
        name="pyroute2-unit",
        fingerprint="unit-fingerprint",
        nodes={linux_node.name: linux_node, bridge_node.name: bridge_node},
        links=(veth_link,),
    )


def _link_message(
    index: int,
    name: str,
    kind: str,
    mtu: int,
    *,
    up: bool = True,
    master: int | None = None,
    stp: int | None = None,
    vlan_filtering: int | None = None,
    alias: str | None = None,
) -> dict[str, object]:
    info_data: list[tuple[str, object]] = []
    if stp is not None:
        info_data.append(("IFLA_BR_STP_STATE", stp))
    if vlan_filtering is not None:
        info_data.append(("IFLA_BR_VLAN_FILTERING", vlan_filtering))
    link_info: list[tuple[str, object]] = [("IFLA_INFO_KIND", kind)]
    if info_data:
        link_info.append(("IFLA_INFO_DATA", {"attrs": info_data}))
    attributes: list[tuple[str, object]] = [
        ("IFLA_IFNAME", name),
        ("IFLA_MTU", mtu),
        ("IFLA_LINKINFO", {"attrs": link_info}),
    ]
    if master is not None:
        attributes.append(("IFLA_MASTER", master))
    if alias is not None:
        attributes.append(("IFLA_IFALIAS", alias))
    return {
        "index": index,
        "flags": 1 if up else 0,
        "attrs": attributes,
    }


def _address_message(index: int, address: str, prefixlen: int) -> dict[str, object]:
    return {
        "index": index,
        "prefixlen": prefixlen,
        "attrs": [("IFA_LOCAL", address)],
    }


def _alias_message(alias: str = _OWNERSHIP_TOKEN) -> dict[str, object]:
    return {"attrs": [("IFLA_IFALIAS", alias)]}


def _route_message(
    dst: str | None,
    prefixlen: int,
    oif: int | None,
    *,
    gateway: str | None = None,
    table: int = 254,
    route_type: int = 1,
    src_len: int = 0,
    tos: int = 0,
    flags: int = 0,
    proto: int = 4,
    scope: int = 0,
    extra_attrs: tuple[tuple[str, object], ...] = (),
) -> dict[str, object]:
    attributes: list[tuple[str, object]] = []
    if oif is not None:
        attributes.append(("RTA_OIF", oif))
    if dst is not None:
        attributes.append(("RTA_DST", dst))
    if gateway is not None:
        attributes.append(("RTA_GATEWAY", gateway))
    attributes.extend(extra_attrs)
    return {
        "table": table,
        "type": route_type,
        "dst_len": prefixlen,
        "src_len": src_len,
        "tos": tos,
        "flags": flags,
        "proto": proto,
        "scope": scope,
        "attrs": attributes,
    }


class _IndexedAttribute:
    def __init__(self, name: str, value: object) -> None:
        self._name = name
        self._value = value

    def __getitem__(self, index: int) -> object:
        if index == 0:
            return self._name
        if index == 1:
            return self._value
        raise IndexError(index)


class _NamedAttribute:
    def __init__(self, name: str, value: object) -> None:
        self.name = name
        self.value = value


@pytest.mark.parametrize("attribute_type", (_IndexedAttribute, _NamedAttribute))
def test_inventory_attribute_fallback_supports_slots_without_len(
    attribute_type: type[_IndexedAttribute] | type[_NamedAttribute],
) -> None:
    def attribute(name: str, value: object) -> object:
        return attribute_type(name, value)

    link_info = {
        "attrs": [
            attribute("IFLA_INFO_KIND", "bridge"),
            attribute(
                "IFLA_INFO_DATA",
                {
                    "attrs": [
                        attribute("IFLA_BR_STP_STATE", 1),
                        attribute("IFLA_BR_VLAN_FILTERING", 0),
                    ]
                },
            ),
        ]
    }
    message = {
        "index": 20,
        "flags": 1,
        "attrs": [
            attribute("IFLA_IFNAME", "br0"),
            attribute("IFLA_MTU", 1500),
            attribute("IFLA_IFALIAS", "semantic-link-id"),
            attribute("IFLA_LINKINFO", link_info),
        ],
    }

    interfaces, names_by_index = Pyroute2Backend._inventory_interfaces((message,), ())

    assert names_by_index == {20: "br0"}
    assert interfaces["br0"].kind == "bridge"
    assert interfaces["br0"].mtu == 1500
    assert interfaces["br0"].up is True
    assert interfaces["br0"].stp is True
    assert interfaces["br0"].vlan_filtering is False
    assert interfaces["br0"].link_id == "semantic-link-id"


def test_backend_implements_network_backend_protocol() -> None:
    assert isinstance(Pyroute2Backend(), NetworkBackend)


def test_default_namespace_opener_never_creates_missing_inventory_namespaces(
    topology_plan: TopologyPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor = Mock(side_effect=FileNotFoundError(errno.ENOENT, "namespace missing"))
    root = Mock()
    root.link_lookup.return_value = []
    monkeypatch.setattr(pyroute2_backend, "NetNS", constructor)
    default_opener = signature(Pyroute2Backend.__init__).parameters["netns_factory"].default

    assert default_opener is pyroute2_backend._open_existing_namespace

    inventory = Pyroute2Backend(iproute_factory=Mock(return_value=root)).inventory(topology_plan)

    assert constructor.call_args_list == [
        call(node.namespace, flags=0) for node in topology_plan.nodes.values()
    ]
    assert all(not observed.exists for observed in inventory.namespaces.values())
    assert root.link_lookup.call_args_list == [
        call(ifname=endpoint.temporary_name)
        for link in topology_plan.links
        for endpoint in (link.left, link.right)
    ]
    root.close.assert_called_once_with()


def test_default_namespace_enter_uses_non_creating_setns(
    tmp_path: Path,
    linux_node: NodePlan,
    topology_plan: TopologyPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    push_current = Mock()
    set_existing = Mock()
    restore_current = Mock()
    monkeypatch.setattr(pyroute2_backend.netns, "pushns", push_current)
    monkeypatch.setattr(pyroute2_backend.netns, "setns", set_existing)
    monkeypatch.setattr(pyroute2_backend.netns, "popns", restore_current)
    default_enter = signature(Pyroute2Backend.__init__).parameters["pushns"].default

    assert default_enter is pyroute2_backend._enter_existing_namespace

    node = replace(linux_node, interfaces={}, routes=())
    sysctl = tmp_path / "net/ipv4/ip_forward"
    sysctl.parent.mkdir(parents=True)
    sysctl.write_text("0\n", encoding="ascii")
    backend = Pyroute2Backend(
        netns_factory=Mock(return_value=Mock()),
        popns=restore_current,
        sysctl_root=tmp_path,
    )

    backend.configure_node(node, topology_plan)

    push_current.assert_called_once_with()
    set_existing.assert_called_once_with(node.namespace, flags=0)
    restore_current.assert_called_once_with()
    assert sysctl.read_text(encoding="ascii") == "1\n"


def test_default_namespace_enter_restores_stack_when_setns_fails(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = FileNotFoundError(errno.ENOENT, "namespace missing")
    push_current = Mock()
    set_existing = Mock(side_effect=failure)
    restore_current = Mock()
    monkeypatch.setattr(pyroute2_backend.netns, "pushns", push_current)
    monkeypatch.setattr(pyroute2_backend.netns, "setns", set_existing)
    monkeypatch.setattr(pyroute2_backend.netns, "popns", restore_current)
    default_enter = signature(Pyroute2Backend.__init__).parameters["pushns"].default

    assert default_enter is pyroute2_backend._enter_existing_namespace

    with pytest.raises(FileNotFoundError) as caught:
        default_enter(linux_node.namespace)

    assert caught.value is failure
    push_current.assert_called_once_with()
    set_existing.assert_called_once_with(linux_node.namespace, flags=0)
    restore_current.assert_called_once_with()


def test_default_namespace_enter_restore_failure_does_not_mask_setns_error(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = RuntimeError("setns failed")
    cleanup = RuntimeError("pop failed")
    push_current = Mock()
    set_existing = Mock(side_effect=primary)
    restore_current = Mock(side_effect=cleanup)
    monkeypatch.setattr(pyroute2_backend.netns, "pushns", push_current)
    monkeypatch.setattr(pyroute2_backend.netns, "setns", set_existing)
    monkeypatch.setattr(pyroute2_backend.netns, "popns", restore_current)

    with pytest.raises(RuntimeError) as caught:
        pyroute2_backend._enter_existing_namespace(linux_node.namespace)

    assert caught.value is primary
    assert caught.value.__notes__ == ["namespace pop cleanup failed: RuntimeError('pop failed')"]
    push_current.assert_called_once_with()
    set_existing.assert_called_once_with(linux_node.namespace, flags=0)
    restore_current.assert_called_once_with()


def test_create_namespace_creates_name_brings_loopback_up_and_closes_handle(
    linux_node: NodePlan,
) -> None:
    events: list[tuple[str, str]] = []
    namespace_create = Mock(side_effect=lambda name: events.append(("create", name)))
    handle = Mock()
    handle.link_lookup.return_value = [7]

    def netns_factory(name: str) -> Mock:
        events.append(("open", name))
        return handle

    backend = Pyroute2Backend(
        namespace_create=namespace_create,
        netns_factory=netns_factory,
    )

    backend.create_namespace(linux_node)

    assert events == [
        ("create", linux_node.namespace),
        ("open", linux_node.namespace),
    ]
    assert handle.mock_calls == [
        call.link_lookup(ifname="lo"),
        call.link("set", index=7, state="up"),
        call.close(),
    ]


@pytest.mark.parametrize(
    ("error_number", "expected_code"),
    [
        (errno.EEXIST, "RESOURCE_EXISTS"),
        (errno.ENOENT, "RESOURCE_MISSING"),
        (errno.EPERM, "NETLINK_ERROR"),
    ],
)
def test_netlink_errors_preserve_errno_operation_and_resource(
    linux_node: NodePlan,
    error_number: int,
    expected_code: str,
) -> None:
    failure = NetlinkError(error_number, "injected")
    namespace_create = Mock(side_effect=failure)
    backend = Pyroute2Backend(namespace_create=namespace_create)

    with pytest.raises(NslabError) as caught:
        backend.create_namespace(linux_node)

    assert caught.value.code == expected_code
    assert caught.value.details == {
        "errno": error_number,
        "operation": "create_namespace",
        "resource": linux_node.namespace,
    }
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (FileExistsError(errno.EEXIST, "namespace exists"), "RESOURCE_EXISTS"),
        (FileNotFoundError(errno.ENOENT, "namespace path missing"), "RESOURCE_MISSING"),
        (OSError(errno.ENODEV, "namespace device missing"), "RESOURCE_MISSING"),
        (OSError(errno.EPERM, "namespace operation denied"), "NETLINK_ERROR"),
    ],
)
def test_namespace_os_errors_use_stable_resource_translation(
    linux_node: NodePlan,
    failure: OSError,
    expected_code: str,
) -> None:
    namespace_create = Mock(side_effect=failure)
    backend = Pyroute2Backend(namespace_create=namespace_create)

    with pytest.raises(NslabError) as caught:
        backend.create_namespace(linux_node)

    assert caught.value.code == expected_code
    assert caught.value.details == {
        "errno": failure.errno,
        "operation": "create_namespace",
        "resource": linux_node.namespace,
    }
    assert caught.value.__cause__ is failure


def test_create_namespace_failure_after_create_cleans_without_masking_original(
    linux_node: NodePlan,
) -> None:
    original = NetlinkError(errno.EPERM, "failed to open created namespace")
    cleanup = OSError(errno.EIO, "cleanup failed")
    namespace_create = Mock()
    namespace_remove = Mock(side_effect=cleanup)
    backend = Pyroute2Backend(
        namespace_create=namespace_create,
        namespace_remove=namespace_remove,
        netns_factory=Mock(side_effect=original),
    )

    with pytest.raises(NslabError) as caught:
        backend.create_namespace(linux_node)

    namespace_create.assert_called_once_with(linux_node.namespace)
    namespace_remove.assert_called_once_with(linux_node.namespace)
    assert caught.value.code == "NETLINK_ERROR"
    assert caught.value.__cause__ is original


def test_create_namespace_exists_failure_never_removes_existing_namespace(
    linux_node: NodePlan,
) -> None:
    original = FileExistsError(errno.EEXIST, "namespace exists")
    namespace_remove = Mock()
    backend = Pyroute2Backend(
        namespace_create=Mock(side_effect=original),
        namespace_remove=namespace_remove,
    )

    with pytest.raises(NslabError) as caught:
        backend.create_namespace(linux_node)

    assert caught.value.code == "RESOURCE_EXISTS"
    namespace_remove.assert_not_called()


def test_delete_namespace_uses_injected_remove_and_translates_missing(
    linux_node: NodePlan,
) -> None:
    failure = NetlinkError(errno.ENODEV, "gone")
    namespace_remove = Mock(side_effect=failure)
    backend = Pyroute2Backend(namespace_remove=namespace_remove)

    with pytest.raises(NslabError) as caught:
        backend.delete_namespace(linux_node.namespace)

    namespace_remove.assert_called_once_with(linux_node.namespace)
    assert caught.value.code == "RESOURCE_MISSING"
    assert caught.value.details == {
        "errno": errno.ENODEV,
        "operation": "delete_namespace",
        "resource": linux_node.namespace,
    }


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError(errno.ENOENT, "namespace path missing"),
        OSError(errno.ENODEV, "namespace device missing"),
    ],
)
def test_delete_namespace_translates_missing_os_errors(
    linux_node: NodePlan,
    failure: OSError,
) -> None:
    namespace_remove = Mock(side_effect=failure)
    backend = Pyroute2Backend(namespace_remove=namespace_remove)

    with pytest.raises(NslabError) as caught:
        backend.delete_namespace(linux_node.namespace)

    assert caught.value.code == "RESOURCE_MISSING"
    assert caught.value.details == {
        "errno": failure.errno,
        "operation": "delete_namespace",
        "resource": linux_node.namespace,
    }
    assert caught.value.__cause__ is failure


def test_create_bridge_sets_kernel_bridge_attributes_and_closes_handle(
    bridge_node: NodePlan,
) -> None:
    handle = Mock()
    netns_factory = Mock(return_value=handle)
    backend = Pyroute2Backend(netns_factory=netns_factory)

    backend.create_bridge(bridge_node)

    netns_factory.assert_called_once_with(bridge_node.namespace)
    assert handle.mock_calls == [
        call.link(
            "add",
            ifname="br0",
            kind="bridge",
            br_stp_state=1,
            br_vlan_filtering=0,
        ),
        call.close(),
    ]


def test_create_bridge_closes_handle_and_translates_netlink_failure(
    bridge_node: NodePlan,
) -> None:
    failure = NetlinkError(errno.EEXIST, "bridge exists")
    handle = Mock()
    handle.link.side_effect = failure
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    with pytest.raises(NslabError) as caught:
        backend.create_bridge(bridge_node)

    handle.close.assert_called_once_with()
    assert caught.value.code == "RESOURCE_EXISTS"
    assert caught.value.details == {
        "errno": errno.EEXIST,
        "operation": "create_bridge",
        "resource": f"{bridge_node.namespace}:br0",
    }
    assert caught.value.__cause__ is failure


def test_handle_close_failure_is_not_allowed_to_mask_primary_error(
    bridge_node: NodePlan,
) -> None:
    primary = RuntimeError("bridge creation failed")
    cleanup = RuntimeError("close failed")
    handle = Mock()
    handle.link.side_effect = primary
    handle.close.side_effect = cleanup
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    with pytest.raises(RuntimeError) as caught:
        backend.create_bridge(bridge_node)

    assert caught.value is primary
    assert caught.value.__notes__ == ["handle close cleanup failed: RuntimeError('close failed')"]


def test_handle_close_failure_propagates_without_primary_error(
    bridge_node: NodePlan,
) -> None:
    cleanup = RuntimeError("close failed")
    handle = Mock()
    handle.close.side_effect = cleanup
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    with pytest.raises(RuntimeError) as caught:
        backend.create_bridge(bridge_node)

    assert caught.value is cleanup


def test_create_veth_moves_renames_sizes_and_brings_up_both_endpoints(
    veth_link: LinkPlan,
) -> None:
    root = Mock()
    lookup_count: dict[str, int] = {}

    def root_lookup(*, ifname: str) -> list[int]:
        occurrence = lookup_count.get(ifname, 0)
        lookup_count[ifname] = occurrence + 1
        if occurrence == 0:
            return []
        return [101 if ifname == veth_link.left.temporary_name else 102]

    root.link_lookup.side_effect = root_lookup
    left = Mock()
    left.link_lookup.return_value = [201]
    right = Mock()
    right.link_lookup.return_value = [301]
    handles = {
        veth_link.left.namespace: left,
        veth_link.right.namespace: right,
    }
    netns_factory = Mock(side_effect=lambda namespace: handles[namespace])
    ownership_token_factory = Mock(return_value=_OWNERSHIP_TOKEN)
    backend = Pyroute2Backend(
        iproute_factory=Mock(return_value=root),
        netns_factory=netns_factory,
        ownership_token_factory=ownership_token_factory,
    )

    backend.create_veth(veth_link)

    ownership_token_factory.assert_called_once_with()
    assert root.mock_calls == [
        call.link_lookup(ifname=veth_link.left.temporary_name),
        call.link_lookup(ifname=veth_link.right.temporary_name),
        call.link(
            "add",
            ifname=veth_link.left.temporary_name,
            kind="veth",
            ifalias=_OWNERSHIP_TOKEN,
            peer={
                "ifname": veth_link.right.temporary_name,
                "ifalias": _OWNERSHIP_TOKEN,
            },
        ),
        call.link_lookup(ifname=veth_link.left.temporary_name),
        call.link_lookup(ifname=veth_link.right.temporary_name),
        call.link(
            "set",
            index=101,
            ifalias=_OWNERSHIP_TOKEN,
        ),
        call.link(
            "set",
            index=102,
            ifalias=_OWNERSHIP_TOKEN,
        ),
        call.link(
            "set",
            index=101,
            net_ns_fd=veth_link.left.namespace,
        ),
        call.link(
            "set",
            index=102,
            net_ns_fd=veth_link.right.namespace,
        ),
        call.close(),
    ]
    assert netns_factory.call_args_list == [
        call(veth_link.left.namespace),
        call(veth_link.right.namespace),
    ]
    assert left.mock_calls == [
        call.link_lookup(ifname=veth_link.left.temporary_name),
        call.link("set", index=201, ifname=veth_link.left.interface),
        call.link("set", index=201, mtu=veth_link.mtu),
        call.link("set", index=201, state="up"),
        call.close(),
    ]
    assert right.mock_calls == [
        call.link_lookup(ifname=veth_link.right.temporary_name),
        call.link("set", index=301, ifname=veth_link.right.interface),
        call.link("set", index=301, mtu=veth_link.mtu),
        call.link("set", index=301, state="up"),
        call.close(),
    ]


def test_veth_cleanup_skips_same_location_name_reused_by_foreign_interface(
    veth_link: LinkPlan,
) -> None:
    primary = NetlinkError(errno.EPERM, "first move failed")
    root = Mock()
    lookup_count: dict[str, int] = {}

    def root_lookup(*, ifname: str) -> list[int]:
        occurrence = lookup_count.get(ifname, 0)
        lookup_count[ifname] = occurrence + 1
        if occurrence == 0:
            return []
        if occurrence == 1:
            return [101 if ifname == veth_link.left.temporary_name else 102]
        return [999 if ifname == veth_link.left.temporary_name else 102]

    def root_link(command: str, **kwargs: object) -> None:
        if command == "set" and kwargs.get("net_ns_fd") == veth_link.left.namespace:
            raise primary

    root.link_lookup.side_effect = root_lookup
    root.link.side_effect = root_link
    root.get_links.side_effect = lambda index: [
        _link_message(
            index,
            (veth_link.left.temporary_name if index == 999 else veth_link.right.temporary_name),
            "veth",
            veth_link.mtu,
            alias="foreign-owner" if index == 999 else _OWNERSHIP_TOKEN,
        )
    ]
    backend = Pyroute2Backend(
        iproute_factory=Mock(return_value=root),
        ownership_token_factory=Mock(return_value=_OWNERSHIP_TOKEN),
    )

    with pytest.raises(NslabError) as caught:
        backend.create_veth(veth_link)

    assert caught.value.code == "NETLINK_ERROR"
    assert caught.value.__cause__ is primary
    assert root.get_links.call_args_list == [call(999), call(102)]
    assert call.link("del", index=999) not in root.mock_calls
    assert call.link("del", index=102) in root.mock_calls


def test_partial_veth_failure_cleans_all_locations_without_masking_original(
    veth_link: LinkPlan,
) -> None:
    original = NetlinkError(errno.EPERM, "second move failed")
    cleanup = NetlinkError(errno.EIO, "root cleanup failed")
    root = Mock()
    lookup_count: dict[str, int] = {}

    def root_lookup(*, ifname: str) -> list[int]:
        occurrence = lookup_count.get(ifname, 0)
        lookup_count[ifname] = occurrence + 1
        if occurrence == 0:
            return []
        if occurrence == 1:
            return [101 if ifname == veth_link.left.temporary_name else 102]
        if ifname == veth_link.right.temporary_name:
            return [102]
        return []

    def root_link(command: str, **kwargs: object) -> None:
        if command == "set" and kwargs.get("net_ns_fd") == veth_link.right.namespace:
            raise original
        if command == "del" and kwargs.get("index") == 102:
            raise cleanup

    root.link_lookup.side_effect = root_lookup
    root.link.side_effect = root_link
    root.get_links.return_value = [_alias_message()]
    left = Mock()
    left.link_lookup.side_effect = lambda *, ifname: (
        [201] if ifname == veth_link.left.temporary_name else []
    )
    left.get_links.return_value = [_alias_message()]
    right = Mock()
    right.link_lookup.return_value = []
    handles = {
        veth_link.left.namespace: left,
        veth_link.right.namespace: right,
    }
    backend = Pyroute2Backend(
        iproute_factory=Mock(return_value=root),
        netns_factory=Mock(side_effect=lambda namespace: handles[namespace]),
        ownership_token_factory=Mock(return_value=_OWNERSHIP_TOKEN),
    )

    with pytest.raises(NslabError) as caught:
        backend.create_veth(veth_link)

    assert caught.value.code == "NETLINK_ERROR"
    assert caught.value.details == {
        "errno": errno.EPERM,
        "operation": "create_veth",
        "resource": (f"{veth_link.left.temporary_name}<->{veth_link.right.temporary_name}"),
    }
    assert caught.value.__cause__ is original
    assert call.link("del", index=102) in root.mock_calls
    assert call.link("del", index=201) in left.mock_calls
    assert root.close.call_count == 2
    left.close.assert_called_once_with()
    right.close.assert_not_called()


def test_veth_rename_collision_cleans_owned_temp_without_deleting_existing_final(
    veth_link: LinkPlan,
) -> None:
    original = NetlinkError(errno.EEXIST, "final interface already exists")
    root = Mock()
    root_lookup_count: dict[str, int] = {}

    def root_lookup(*, ifname: str) -> list[int]:
        occurrence = root_lookup_count.get(ifname, 0)
        root_lookup_count[ifname] = occurrence + 1
        if occurrence == 0:
            return []
        if occurrence == 1:
            return [101 if ifname == veth_link.left.temporary_name else 102]
        return []

    root.link_lookup.side_effect = root_lookup
    left = Mock()
    left.link_lookup.side_effect = lambda *, ifname: (
        [999] if ifname == veth_link.left.interface else [201]
    )

    def left_link(command: str, **kwargs: object) -> None:
        if command == "set" and kwargs.get("ifname") == veth_link.left.interface:
            raise original

    left.link.side_effect = left_link
    left.get_links.return_value = [_alias_message()]
    right = Mock()
    right.link_lookup.return_value = [301]
    right.get_links.return_value = [_alias_message()]
    handles = {
        veth_link.left.namespace: left,
        veth_link.right.namespace: right,
    }
    backend = Pyroute2Backend(
        iproute_factory=Mock(return_value=root),
        netns_factory=Mock(side_effect=lambda namespace: handles[namespace]),
        ownership_token_factory=Mock(return_value=_OWNERSHIP_TOKEN),
    )

    with pytest.raises(NslabError) as caught:
        backend.create_veth(veth_link)

    assert caught.value.code == "RESOURCE_EXISTS"
    assert caught.value.details == {
        "errno": errno.EEXIST,
        "operation": "create_veth",
        "resource": (f"{veth_link.left.temporary_name}<->{veth_link.right.temporary_name}"),
    }
    assert caught.value.__cause__ is original
    assert call.link("del", index=201) in left.mock_calls
    assert call.link("del", index=301) in right.mock_calls
    assert call.link_lookup(ifname=veth_link.left.interface) not in left.mock_calls
    assert call.link("del", index=999) not in left.mock_calls


def test_veth_mtu_failure_cleans_owned_final_without_probing_reused_temp(
    veth_link: LinkPlan,
) -> None:
    original = NetlinkError(errno.EPERM, "mtu update failed")
    root = Mock()
    root_lookup_count: dict[str, int] = {}

    def root_lookup(*, ifname: str) -> list[int]:
        occurrence = root_lookup_count.get(ifname, 0)
        root_lookup_count[ifname] = occurrence + 1
        if occurrence == 0:
            return []
        if occurrence == 1:
            return [101 if ifname == veth_link.left.temporary_name else 102]
        return []

    root.link_lookup.side_effect = root_lookup
    root_factory = Mock(return_value=root)
    left = Mock()
    left_temp_lookups = 0

    def left_lookup(*, ifname: str) -> list[int]:
        nonlocal left_temp_lookups
        if ifname == veth_link.left.interface:
            return [201]
        left_temp_lookups += 1
        return [201] if left_temp_lookups == 1 else [999]

    left.link_lookup.side_effect = left_lookup

    def left_link(command: str, **kwargs: object) -> None:
        if command == "set" and kwargs.get("mtu") == veth_link.mtu:
            raise original

    left.link.side_effect = left_link
    left.get_links.return_value = [_alias_message()]
    right = Mock()
    right.link_lookup.return_value = [301]
    right.get_links.return_value = [_alias_message()]
    handles = {
        veth_link.left.namespace: left,
        veth_link.right.namespace: right,
    }
    backend = Pyroute2Backend(
        iproute_factory=root_factory,
        netns_factory=Mock(side_effect=lambda namespace: handles[namespace]),
        ownership_token_factory=Mock(return_value=_OWNERSHIP_TOKEN),
    )

    with pytest.raises(NslabError) as caught:
        backend.create_veth(veth_link)

    assert caught.value.code == "NETLINK_ERROR"
    assert caught.value.__cause__ is original
    assert root_factory.call_count == 1
    assert left.link_lookup.call_args_list == [
        call(ifname=veth_link.left.temporary_name),
        call(ifname=veth_link.left.interface),
    ]
    assert call.link("del", index=201) in left.mock_calls
    assert call.link("del", index=999) not in left.mock_calls
    assert call.link("del", index=301) in right.mock_calls


def test_partial_veth_move_cleans_only_each_endpoints_owned_location(
    veth_link: LinkPlan,
) -> None:
    original = NetlinkError(errno.EPERM, "right move failed")
    root = Mock()
    root_lookup_count: dict[str, int] = {}

    def root_lookup(*, ifname: str) -> list[int]:
        occurrence = root_lookup_count.get(ifname, 0)
        root_lookup_count[ifname] = occurrence + 1
        if occurrence == 0:
            return []
        if occurrence == 1:
            return [101 if ifname == veth_link.left.temporary_name else 102]
        if ifname == veth_link.left.temporary_name:
            return [999]
        return [102]

    root.link_lookup.side_effect = root_lookup
    root.get_links.return_value = [_alias_message()]

    def root_link(command: str, **kwargs: object) -> None:
        if command == "set" and kwargs.get("net_ns_fd") == veth_link.right.namespace:
            raise original

    root.link.side_effect = root_link
    left = Mock()
    left.link_lookup.return_value = [201]
    left.get_links.return_value = [_alias_message()]
    right = Mock()
    handles = {
        veth_link.left.namespace: left,
        veth_link.right.namespace: right,
    }
    netns_factory = Mock(side_effect=lambda namespace: handles[namespace])
    backend = Pyroute2Backend(
        iproute_factory=Mock(return_value=root),
        netns_factory=netns_factory,
        ownership_token_factory=Mock(return_value=_OWNERSHIP_TOKEN),
    )

    with pytest.raises(NslabError) as caught:
        backend.create_veth(veth_link)

    assert caught.value.code == "NETLINK_ERROR"
    assert caught.value.__cause__ is original
    assert root.link_lookup.call_args_list.count(call(ifname=veth_link.left.temporary_name)) == 2
    assert call.link("del", index=999) not in root.mock_calls
    assert call.link("del", index=102) in root.mock_calls
    assert call.link("del", index=201) in left.mock_calls
    assert netns_factory.call_args_list == [call(veth_link.left.namespace)]
    right.close.assert_not_called()


def test_veth_add_failure_cleans_discoverable_partial_root_endpoint(
    veth_link: LinkPlan,
) -> None:
    original = NetlinkError(errno.EIO, "add left a partial pair")
    root = Mock()

    def root_link(command: str, **_kwargs: object) -> None:
        if command == "add":
            raise original

    root.link.side_effect = root_link
    lookup_count: dict[str, int] = {}

    def root_lookup(*, ifname: str) -> list[int]:
        occurrence = lookup_count.get(ifname, 0)
        lookup_count[ifname] = occurrence + 1
        if occurrence == 0:
            return []
        return [101] if ifname == veth_link.left.temporary_name else []

    root.link_lookup.side_effect = root_lookup
    root.get_links.return_value = [_alias_message()]
    empty_namespace = Mock()
    empty_namespace.link_lookup.return_value = []
    netns_factory = Mock(return_value=empty_namespace)
    backend = Pyroute2Backend(
        iproute_factory=Mock(return_value=root),
        netns_factory=netns_factory,
        ownership_token_factory=Mock(return_value=_OWNERSHIP_TOKEN),
    )

    with pytest.raises(NslabError) as caught:
        backend.create_veth(veth_link)

    assert caught.value.code == "NETLINK_ERROR"
    assert caught.value.__cause__ is original
    assert call.link("del", index=101) in root.mock_calls
    netns_factory.assert_not_called()


def test_veth_preflight_never_deletes_or_mutates_preexisting_temporary_name(
    veth_link: LinkPlan,
) -> None:
    root = Mock()
    root.link_lookup.side_effect = [
        [101],
    ]
    backend = Pyroute2Backend(iproute_factory=Mock(return_value=root))

    with pytest.raises(NslabError) as caught:
        backend.create_veth(veth_link)

    assert caught.value.code == "RESOURCE_EXISTS"
    root.link.assert_not_called()
    assert call.link("del", index=101) not in root.mock_calls


def test_configure_linux_node_adds_address_route_and_namespace_scoped_sysctl(
    tmp_path: Path,
    linux_node: NodePlan,
    topology_plan: TopologyPlan,
) -> None:
    events: list[str] = []
    handle = Mock()
    handle.link_lookup.return_value = [10]
    handle.close.side_effect = lambda: events.append("close")
    pushns = Mock(side_effect=lambda _namespace: events.append("push"))
    popns = Mock(side_effect=lambda: events.append("pop"))
    sysctl = tmp_path / "net/ipv4/ip_forward"
    sysctl.parent.mkdir(parents=True)
    sysctl.write_text("0\n", encoding="ascii")
    backend = Pyroute2Backend(
        netns_factory=Mock(return_value=handle),
        pushns=pushns,
        popns=popns,
        sysctl_root=tmp_path,
    )

    backend.configure_node(linux_node, topology_plan)

    assert handle.mock_calls == [
        call.link_lookup(ifname="eth0"),
        call.addr(
            "add",
            index=10,
            address="10.10.0.1",
            prefixlen=24,
        ),
        call.link("set", index=10, state="up"),
        call.route(
            "add",
            dst="0.0.0.0/0",
            oif=10,
            gateway="10.10.0.254",
        ),
        call.close(),
    ]
    assert events == ["close", "push", "pop"]
    pushns.assert_called_once_with(linux_node.namespace)
    assert sysctl.read_text(encoding="ascii") == "1\n"


def test_configure_bridge_attaches_ports_and_configures_internal_bridge(
    bridge_node: NodePlan,
    topology_plan: TopologyPlan,
) -> None:
    handle = Mock()
    handle.link_lookup.side_effect = lambda *, ifname: {
        "br0": [20],
        "swp1": [21],
    }[ifname]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    backend.configure_node(bridge_node, topology_plan)

    assert handle.mock_calls == [
        call.link_lookup(ifname="br0"),
        call.link_lookup(ifname="swp1"),
        call.link("set", index=21, master=20),
        call.link("set", index=20, state="up"),
        call.close(),
    ]


def test_configure_bridge_adds_address_only_when_explicitly_declared(
    bridge_node: NodePlan,
    topology_plan: TopologyPlan,
) -> None:
    addressed = replace(
        bridge_node,
        interfaces={"br0": (IPv4Interface("192.0.2.1/24"),)},
    )
    addressed_plan = replace(
        topology_plan,
        nodes={"h1": topology_plan.nodes["h1"], "sw1": addressed},
    )
    handle = Mock()
    handle.link_lookup.side_effect = lambda *, ifname: {
        "br0": [20],
        "swp1": [21],
    }[ifname]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    backend.configure_node(addressed, addressed_plan)

    assert (
        call.addr(
            "add",
            index=20,
            address="192.0.2.1",
            prefixlen=24,
        )
        in handle.mock_calls
    )
    assert call.link("set", index=20, state="up") in handle.mock_calls


def test_configure_node_closes_netns_and_translates_netlink_failure(
    linux_node: NodePlan,
    topology_plan: TopologyPlan,
) -> None:
    failure = NetlinkError(errno.EEXIST, "address exists")
    handle = Mock()
    handle.link_lookup.return_value = [10]
    handle.addr.side_effect = failure
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    with pytest.raises(NslabError) as caught:
        backend.configure_node(linux_node, topology_plan)

    handle.close.assert_called_once_with()
    assert caught.value.code == "RESOURCE_EXISTS"
    assert caught.value.details == {
        "errno": errno.EEXIST,
        "operation": "configure_node",
        "resource": linux_node.namespace,
    }


def test_sysctl_popns_runs_in_finally_when_proc_write_fails(
    tmp_path: Path,
    linux_node: NodePlan,
    topology_plan: TopologyPlan,
) -> None:
    handle = Mock()
    pushns = Mock()
    popns = Mock()
    node = replace(linux_node, interfaces={}, routes=())
    backend = Pyroute2Backend(
        netns_factory=Mock(return_value=handle),
        pushns=pushns,
        popns=popns,
        sysctl_root=tmp_path,
    )

    with pytest.raises(FileNotFoundError):
        backend.configure_node(node, topology_plan)

    pushns.assert_called_once_with(node.namespace)
    popns.assert_called_once_with()


def test_popns_failure_is_not_allowed_to_mask_primary_error(
    linux_node: NodePlan,
    topology_plan: TopologyPlan,
) -> None:
    primary = RuntimeError("sysctl write failed")
    cleanup = RuntimeError("pop failed")
    sysctl_path = Mock()
    sysctl_path.write_text.side_effect = primary
    sysctl_root = Mock()
    sysctl_root.joinpath.return_value = sysctl_path
    node = replace(linux_node, interfaces={}, routes=())
    backend = Pyroute2Backend(
        netns_factory=Mock(return_value=Mock()),
        pushns=Mock(),
        popns=Mock(side_effect=cleanup),
        sysctl_root=sysctl_root,
    )

    with pytest.raises(RuntimeError) as caught:
        backend.configure_node(node, topology_plan)

    assert caught.value is primary
    assert caught.value.__notes__ == ["namespace pop cleanup failed: RuntimeError('pop failed')"]


def test_popns_failure_propagates_without_primary_error(
    linux_node: NodePlan,
    topology_plan: TopologyPlan,
) -> None:
    cleanup = RuntimeError("pop failed")
    sysctl_path = Mock()
    sysctl_root = Mock()
    sysctl_root.joinpath.return_value = sysctl_path
    node = replace(linux_node, interfaces={}, routes=())
    backend = Pyroute2Backend(
        netns_factory=Mock(return_value=Mock()),
        pushns=Mock(),
        popns=Mock(side_effect=cleanup),
        sysctl_root=sysctl_root,
    )

    with pytest.raises(RuntimeError) as caught:
        backend.configure_node(node, topology_plan)

    assert caught.value is cleanup


def test_target_preexec_unblocks_exec_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pthread_sigmask = Mock(return_value=set())
    monkeypatch.setattr(pyroute2_backend.signal, "pthread_sigmask", pthread_sigmask)

    pyroute2_backend._unblock_exec_signals()

    pthread_sigmask.assert_called_once_with(
        signal.SIG_UNBLOCK,
        frozenset((signal.SIGINT, signal.SIGTERM)),
    )


def test_execute_blocks_proxy_signals_restores_mask_and_keeps_target_foreground(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_mask = {signal.SIGUSR1}
    mask_calls: list[tuple[int, object]] = []

    def pthread_sigmask(how: int, mask: object) -> set[int | signal.Signals]:
        mask_calls.append((how, mask))
        return previous_mask if how == signal.SIG_BLOCK else set()

    monkeypatch.setattr(pyroute2_backend.signal, "pthread_sigmask", pthread_sigmask)
    process = Mock(pid=321)
    process.communicate.return_value = ("", "")
    process.returncode = 0
    factory = Mock(return_value=process)
    backend = Pyroute2Backend(nspopen_factory=factory)

    backend.execute(linux_node.namespace, ("true",))

    assert mask_calls == [
        (signal.SIG_BLOCK, frozenset((signal.SIGINT, signal.SIGTERM))),
        (signal.SIG_SETMASK, previous_mask),
        (signal.SIG_BLOCK, frozenset((signal.SIGINT, signal.SIGTERM))),
        (signal.SIG_SETMASK, previous_mask),
    ]
    factory.assert_called_once_with(
        linux_node.namespace,
        [
            "/usr/bin/timeout",
            "--foreground",
            "--kill-after=1s",
            "--",
            "0",
            "true",
        ],
        stdout=PIPE,
        stderr=PIPE,
        text=True,
        shell=False,
        preexec_fn=pyroute2_backend._unblock_exec_signals,
    )


def test_spawn_context_keeps_signals_blocked_until_body_restores_before_wait(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_mask = {signal.SIGUSR1}
    events: list[tuple[str, ...]] = []

    def pthread_sigmask(how: int, _mask: object) -> set[int | signal.Signals]:
        event = "block" if how == signal.SIG_BLOCK else "restore"
        events.append((event,))
        return previous_mask if how == signal.SIG_BLOCK else set()

    monkeypatch.setattr(pyroute2_backend.signal, "pthread_sigmask", pthread_sigmask)
    monkeypatch.setattr(
        pyroute2_backend.os,
        "pidfd_open",
        lambda pid: events.append(("pidfd-open",)) or 81,
    )
    monkeypatch.setattr(
        pyroute2_backend.os,
        "close",
        lambda pidfd: events.append(("close",)),
    )
    process = Mock()
    type(process).pid = PropertyMock(side_effect=lambda: events.append(("pid",)) or "4321")
    process.communicate.side_effect = lambda: events.append(("communicate",)) or ("", "")
    process.release.side_effect = lambda: events.append(("release",))

    def factory(*_args: object, **_kwargs: object) -> Mock:
        events.append(("factory",))
        return process

    with pyroute2_backend._spawn_nspopen(
        factory,
        linux_node.namespace,
        ("true",),
        {"text": True, "shell": False},
    ) as spawned:
        events.append(("context-entered",))
        assert events == [
            ("block",),
            ("factory",),
            ("pid",),
            ("pidfd-open",),
            ("context-entered",),
        ]
        actual_process, restore_signal_mask, block_release_signals = spawned
        restore_signal_mask()
        actual_process.communicate()
        block_release_signals()

    assert events == [
        ("block",),
        ("factory",),
        ("pid",),
        ("pidfd-open",),
        ("context-entered",),
        ("restore",),
        ("communicate",),
        ("block",),
        ("release",),
        ("close",),
        ("restore",),
    ]


@pytest.mark.parametrize(
    ("cancellation", "expected_signal"),
    [
        (KeyboardInterrupt("pending SIGINT"), signal.SIGINT),
        (OperationCancelled("pending SIGTERM"), signal.SIGTERM),
    ],
)
def test_pending_cancellation_during_compensating_restore_wins_over_body_error(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
    cancellation: BaseException,
    expected_signal: signal.Signals,
) -> None:
    body_failure = RuntimeError("body failed before mask restore")
    pthread_sigmask = Mock(side_effect=[set(), cancellation, set(), set()])
    monkeypatch.setattr(pyroute2_backend.signal, "pthread_sigmask", pthread_sigmask)
    events: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pidfd_send_signal",
        lambda pidfd, signum: events.append(("pidfd-send-signal", pidfd, signum)),
    )
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", Mock(return_value=82))
    monkeypatch.setattr(
        pyroute2_backend.os,
        "close",
        lambda pidfd: events.append(("close", pidfd)),
    )
    raw_kill = Mock()
    monkeypatch.setattr(pyroute2_backend.os, "kill", raw_kill)
    process = Mock(pid=4321)
    process.release.side_effect = lambda: events.append(("release",))

    with (
        pytest.raises(BaseException) as caught,
        pyroute2_backend._spawn_nspopen(
            Mock(return_value=process),
            linux_node.namespace,
            ("true",),
            {"text": True, "shell": False},
        ),
    ):
        raise body_failure

    assert caught.value is cancellation
    assert caught.value.__cause__ is body_failure
    assert pthread_sigmask.call_count == 4
    assert events == [
        ("pidfd-send-signal", 82, expected_signal),
        ("release",),
        ("close", 82),
    ]
    raw_kill.assert_not_called()
    process.communicate.assert_not_called()


def test_execute_opens_pidfd_immediately_after_integer_pid_before_remote_communicate(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, ...]] = []

    def pthread_sigmask(how: int, _mask: object) -> set[int | signal.Signals]:
        event = "block" if how == signal.SIG_BLOCK else "restore"
        events.append((event,))
        return set()

    monkeypatch.setattr(pyroute2_backend.signal, "pthread_sigmask", pthread_sigmask)
    monkeypatch.setattr(
        pyroute2_backend.os,
        "pidfd_open",
        lambda pid: events.append(("pidfd-open",)) or 83,
    )
    monkeypatch.setattr(
        pyroute2_backend.os,
        "close",
        lambda pidfd: events.append(("close",)),
    )
    process = Mock()
    type(process).pid = PropertyMock(side_effect=lambda: events.append(("pid",)) or "4321")
    process.communicate.side_effect = lambda: events.append(("communicate",)) or ("", "")
    type(process).returncode = PropertyMock(side_effect=lambda: events.append(("returncode",)) or 0)
    process.release.side_effect = lambda: events.append(("release",))
    backend = Pyroute2Backend(nspopen_factory=Mock(return_value=process))

    backend.execute(linux_node.namespace, ("true",))

    assert events == [
        ("block",),
        ("pid",),
        ("pidfd-open",),
        ("restore",),
        ("communicate",),
        ("returncode",),
        ("block",),
        ("release",),
        ("close",),
        ("restore",),
    ]


def test_keyboard_interrupt_after_mask_restore_before_spawn_return_cleans_target(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt("after mask restore")
    pthread_sigmask = Mock(side_effect=[set(), set(), set()])
    monkeypatch.setattr(pyroute2_backend.signal, "pthread_sigmask", pthread_sigmask)
    original_restore = pyroute2_backend._restore_exec_signal_mask
    interrupt_armed = False
    restore_trace: Callable[[], None] | None = None

    def restore_and_interrupt(previous_mask: set[int | signal.Signals]) -> None:
        nonlocal interrupt_armed, restore_trace
        original_restore(previous_mask)
        if not interrupt_armed:
            interrupt_armed = True
            restore_trace = _raise_on_next_line(sys._getframe(1), interrupt)

    monkeypatch.setattr(
        pyroute2_backend,
        "_restore_exec_signal_mask",
        restore_and_interrupt,
    )
    events: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pidfd_send_signal",
        lambda pidfd, signum: events.append(("pidfd-send-signal", pidfd, signum)),
    )
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", Mock(return_value=84))
    monkeypatch.setattr(
        pyroute2_backend.os,
        "close",
        lambda pidfd: events.append(("close", pidfd)),
    )
    process = Mock(pid=4321)
    process.release.side_effect = lambda: events.append(("release",))
    backend = Pyroute2Backend(nspopen_factory=Mock(return_value=process))

    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            backend.execute(linux_node.namespace, ("true",))
    finally:
        if restore_trace is not None:
            restore_trace()

    assert caught.value is interrupt
    assert pthread_sigmask.call_count == 4
    assert events == [
        ("pidfd-send-signal", 84, signal.SIGINT),
        ("release",),
        ("close", 84),
    ]
    process.communicate.assert_not_called()


def test_operation_cancelled_after_spawn_return_before_wait_guard_cleans_target(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = OperationCancelled("after spawn return")
    pthread_sigmask = Mock(side_effect=[set(), set(), set(), set()])
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pthread_sigmask",
        pthread_sigmask,
    )
    events: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pidfd_send_signal",
        lambda pidfd, signum: events.append(("pidfd-send-signal", pidfd, signum)),
    )
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", Mock(return_value=85))
    monkeypatch.setattr(
        pyroute2_backend.os,
        "close",
        lambda pidfd: events.append(("close", pidfd)),
    )
    process = Mock(pid=4321)
    process.release.side_effect = lambda: events.append(("release",))
    restore_trace: Callable[[], None] | None = None

    def factory(*_args: object, **_kwargs: object) -> Mock:
        nonlocal restore_trace
        execute_frame = sys._getframe(1)
        while execute_frame.f_code is not Pyroute2Backend.execute.__code__:
            assert execute_frame.f_back is not None
            execute_frame = execute_frame.f_back
        restore_trace = _raise_on_next_line(execute_frame, cancellation)
        return process

    backend = Pyroute2Backend(nspopen_factory=factory)

    try:
        with pytest.raises(OperationCancelled) as caught:
            backend.execute(linux_node.namespace, ("true",))
    finally:
        if restore_trace is not None:
            restore_trace()

    assert caught.value is cancellation
    assert pthread_sigmask.call_count == 4
    assert events == [
        ("pidfd-send-signal", 85, signal.SIGTERM),
        ("release",),
        ("close", 85),
    ]
    process.communicate.assert_not_called()


def test_trace_probe_restores_existing_global_and_frame_trace_state() -> None:
    def suspended_frame() -> Iterator[FrameType]:
        yield sys._getframe()

    def global_trace(_frame: FrameType, _event: str, _arg: object):
        return global_trace

    def frame_trace(_frame: FrameType, _event: str, _arg: object):
        return frame_trace

    generator = suspended_frame()
    frame = next(generator)
    saved_global_trace = sys.gettrace()
    saved_frame_trace = frame.f_trace
    try:
        sys.settrace(global_trace)
        frame.f_trace = frame_trace
        restore_trace = _raise_on_next_line(frame, RuntimeError("not raised"))

        restore_trace()

        assert sys.gettrace() is global_trace
        assert frame.f_trace is frame_trace
    finally:
        frame.f_trace = saved_frame_trace
        sys.settrace(saved_global_trace)
        generator.close()


def test_triggered_trace_probe_restores_existing_global_and_frame_trace_state() -> None:
    def suspended_frame() -> Iterator[FrameType]:
        yield sys._getframe()
        _resumed = True

    def global_trace(_frame: FrameType, _event: str, _arg: object):
        return global_trace

    def frame_trace(_frame: FrameType, _event: str, _arg: object):
        return frame_trace

    failure = RuntimeError("injected failure")
    generator = suspended_frame()
    frame = next(generator)
    saved_global_trace = sys.gettrace()
    saved_frame_trace = frame.f_trace
    restore_trace: Callable[[], None] | None = None
    try:
        sys.settrace(global_trace)
        frame.f_trace = frame_trace
        restore_trace = _raise_on_next_line(frame, failure)

        with pytest.raises(RuntimeError) as caught:
            next(generator)

        assert caught.value is failure
    finally:
        if restore_trace is not None:
            restore_trace()

    try:
        assert sys.gettrace() is global_trace
        assert frame.f_trace is frame_trace
    finally:
        frame.f_trace = saved_frame_trace
        sys.settrace(saved_global_trace)
        generator.close()


def test_cancellation_at_generator_normal_exit_releases_and_closes_before_delivery(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt("normal exit interrupted")
    events: list[tuple[str, ...]] = []

    def pthread_sigmask(how: int, _mask: object) -> set[int | signal.Signals]:
        events.append(("block",) if how == signal.SIG_BLOCK else ("restore",))
        return set()

    monkeypatch.setattr(pyroute2_backend.signal, "pthread_sigmask", pthread_sigmask)
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", Mock(return_value=86))
    monkeypatch.setattr(
        pyroute2_backend.os,
        "close",
        lambda _pidfd: events.append(("close",)),
    )
    pidfd_send_signal = Mock()
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pidfd_send_signal",
        pidfd_send_signal,
    )
    process = Mock(pid=4321)
    process.release.side_effect = lambda: events.append(("release",))
    manager = pyroute2_backend._spawn_nspopen(
        Mock(return_value=process),
        linux_node.namespace,
        ("true",),
        {"text": True, "shell": False},
    )
    restore_trace: Callable[[], None] | None = None

    try:
        with (
            pytest.raises(KeyboardInterrupt) as caught,
            manager as (
                _actual_process,
                restore_signal_mask,
                block_release_signals,
            ),
        ):
            restore_signal_mask()
            block_release_signals()
            generator_frame = manager.gen.gi_frame
            assert generator_frame is not None
            restore_trace = _raise_on_next_line(generator_frame, interrupt)
    finally:
        if restore_trace is not None:
            restore_trace()

    assert caught.value is interrupt
    assert events == [
        ("block",),
        ("restore",),
        ("block",),
        ("release",),
        ("close",),
        ("restore",),
    ]
    pidfd_send_signal.assert_not_called()


def test_cancellation_inside_normal_release_still_closes_and_restores_mask(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt("release interrupted")
    events: list[tuple[str, ...]] = []

    def pthread_sigmask(how: int, _mask: object) -> set[int | signal.Signals]:
        events.append(("block",) if how == signal.SIG_BLOCK else ("restore",))
        return set()

    monkeypatch.setattr(pyroute2_backend.signal, "pthread_sigmask", pthread_sigmask)
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", Mock(return_value=87))
    monkeypatch.setattr(
        pyroute2_backend.os,
        "close",
        lambda _pidfd: events.append(("close",)),
    )
    pidfd_send_signal = Mock()
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pidfd_send_signal",
        pidfd_send_signal,
    )
    process = Mock(pid=4321)
    process.communicate.return_value = ("", "")
    process.returncode = 0

    def release() -> None:
        events.append(("release",))
        raise interrupt

    process.release.side_effect = release
    backend = Pyroute2Backend(nspopen_factory=Mock(return_value=process))

    with pytest.raises(KeyboardInterrupt) as caught:
        backend.execute(linux_node.namespace, ("true",))

    assert caught.value is interrupt
    assert events == [
        ("block",),
        ("restore",),
        ("block",),
        ("release",),
        ("close",),
        ("restore",),
    ]
    pidfd_send_signal.assert_not_called()


def test_pending_cancellation_on_final_restore_arrives_after_release_and_close(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = OperationCancelled("pending final SIGTERM")
    events: list[tuple[str, ...]] = []
    mask_call_count = 0

    def pthread_sigmask(how: int, _mask: object) -> set[int | signal.Signals]:
        nonlocal mask_call_count
        mask_call_count += 1
        events.append(("block",) if how == signal.SIG_BLOCK else ("restore",))
        if mask_call_count == 4:
            raise cancellation
        return set()

    monkeypatch.setattr(pyroute2_backend.signal, "pthread_sigmask", pthread_sigmask)
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", Mock(return_value=88))
    monkeypatch.setattr(
        pyroute2_backend.os,
        "close",
        lambda _pidfd: events.append(("close",)),
    )
    pidfd_send_signal = Mock()
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pidfd_send_signal",
        pidfd_send_signal,
    )
    process = Mock(pid=4321)
    process.communicate.return_value = ("", "")
    process.returncode = 0
    process.release.side_effect = lambda: events.append(("release",))
    backend = Pyroute2Backend(nspopen_factory=Mock(return_value=process))

    with pytest.raises(OperationCancelled) as caught:
        backend.execute(linux_node.namespace, ("true",))

    assert caught.value is cancellation
    assert events == [
        ("block",),
        ("restore",),
        ("block",),
        ("release",),
        ("close",),
        ("restore",),
    ]
    pidfd_send_signal.assert_not_called()


def test_execute_uses_timeout_trampoline_captures_streams_and_releases_process(
    linux_node: NodePlan,
) -> None:
    process = Mock(pid=321)
    process.communicate.return_value = ("stdout text\n", "stderr text\n")
    process.returncode = 7
    nspopen_factory = Mock(return_value=process)
    backend = Pyroute2Backend(nspopen_factory=nspopen_factory)
    argv = ("printf", "%s", "hello; not a shell")

    result = backend.execute(linux_node.namespace, argv)

    nspopen_factory.assert_called_once_with(
        linux_node.namespace,
        [
            "/usr/bin/timeout",
            "--foreground",
            "--kill-after=1s",
            "--",
            "0",
            *argv,
        ],
        stdout=PIPE,
        stderr=PIPE,
        text=True,
        shell=False,
        preexec_fn=pyroute2_backend._unblock_exec_signals,
    )
    process.communicate.assert_called_once_with()
    process.release.assert_called_once_with()
    assert result == ExecResult(
        argv=argv,
        returncode=7,
        stdout="stdout text\n",
        stderr="stderr text\n",
    )


def test_execute_passthrough_inherits_streams_and_returns_empty_text(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin_fd = 71
    timeout_pidfd = 72
    dup = Mock(return_value=stdin_fd)
    close = Mock()
    monkeypatch.setattr(pyroute2_backend.os, "dup", dup)
    monkeypatch.setattr(
        pyroute2_backend.os,
        "pidfd_open",
        Mock(return_value=timeout_pidfd),
    )
    monkeypatch.setattr(pyroute2_backend.os, "close", close)
    process = Mock(pid=322)
    process.communicate.return_value = (None, None)
    process.returncode = 0
    factory = Mock(return_value=process)
    backend = Pyroute2Backend(nspopen_factory=factory)
    argv = ("iperf3", "-s")

    result = backend.execute(linux_node.namespace, argv, capture_output=False)

    factory.assert_called_once_with(
        linux_node.namespace,
        [
            "/usr/bin/timeout",
            "--foreground",
            "--kill-after=1s",
            "--",
            "0",
            *argv,
        ],
        stdin=stdin_fd,
        text=True,
        shell=False,
        preexec_fn=pyroute2_backend._unblock_exec_signals,
    )
    dup.assert_called_once_with(0)
    assert close.call_args_list == [call(stdin_fd), call(timeout_pidfd)]
    assert result == ExecResult(argv=argv, returncode=0, stdout="", stderr="")
    process.communicate.assert_called_once_with()
    process.release.assert_called_once_with()


def test_passthrough_stdin_duplicate_closes_when_factory_fails(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin_fd = 73
    failure = OSError(errno.EPERM, "namespace access denied")
    dup = Mock(return_value=stdin_fd)
    close = Mock()
    monkeypatch.setattr(pyroute2_backend.os, "dup", dup)
    monkeypatch.setattr(pyroute2_backend.os, "close", close)
    factory = Mock(side_effect=failure)
    backend = Pyroute2Backend(nspopen_factory=factory)

    with pytest.raises(NslabError) as caught:
        backend.execute(linux_node.namespace, ("true",), capture_output=False)

    assert caught.value.__cause__ is failure
    assert factory.call_args.kwargs["stdin"] == stdin_fd
    dup.assert_called_once_with(0)
    close.assert_called_once_with(stdin_fd)


def test_passthrough_stdin_dup_error_is_untranslated_and_does_not_spawn(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = OSError(errno.EBADF, "stdin is closed")
    dup = Mock(side_effect=failure)
    close = Mock()
    monkeypatch.setattr(pyroute2_backend.os, "dup", dup)
    monkeypatch.setattr(pyroute2_backend.os, "close", close)
    factory = Mock()
    backend = Pyroute2Backend(nspopen_factory=factory)

    with pytest.raises(OSError) as caught:
        backend.execute(linux_node.namespace, ("true",), capture_output=False)

    assert caught.value is failure
    dup.assert_called_once_with(0)
    factory.assert_not_called()
    close.assert_not_called()


def test_execute_missing_executable_returns_stable_nonzero_result(
    linux_node: NodePlan,
) -> None:
    process = Mock(pid=323)
    process.communicate.return_value = (
        "",
        "timeout: failed to run missing command\n",
    )
    process.returncode = 127
    nspopen_factory = Mock(return_value=process)
    backend = Pyroute2Backend(nspopen_factory=nspopen_factory)
    argv = ("nslab-command-does-not-exist", "argument")

    result = backend.execute(linux_node.namespace, argv)

    nspopen_factory.assert_called_once_with(
        linux_node.namespace,
        [
            "/usr/bin/timeout",
            "--foreground",
            "--kill-after=1s",
            "--",
            "0",
            *argv,
        ],
        stdout=PIPE,
        stderr=PIPE,
        text=True,
        shell=False,
        preexec_fn=pyroute2_backend._unblock_exec_signals,
    )
    assert result == ExecResult(
        argv=argv,
        returncode=127,
        stdout="",
        stderr="timeout: failed to run missing command\n",
    )


def test_execute_assignment_like_argv_is_not_interpreted_as_environment(
    linux_node: NodePlan,
) -> None:
    process = Mock(pid=324)
    process.communicate.return_value = (
        "",
        "timeout: failed to run command 'FOO=bar'\n",
    )
    process.returncode = 126
    nspopen_factory = Mock(return_value=process)
    backend = Pyroute2Backend(nspopen_factory=nspopen_factory)
    argv = ("FOO=bar", "/usr/bin/printenv", "FOO")

    result = backend.execute(linux_node.namespace, argv)

    nspopen_factory.assert_called_once_with(
        linux_node.namespace,
        [
            "/usr/bin/timeout",
            "--foreground",
            "--kill-after=1s",
            "--",
            "0",
            *argv,
        ],
        stdout=PIPE,
        stderr=PIPE,
        text=True,
        shell=False,
        preexec_fn=pyroute2_backend._unblock_exec_signals,
    )
    assert result == ExecResult(
        argv=argv,
        returncode=126,
        stdout="",
        stderr="timeout: failed to run command 'FOO=bar'\n",
    )


def test_execute_empty_argv_returns_stable_nonzero_result(
    linux_node: NodePlan,
) -> None:
    process = Mock(pid=325)
    process.communicate.return_value = ("", "timeout: missing operand\n")
    process.returncode = 125
    nspopen_factory = Mock(return_value=process)
    backend = Pyroute2Backend(nspopen_factory=nspopen_factory)

    result = backend.execute(linux_node.namespace, ())

    nspopen_factory.assert_called_once_with(
        linux_node.namespace,
        [
            "/usr/bin/timeout",
            "--foreground",
            "--kill-after=1s",
            "--",
            "0",
        ],
        stdout=PIPE,
        stderr=PIPE,
        text=True,
        shell=False,
        preexec_fn=pyroute2_backend._unblock_exec_signals,
    )
    assert result == ExecResult(
        argv=(),
        returncode=125,
        stdout="",
        stderr="timeout: missing operand\n",
    )


@pytest.mark.parametrize(
    ("failure", "expected_signal"),
    [
        (KeyboardInterrupt("communicate interrupted"), signal.SIGINT),
        (OperationCancelled("SIGTERM cancellation"), signal.SIGTERM),
        (RuntimeError("communication failed"), signal.SIGTERM),
    ],
)
def test_execute_signals_target_before_release_after_wait_failure(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected_signal: signal.Signals,
) -> None:
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pthread_sigmask",
        Mock(return_value=set()),
    )
    events: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pidfd_send_signal",
        lambda pidfd, signum: events.append(("pidfd-send-signal", pidfd, signum)),
    )
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", Mock(return_value=90))
    monkeypatch.setattr(
        pyroute2_backend.os,
        "close",
        lambda pidfd: events.append(("close", pidfd)),
    )
    raw_kill = Mock()
    monkeypatch.setattr(pyroute2_backend.os, "kill", raw_kill)
    process = Mock(pid=4321)
    process.communicate.side_effect = failure
    process.release.side_effect = lambda: events.append(("release",))
    backend = Pyroute2Backend(nspopen_factory=Mock(return_value=process))

    with pytest.raises(BaseException) as caught:
        backend.execute(linux_node.namespace, ("iperf3", "-s"))

    assert caught.value is failure
    assert events == [
        ("pidfd-send-signal", 90, expected_signal),
        ("release",),
        ("close", 90),
    ]
    raw_kill.assert_not_called()


def test_execute_still_releases_when_interrupted_target_is_already_absent(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pthread_sigmask",
        Mock(return_value=set()),
    )
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", Mock(return_value=91))
    pidfd_send_signal = Mock(side_effect=ProcessLookupError)
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pidfd_send_signal",
        pidfd_send_signal,
    )
    close = Mock()
    monkeypatch.setattr(pyroute2_backend.os, "close", close)
    raw_kill = Mock()
    monkeypatch.setattr(pyroute2_backend.os, "kill", raw_kill)
    interrupt = KeyboardInterrupt("target exited")
    process = Mock(pid=4321)
    process.communicate.side_effect = interrupt
    backend = Pyroute2Backend(nspopen_factory=Mock(return_value=process))

    with pytest.raises(KeyboardInterrupt) as caught:
        backend.execute(linux_node.namespace, ("true",))

    assert caught.value is interrupt
    pidfd_send_signal.assert_called_once_with(91, signal.SIGINT)
    process.release.assert_called_once_with()
    close.assert_called_once_with(91)
    raw_kill.assert_not_called()


def test_pid_read_failure_terminates_proxy_before_release(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pthread_sigmask",
        Mock(return_value=set()),
    )
    pidfd_open = Mock()
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", pidfd_open)
    failure = ValueError("invalid timeout pid")
    events: list[tuple[str, ...]] = []
    process = Mock()
    type(process).pid = PropertyMock(side_effect=failure)
    process.terminate.side_effect = lambda: events.append(("terminate",))
    process.release.side_effect = lambda: events.append(("release",))
    backend = Pyroute2Backend(nspopen_factory=Mock(return_value=process))

    with pytest.raises(ValueError) as caught:
        backend.execute(linux_node.namespace, ("true",))

    assert caught.value is failure
    assert events == [("terminate",), ("release",)]
    pidfd_open.assert_not_called()
    process.communicate.assert_not_called()


@pytest.mark.parametrize("pid", [0, -1])
def test_nonpositive_pid_terminates_proxy_before_release(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
    pid: int,
) -> None:
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pthread_sigmask",
        Mock(return_value=set()),
    )
    pidfd_open = Mock()
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", pidfd_open)
    events: list[tuple[str, ...]] = []
    process = Mock(pid=pid)
    process.terminate.side_effect = lambda: events.append(("terminate",))
    process.release.side_effect = lambda: events.append(("release",))
    backend = Pyroute2Backend(nspopen_factory=Mock(return_value=process))

    with pytest.raises(ValueError, match="positive"):
        backend.execute(linux_node.namespace, ("true",))

    assert events == [("terminate",), ("release",)]
    pidfd_open.assert_not_called()
    process.communicate.assert_not_called()


def test_pidfd_open_failure_terminates_proxy_before_release_and_preserves_error(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pthread_sigmask",
        Mock(return_value=set()),
    )
    failure = OSError(errno.EMFILE, "pidfd table full")
    pidfd_open = Mock(side_effect=failure)
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", pidfd_open)
    close = Mock()
    monkeypatch.setattr(pyroute2_backend.os, "close", close)
    events: list[tuple[str, ...]] = []
    process = Mock(pid=4321)
    process.terminate.side_effect = lambda: events.append(("terminate",))
    process.release.side_effect = lambda: events.append(("release",))
    backend = Pyroute2Backend(nspopen_factory=Mock(return_value=process))

    with pytest.raises(OSError) as caught:
        backend.execute(linux_node.namespace, ("true",))

    assert caught.value is failure
    assert events == [("terminate",), ("release",)]
    pidfd_open.assert_called_once_with(4321)
    close.assert_not_called()
    process.communicate.assert_not_called()


def test_pending_interrupt_during_mask_restore_signals_target_before_release(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt("pending SIGINT")
    pthread_sigmask = Mock(side_effect=[{signal.SIGUSR1}, interrupt, set(), set()])
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pthread_sigmask",
        pthread_sigmask,
    )
    events: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pidfd_send_signal",
        lambda pidfd, signum: events.append(("pidfd-send-signal", pidfd, signum)),
    )
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", Mock(return_value=92))
    monkeypatch.setattr(
        pyroute2_backend.os,
        "close",
        lambda pidfd: events.append(("close", pidfd)),
    )
    process = Mock(pid=4321)
    process.release.side_effect = lambda: events.append(("release",))
    backend = Pyroute2Backend(nspopen_factory=Mock(return_value=process))

    with pytest.raises(KeyboardInterrupt) as caught:
        backend.execute(linux_node.namespace, ("true",))

    assert caught.value is interrupt
    assert pthread_sigmask.call_count == 4
    assert events == [
        ("pidfd-send-signal", 92, signal.SIGINT),
        ("release",),
        ("close", 92),
    ]
    process.communicate.assert_not_called()


def test_pending_cancellation_during_mask_restore_wins_over_cleanup_failures(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = OperationCancelled("pending SIGTERM")
    target_cleanup = PermissionError("kill denied")
    release_cleanup = RuntimeError("release failed")
    close_cleanup = OSError(errno.EBADF, "close failed")
    pthread_sigmask = Mock(side_effect=[set(), cancellation, set(), set()])
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pthread_sigmask",
        pthread_sigmask,
    )
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", Mock(return_value=93))
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pidfd_send_signal",
        Mock(side_effect=target_cleanup),
    )
    close = Mock(side_effect=close_cleanup)
    monkeypatch.setattr(pyroute2_backend.os, "close", close)
    process = Mock(pid=4321)
    process.release.side_effect = release_cleanup
    backend = Pyroute2Backend(nspopen_factory=Mock(return_value=process))

    with pytest.raises(OperationCancelled) as caught:
        backend.execute(linux_node.namespace, ("true",))

    assert caught.value is cancellation
    assert pthread_sigmask.call_count == 4
    assert caught.value.__notes__ == [
        "exec target termination cleanup failed: PermissionError('kill denied')",
        "NSPopen release cleanup failed: RuntimeError('release failed')",
        "exec pidfd close cleanup failed: OSError(9, 'close failed')",
    ]
    close.assert_called_once_with(93)
    process.communicate.assert_not_called()


def test_mask_restore_os_error_is_not_translated_as_namespace_failure(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = OSError(errno.EINVAL, "mask restore failed")
    pthread_sigmask = Mock(side_effect=[set(), failure, set(), set()])
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pthread_sigmask",
        pthread_sigmask,
    )
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", Mock(return_value=94))
    pidfd_send_signal = Mock()
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pidfd_send_signal",
        pidfd_send_signal,
    )
    close = Mock()
    monkeypatch.setattr(pyroute2_backend.os, "close", close)
    process = Mock(pid=4321)
    backend = Pyroute2Backend(nspopen_factory=Mock(return_value=process))

    with pytest.raises(OSError) as caught:
        backend.execute(linux_node.namespace, ("true",))

    assert caught.value is failure
    assert pthread_sigmask.call_count == 4
    pidfd_send_signal.assert_called_once_with(94, signal.SIGTERM)
    process.release.assert_called_once_with()
    close.assert_called_once_with(94)
    process.communicate.assert_not_called()


def test_pending_cancellation_wins_over_factory_failure(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_failure = OSError(errno.ENOENT, "namespace disappeared")
    cancellation = OperationCancelled("pending SIGTERM")
    pthread_sigmask = Mock(side_effect=[set(), cancellation, set()])
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pthread_sigmask",
        pthread_sigmask,
    )
    factory = Mock(side_effect=factory_failure)
    backend = Pyroute2Backend(nspopen_factory=factory)

    with pytest.raises(OperationCancelled) as caught:
        backend.execute(linux_node.namespace, ("true",))

    assert caught.value is cancellation
    assert pthread_sigmask.call_count == 3


def test_initial_mask_os_error_is_not_translated_or_spawned(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = OSError(errno.EINVAL, "mask block failed")
    monkeypatch.setattr(
        pyroute2_backend.signal,
        "pthread_sigmask",
        Mock(side_effect=failure),
    )
    factory = Mock()
    backend = Pyroute2Backend(nspopen_factory=factory)

    with pytest.raises(OSError) as caught:
        backend.execute(linux_node.namespace, ("true",))

    assert caught.value is failure
    factory.assert_not_called()


def test_execute_releases_process_when_stream_capture_fails(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = RuntimeError("capture failed")
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", Mock(return_value=95))
    raw_kill = Mock()
    monkeypatch.setattr(pyroute2_backend.os, "kill", raw_kill)
    close = Mock()
    monkeypatch.setattr(pyroute2_backend.os, "close", close)
    process = Mock(pid=326)
    process.communicate.side_effect = failure
    backend = Pyroute2Backend(nspopen_factory=Mock(return_value=process))

    with pytest.raises(RuntimeError) as caught:
        backend.execute(linux_node.namespace, ("true",))

    assert caught.value is failure
    process.release.assert_called_once_with()
    close.assert_called_once_with(95)
    raw_kill.assert_not_called()


def test_execute_preserves_communicate_os_error_without_namespace_translation(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = OSError(errno.ENOENT, "capture path missing")
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", Mock(return_value=96))
    raw_kill = Mock()
    monkeypatch.setattr(pyroute2_backend.os, "kill", raw_kill)
    close = Mock()
    monkeypatch.setattr(pyroute2_backend.os, "close", close)
    process = Mock(pid=327)
    process.communicate.side_effect = failure
    backend = Pyroute2Backend(nspopen_factory=Mock(return_value=process))

    with pytest.raises(OSError) as caught:
        backend.execute(linux_node.namespace, ("true",))

    assert caught.value is failure
    process.release.assert_called_once_with()
    close.assert_called_once_with(96)
    raw_kill.assert_not_called()


def test_execute_preserves_release_os_error_without_namespace_translation(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = OSError(errno.ENOENT, "release path missing")
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", Mock(return_value=97))
    close = Mock()
    monkeypatch.setattr(pyroute2_backend.os, "close", close)
    process = Mock(pid=328)
    process.communicate.return_value = ("", "")
    process.returncode = 0
    process.release.side_effect = failure
    backend = Pyroute2Backend(nspopen_factory=Mock(return_value=process))

    with pytest.raises(OSError) as caught:
        backend.execute(linux_node.namespace, ("true",))

    assert caught.value is failure
    process.communicate.assert_called_once_with()
    close.assert_called_once_with(97)


def test_release_failure_is_not_allowed_to_mask_primary_error(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = RuntimeError("capture failed")
    cleanup = RuntimeError("release failed")
    close_cleanup = OSError(errno.EBADF, "pidfd close failed")
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", Mock(return_value=98))
    raw_kill = Mock()
    monkeypatch.setattr(pyroute2_backend.os, "kill", raw_kill)
    close = Mock(side_effect=close_cleanup)
    monkeypatch.setattr(pyroute2_backend.os, "close", close)
    process = Mock(pid=329)
    process.communicate.side_effect = primary
    process.release.side_effect = cleanup
    backend = Pyroute2Backend(nspopen_factory=Mock(return_value=process))

    with pytest.raises(RuntimeError) as caught:
        backend.execute(linux_node.namespace, ("true",))

    assert caught.value is primary
    assert caught.value.__notes__ == [
        "NSPopen release cleanup failed: RuntimeError('release failed')",
        "exec pidfd close cleanup failed: OSError(9, 'pidfd close failed')",
    ]
    close.assert_called_once_with(98)
    raw_kill.assert_not_called()


def test_release_failure_propagates_without_primary_error(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup = RuntimeError("release failed")
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", Mock(return_value=99))
    close = Mock()
    monkeypatch.setattr(pyroute2_backend.os, "close", close)
    process = Mock(pid=330)
    process.communicate.return_value = ("", "")
    process.returncode = 0
    process.release.side_effect = cleanup
    backend = Pyroute2Backend(nspopen_factory=Mock(return_value=process))

    with pytest.raises(RuntimeError) as caught:
        backend.execute(linux_node.namespace, ("true",))

    assert caught.value is cleanup
    close.assert_called_once_with(99)


def test_normal_pidfd_close_failure_propagates_after_release_and_mask_restore(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = OSError(errno.EBADF, "pidfd close failed")
    events: list[tuple[str, ...]] = []

    def pthread_sigmask(how: int, _mask: object) -> set[int | signal.Signals]:
        events.append(("block",) if how == signal.SIG_BLOCK else ("restore",))
        return set()

    monkeypatch.setattr(pyroute2_backend.signal, "pthread_sigmask", pthread_sigmask)
    monkeypatch.setattr(pyroute2_backend.os, "pidfd_open", Mock(return_value=100))

    def close(_pidfd: int) -> None:
        events.append(("close",))
        raise failure

    monkeypatch.setattr(pyroute2_backend.os, "close", close)
    process = Mock(pid=4321)
    process.communicate.return_value = ("", "")
    process.returncode = 0
    process.release.side_effect = lambda: events.append(("release",))
    backend = Pyroute2Backend(nspopen_factory=Mock(return_value=process))

    with pytest.raises(OSError) as caught:
        backend.execute(linux_node.namespace, ("true",))

    assert caught.value is failure
    assert events == [
        ("block",),
        ("restore",),
        ("block",),
        ("release",),
        ("close",),
        ("restore",),
    ]


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (FileNotFoundError(errno.ENOENT, "namespace missing"), "RESOURCE_MISSING"),
        (OSError(errno.EPERM, "namespace access denied"), "NETLINK_ERROR"),
    ],
)
def test_execute_translates_os_errors(
    linux_node: NodePlan,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
    expected_code: str,
) -> None:
    pthread_sigmask = Mock(side_effect=[{signal.SIGUSR1}, set()])
    monkeypatch.setattr(pyroute2_backend.signal, "pthread_sigmask", pthread_sigmask)
    backend = Pyroute2Backend(nspopen_factory=Mock(side_effect=failure))

    with pytest.raises(NslabError) as caught:
        backend.execute(linux_node.namespace, ("true",))

    assert caught.value.code == expected_code
    assert caught.value.details == {
        "errno": failure.errno,
        "operation": "execute",
        "resource": linux_node.namespace,
    }
    assert caught.value.__cause__ is failure
    assert pthread_sigmask.call_args_list == [
        call(signal.SIG_BLOCK, frozenset((signal.SIGINT, signal.SIGTERM))),
        call(signal.SIG_SETMASK, {signal.SIGUSR1}),
    ]


def test_inventory_opens_only_planned_namespaces_and_records_missing_explicitly(
    topology_plan: TopologyPlan,
) -> None:
    missing = topology_plan.nodes["h1"]
    present = topology_plan.nodes["sw1"]
    present_handle = Mock()
    present_handle.get_links.return_value = []
    present_handle.get_addr.return_value = []
    present_handle.get_routes.return_value = []

    def netns_factory(namespace: str) -> Mock:
        if namespace == missing.namespace:
            raise FileNotFoundError(namespace)
        assert namespace == present.namespace
        return present_handle

    root = Mock()
    root.link_lookup.return_value = []
    iproute_factory = Mock(return_value=root)
    backend = Pyroute2Backend(
        iproute_factory=iproute_factory,
        netns_factory=netns_factory,
    )

    inventory = backend.inventory(topology_plan)

    assert tuple(inventory.namespaces) == (missing.namespace, present.namespace)
    absent = inventory.namespaces[missing.namespace]
    assert absent.node == missing.name
    assert absent.kind == missing.kind
    assert absent.namespace == missing.namespace
    assert absent.exists is False
    assert absent.interfaces == {}
    assert absent.routes == ()
    assert absent.sysctls == {}
    assert inventory.namespaces[present.namespace].exists is True
    present_handle.close.assert_called_once_with()
    iproute_factory.assert_called_once_with()
    assert root.link_lookup.call_args_list == [
        call(ifname=endpoint.temporary_name)
        for link in topology_plan.links
        for endpoint in (link.left, link.right)
    ]
    root.get_links.assert_not_called()
    root.close.assert_called_once_with()


def test_inventory_probes_only_exact_planned_root_names_and_records_leftover(
    topology_plan: TopologyPlan,
) -> None:
    left, right = (endpoint for link in topology_plan.links for endpoint in (link.left, link.right))
    root = Mock()
    root.link_lookup.side_effect = [[], [88]]
    root.get_links.return_value = [
        _link_message(
            88,
            right.temporary_name,
            "veth",
            1450,
            up=False,
            alias="leftover-link-id",
        )
    ]
    netns_factory = Mock(side_effect=FileNotFoundError(errno.ENOENT, "missing"))
    backend = Pyroute2Backend(
        iproute_factory=Mock(return_value=root),
        netns_factory=netns_factory,
    )

    inventory = backend.inventory(topology_plan)

    assert tuple(inventory.root_interfaces) == (right.temporary_name,)
    leftover = inventory.root_interfaces[right.temporary_name]
    assert leftover.name == right.temporary_name
    assert leftover.kind == "veth"
    assert leftover.ifindex == 88
    assert leftover.mtu == 1450
    assert leftover.up is False
    assert leftover.link_id == "leftover-link-id"
    assert root.mock_calls == [
        call.link_lookup(ifname=left.temporary_name),
        call.link_lookup(ifname=right.temporary_name),
        call.get_links(88),
        call.close(),
    ]


def test_inventory_ignores_reused_root_ifindex_with_a_different_name(
    topology_plan: TopologyPlan,
) -> None:
    endpoint = topology_plan.links[0].left
    root = Mock()
    root.link_lookup.side_effect = [[88], [], [], []]
    root.get_links.return_value = [
        _link_message(
            88,
            "foreign0",
            "veth",
            1500,
            up=True,
            alias="foreign-link-id",
        )
    ]
    backend = Pyroute2Backend(
        iproute_factory=Mock(return_value=root),
        netns_factory=Mock(side_effect=FileNotFoundError(errno.ENOENT, "missing")),
    )

    inventory = backend.inventory(topology_plan)

    assert inventory.root_interfaces == {}
    assert root.mock_calls[:2] == [
        call.link_lookup(ifname=endpoint.temporary_name),
        call.get_links(88),
    ]
    root.close.assert_called_once_with()


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError(errno.ENOENT, "root interface disappeared"),
        OSError(errno.ENODEV, "root interface disappeared"),
        NetlinkError(errno.ENOENT, "root interface disappeared"),
    ],
)
def test_inventory_treats_root_probe_disappearance_as_absent(
    topology_plan: TopologyPlan,
    failure: OSError | NetlinkError,
) -> None:
    root = Mock()
    root.link_lookup.side_effect = [[88], [], [], []]
    root.get_links.side_effect = failure
    backend = Pyroute2Backend(
        iproute_factory=Mock(return_value=root),
        netns_factory=Mock(side_effect=FileNotFoundError(errno.ENOENT, "missing")),
    )

    inventory = backend.inventory(topology_plan)

    assert inventory.root_interfaces == {}
    root.close.assert_called_once_with()


def test_inventory_closes_root_handle_and_translates_exact_probe_error(
    topology_plan: TopologyPlan,
) -> None:
    endpoint = topology_plan.links[0].left
    failure = NetlinkError(errno.EPERM, "root inventory denied")
    root = Mock()
    root.link_lookup.side_effect = failure
    netns_factory = Mock()
    backend = Pyroute2Backend(
        iproute_factory=Mock(return_value=root),
        netns_factory=netns_factory,
    )

    with pytest.raises(NslabError) as caught:
        backend.inventory(topology_plan)

    assert caught.value.code == "NETLINK_ERROR"
    assert caught.value.details == {
        "errno": errno.EPERM,
        "operation": "inventory",
        "resource": f"root:{endpoint.temporary_name}",
    }
    assert caught.value.__cause__ is failure
    root.close.assert_called_once_with()
    netns_factory.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "error_number"),
    [
        (FileNotFoundError(errno.ENOENT, "route probe path missing"), errno.ENOENT),
        (OSError(errno.ENODEV, "route probe device missing"), errno.ENODEV),
        (NetlinkError(errno.ENOENT, "route probe missing"), errno.ENOENT),
    ],
)
def test_inventory_translates_missing_route_probe_after_namespace_open(
    linux_node: NodePlan,
    failure: OSError | NetlinkError,
    error_number: int,
) -> None:
    node = replace(linux_node, sysctls={})
    plan = TopologyPlan(
        name="inventory-route-probe-error",
        fingerprint="inventory-route-probe-error",
        nodes={node.name: node},
        links=(),
    )
    handle = Mock()
    handle.get_links.return_value = []
    handle.get_addr.return_value = []
    handle.get_routes.side_effect = failure
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    with pytest.raises(NslabError) as caught:
        backend.inventory(plan)

    assert caught.value.code == "RESOURCE_MISSING"
    assert caught.value.details == {
        "errno": error_number,
        "operation": "inventory",
        "resource": node.namespace,
    }
    assert caught.value.__cause__ is failure
    handle.close.assert_called_once_with()


def test_inventory_translates_missing_sysctl_after_namespace_open(
    tmp_path: Path,
    linux_node: NodePlan,
) -> None:
    plan = TopologyPlan(
        name="inventory-sysctl-error",
        fingerprint="inventory-sysctl-error",
        nodes={linux_node.name: linux_node},
        links=(),
    )
    handle = Mock()
    handle.get_links.return_value = []
    handle.get_addr.return_value = []
    handle.get_routes.return_value = []
    pushns = Mock()
    popns = Mock()
    backend = Pyroute2Backend(
        netns_factory=Mock(return_value=handle),
        pushns=pushns,
        popns=popns,
        sysctl_root=tmp_path,
    )

    with pytest.raises(NslabError) as caught:
        backend.inventory(plan)

    assert caught.value.code == "RESOURCE_MISSING"
    assert caught.value.details == {
        "errno": errno.ENOENT,
        "operation": "inventory",
        "resource": linux_node.namespace,
    }
    assert isinstance(caught.value.__cause__, FileNotFoundError)
    handle.close.assert_called_once_with()
    pushns.assert_called_once_with(linux_node.namespace)
    popns.assert_called_once_with()


@pytest.mark.parametrize(
    ("route_type", "reason"),
    [
        pytest.param(2, "local", id="local"),
        pytest.param(3, "broadcast", id="broadcast"),
        pytest.param(4, "anycast", id="anycast"),
        pytest.param(5, "multicast", id="multicast"),
    ],
)
def test_inventory_rejects_main_table_special_route_types(
    linux_node: NodePlan,
    route_type: int,
    reason: str,
) -> None:
    node = replace(linux_node, sysctls={})
    plan = TopologyPlan(
        name="main-table-special-route",
        fingerprint="main-table-special-route",
        nodes={node.name: node},
        links=(),
    )
    handle = Mock()
    handle.get_links.return_value = [_link_message(10, "eth0", "veth", 1450)]
    handle.get_addr.return_value = []
    handle.get_routes.return_value = [_route_message("192.0.2.0", 24, 10, route_type=route_type)]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    with pytest.raises(NslabError) as caught:
        backend.inventory(plan)

    assert caught.value.as_dict() == {
        "code": "INVENTORY_UNSUPPORTED",
        "message": f"unsupported route in network inventory: {node.namespace}",
        "details": {
            "operation": "inventory",
            "resource": node.namespace,
            "reason": reason,
        },
    }


@pytest.mark.parametrize(
    "route",
    [
        pytest.param(
            _route_message(
                "10.10.0.0",
                24,
                10,
                proto=4,
                scope=253,
                extra_attrs=(("RTA_PREFSRC", "10.10.0.1"),),
            ),
            id="static-protocol",
        ),
        pytest.param(
            _route_message(
                "10.10.0.0",
                24,
                10,
                proto=2,
                scope=0,
                extra_attrs=(("RTA_PREFSRC", "10.10.0.1"),),
            ),
            id="non-link-scope",
        ),
        pytest.param(
            _route_message(
                "10.10.0.0",
                24,
                10,
                gateway="10.10.0.254",
                proto=2,
                scope=253,
                extra_attrs=(("RTA_PREFSRC", "10.10.0.1"),),
            ),
            id="gateway",
        ),
        pytest.param(
            _route_message(
                "10.11.0.0",
                24,
                10,
                proto=2,
                scope=253,
                extra_attrs=(("RTA_PREFSRC", "10.10.0.1"),),
            ),
            id="network-mismatch",
        ),
        pytest.param(
            _route_message(
                "10.10.0.0",
                24,
                10,
                proto=2,
                scope=253,
                extra_attrs=(("RTA_PREFSRC", "10.10.0.2"),),
            ),
            id="address-mismatch",
        ),
    ],
)
def test_inventory_rejects_unverified_preferred_source_routes(
    linux_node: NodePlan,
    route: dict[str, object],
) -> None:
    node = replace(linux_node, sysctls={})
    plan = TopologyPlan(
        name="preferred-source-route",
        fingerprint="preferred-source-route",
        nodes={node.name: node},
        links=(),
    )
    handle = Mock()
    handle.get_links.return_value = [_link_message(10, "eth0", "veth", 1450)]
    handle.get_addr.return_value = [_address_message(10, "10.10.0.1", 24)]
    handle.get_routes.return_value = [route]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    with pytest.raises(NslabError) as caught:
        backend.inventory(plan)

    assert caught.value.details == {
        "operation": "inventory",
        "resource": node.namespace,
        "reason": "preferred_source",
    }


def test_inventory_accepts_verified_kernel_connected_preferred_source(
    linux_node: NodePlan,
) -> None:
    node = replace(linux_node, sysctls={})
    plan = TopologyPlan(
        name="kernel-connected-route",
        fingerprint="kernel-connected-route",
        nodes={node.name: node},
        links=(),
    )
    handle = Mock()
    handle.get_links.return_value = [_link_message(10, "eth0", "veth", 1450)]
    handle.get_addr.return_value = [_address_message(10, "10.10.0.1", 24)]
    handle.get_routes.return_value = [
        _route_message(
            "10.10.0.0",
            24,
            10,
            proto=2,
            scope=253,
            extra_attrs=(
                ("RTA_PREFSRC", "10.10.0.1"),
                ("RTA_CACHEINFO", {"last_use": 0}),
            ),
        )
    ]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    inventory = backend.inventory(plan)

    assert inventory.namespaces[node.namespace].routes == (
        RoutePlan(IPv4Network("10.10.0.0/24"), None, "eth0"),
    )


@pytest.mark.parametrize(
    ("route", "reason"),
    [
        pytest.param(
            _route_message("192.0.2.0", 24, None, route_type=6),
            "blackhole",
            id="blackhole",
        ),
        pytest.param(
            _route_message("192.0.2.0", 24, None, route_type=7),
            "unreachable",
            id="unreachable",
        ),
        pytest.param(
            _route_message(
                "192.0.2.0",
                24,
                None,
                extra_attrs=(("RTA_MULTIPATH", ({"oif": 10}, {"oif": 11})),),
            ),
            "multipath",
            id="multipath",
        ),
        pytest.param(
            _route_message(
                "192.0.2.0",
                24,
                None,
                extra_attrs=(("RTA_NH_ID", 42),),
            ),
            "nexthop_id",
            id="nexthop-id",
        ),
        pytest.param(
            _route_message("192.0.2.0", 24, None),
            "missing_oif",
            id="missing-oif",
        ),
        pytest.param(
            _route_message("192.0.2.0", 24, 999),
            "unknown_ifindex",
            id="unknown-ifindex",
        ),
        pytest.param(
            _route_message(
                "192.0.2.0",
                24,
                10,
                extra_attrs=(("RTA_PRIORITY", 100),),
            ),
            "priority",
            id="priority",
        ),
        pytest.param(
            _route_message(
                "192.0.2.0",
                24,
                10,
                src_len=24,
                extra_attrs=(("RTA_SRC", "198.51.100.0"),),
            ),
            "source_specific",
            id="source-specific",
        ),
        pytest.param(
            _route_message(
                "192.0.2.0",
                24,
                10,
                extra_attrs=(("RTA_METRICS", {"mtu": 1400}),),
            ),
            "metrics",
            id="metrics",
        ),
        pytest.param(
            _route_message(
                "192.0.2.0",
                24,
                10,
                extra_attrs=(("RTA_VIA", {"family": 10, "addr": "::1"}),),
            ),
            "via",
            id="via",
        ),
        pytest.param(
            _route_message("192.0.2.0", 24, 10, tos=4),
            "tos",
            id="tos",
        ),
        pytest.param(
            _route_message("192.0.2.0", 24, 10, flags=1),
            "route_flags",
            id="route-flags",
        ),
        pytest.param(
            _route_message(
                "192.0.2.0",
                24,
                10,
                extra_attrs=(("RTA_FUTURE_SEMANTIC", object()),),
            ),
            "unsupported_attribute",
            id="unknown-attribute",
        ),
        pytest.param(
            _route_message(None, 24, 10),
            "missing_destination",
            id="missing-destination",
        ),
    ],
)
def test_inventory_rejects_routes_that_cannot_be_represented_without_loss(
    linux_node: NodePlan,
    route: dict[str, object],
    reason: str,
) -> None:
    node = replace(linux_node, sysctls={})
    plan = TopologyPlan(
        name="unsupported-route-inventory",
        fingerprint="unsupported-route-inventory",
        nodes={node.name: node},
        links=(),
    )
    handle = Mock()
    handle.get_links.return_value = [
        _link_message(1, "lo", "loopback", 65536),
        _link_message(10, "eth0", "veth", 1450),
    ]
    handle.get_addr.return_value = []
    handle.get_routes.return_value = [route]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    with pytest.raises(NslabError) as caught:
        backend.inventory(plan)

    assert caught.value.as_dict() == {
        "code": "INVENTORY_UNSUPPORTED",
        "message": f"unsupported route in network inventory: {node.namespace}",
        "details": {
            "operation": "inventory",
            "resource": node.namespace,
            "reason": reason,
        },
    }
    handle.close.assert_called_once_with()


def test_inventory_rejects_metric_distinct_routes_instead_of_deduplicating(
    linux_node: NodePlan,
) -> None:
    node = replace(linux_node, sysctls={})
    plan = TopologyPlan(
        name="metric-route-inventory",
        fingerprint="metric-route-inventory",
        nodes={node.name: node},
        links=(),
    )
    handle = Mock()
    handle.get_links.return_value = [_link_message(10, "eth0", "veth", 1450)]
    handle.get_addr.return_value = []
    route = _route_message("203.0.113.0", 24, 10)
    metric_route = _route_message(
        "203.0.113.0",
        24,
        10,
        extra_attrs=(("RTA_PRIORITY", 50),),
    )
    handle.get_routes.return_value = [route, metric_route]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    with pytest.raises(NslabError) as caught:
        backend.inventory(plan)

    assert caught.value.details == {
        "operation": "inventory",
        "resource": node.namespace,
        "reason": "priority",
    }


@pytest.mark.parametrize(
    "include_connected_main_route",
    (False, True),
    ids=("missing-connected", "deduplicates-connected"),
)
def test_inventory_synthesizes_connected_routes_from_observed_addresses(
    linux_node: NodePlan,
    include_connected_main_route: bool,
) -> None:
    node = replace(linux_node, sysctls={})
    plan = TopologyPlan(
        name="inventory-connected-routes",
        fingerprint="inventory-connected-routes",
        nodes={node.name: node},
        links=(),
    )
    handle = Mock()
    handle.get_links.return_value = [
        _link_message(1, "lo", "loopback", 65536),
        _link_message(10, "eth0", "veth", 1450),
    ]
    handle.get_addr.return_value = [
        _address_message(1, "127.0.0.1", 8),
        _address_message(10, "10.10.0.1", 24),
    ]
    main_routes = [
        _route_message(None, 0, 10, gateway="10.10.0.254"),
    ]
    if include_connected_main_route:
        main_routes.insert(0, _route_message("10.10.0.0", 24, 10))
    handle.get_routes.return_value = main_routes
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    inventory = backend.inventory(plan)

    assert inventory.namespaces[node.namespace].routes == (
        RoutePlan(IPv4Network("127.0.0.0/8"), None, "lo"),
        RoutePlan(IPv4Network("10.10.0.0/24"), None, "eth0"),
        node.routes[0],
    )


def test_inventory_records_interfaces_routes_and_declared_sysctls(
    tmp_path: Path,
    topology_plan: TopologyPlan,
) -> None:
    h1 = topology_plan.nodes["h1"]
    sw1 = topology_plan.nodes["sw1"]
    h1_handle = Mock()
    h1_handle.get_links.return_value = [
        _link_message(1, "lo", "loopback", 65536),
        _link_message(10, "eth0", "veth", 1450),
        _link_message(11, "dummy0", "dummy", 9000, up=False),
    ]
    h1_handle.get_addr.return_value = [
        _address_message(1, "127.0.0.1", 8),
        _address_message(10, "10.10.0.1", 24),
    ]
    connected = _route_message("10.10.0.0", 24, 10)
    h1_handle.get_routes.return_value = [
        _route_message("127.0.0.0", 8, 1),
        connected,
        connected,
        _route_message(None, 0, 10, gateway="10.10.0.254"),
        _route_message("198.51.100.0", 24, 10),
        _route_message("10.10.0.1", 32, 10, table=255, route_type=2),
        _route_message("10.10.0.255", 32, 10, table=255, route_type=3),
    ]
    sw1_handle = Mock()
    sw1_handle.get_links.return_value = [
        _link_message(1, "lo", "loopback", 65536),
        _link_message(
            20,
            "br0",
            "bridge",
            1500,
            stp=1,
            vlan_filtering=0,
        ),
        _link_message(21, "swp1", "veth", 1450, master=20),
    ]
    sw1_handle.get_addr.return_value = [
        _address_message(1, "127.0.0.1", 8),
        _address_message(20, "192.0.2.1", 24),
    ]
    sw1_handle.get_routes.return_value = [
        _route_message("127.0.0.0", 8, 1),
        _route_message("192.0.2.0", 24, 20),
    ]
    handles = {h1.namespace: h1_handle, sw1.namespace: sw1_handle}
    netns_factory = Mock(side_effect=lambda namespace: handles[namespace])
    pushns = Mock()
    popns = Mock()
    sysctl = tmp_path / "net/ipv4/ip_forward"
    sysctl.parent.mkdir(parents=True)
    sysctl.write_text("1\n", encoding="ascii")
    backend = Pyroute2Backend(
        iproute_factory=Mock(return_value=Mock(link_lookup=Mock(return_value=[]))),
        netns_factory=netns_factory,
        pushns=pushns,
        popns=popns,
        sysctl_root=tmp_path,
    )

    inventory = backend.inventory(topology_plan)

    assert netns_factory.call_args_list == [call(h1.namespace), call(sw1.namespace)]
    for handle in (h1_handle, sw1_handle):
        handle.get_addr.assert_called_once_with(family=socket.AF_INET)
        handle.get_routes.assert_called_once_with(
            family=socket.AF_INET,
            table=254,
        )
        handle.close.assert_called_once_with()
    h1_inventory = inventory.namespaces[h1.namespace]
    assert tuple(h1_inventory.interfaces) == ("lo", "eth0", "dummy0")
    assert h1_inventory.interfaces["eth0"].kind == "veth"
    assert h1_inventory.interfaces["eth0"].ifindex == 10
    assert h1_inventory.interfaces["eth0"].master is None
    assert h1_inventory.interfaces["eth0"].mtu == 1450
    assert h1_inventory.interfaces["eth0"].up is True
    assert h1_inventory.interfaces["eth0"].addresses == (IPv4Interface("10.10.0.1/24"),)
    assert h1_inventory.interfaces["dummy0"].up is False
    assert h1_inventory.routes == (
        RoutePlan(IPv4Network("127.0.0.0/8"), None, "lo"),
        RoutePlan(IPv4Network("10.10.0.0/24"), None, "eth0"),
        RoutePlan(
            IPv4Network("0.0.0.0/0"),
            IPv4Address("10.10.0.254"),
            "eth0",
        ),
        RoutePlan(IPv4Network("198.51.100.0/24"), None, "eth0"),
    )
    assert h1_inventory.sysctls == {"net.ipv4.ip_forward": 1}
    bridge = inventory.namespaces[sw1.namespace].interfaces["br0"]
    assert bridge.kind == "bridge"
    assert bridge.stp is True
    assert bridge.vlan_filtering is False
    assert inventory.namespaces[sw1.namespace].interfaces["swp1"].master == "br0"
    pushns.assert_called_once_with(h1.namespace)
    popns.assert_called_once_with()


def test_inventory_closes_namespace_and_translates_non_missing_netlink_error(
    topology_plan: TopologyPlan,
) -> None:
    failure = NetlinkError(errno.EPERM, "inventory denied")
    root = Mock()
    root.link_lookup.return_value = []
    handle = Mock()
    handle.get_links.side_effect = failure
    backend = Pyroute2Backend(
        iproute_factory=Mock(return_value=root),
        netns_factory=Mock(return_value=handle),
    )
    first_namespace = next(iter(topology_plan.nodes.values())).namespace

    with pytest.raises(NslabError) as caught:
        backend.inventory(topology_plan)

    handle.close.assert_called_once_with()
    assert caught.value.code == "NETLINK_ERROR"
    assert caught.value.details == {
        "errno": errno.EPERM,
        "operation": "inventory",
        "resource": first_namespace,
    }
    root.close.assert_called_once_with()
