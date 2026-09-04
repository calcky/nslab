from __future__ import annotations

import errno
import os
import signal
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from functools import partial
from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_interface,
)
from pathlib import Path
from subprocess import PIPE
from typing import Any
from uuid import uuid4

from pyroute2 import (
    IPRoute,
    NetNS,
    NSPopen,
    netns,
)
from pyroute2 import config as pyroute2_config
from pyroute2.iproute.linux import get_arguments_processor
from pyroute2.netlink.exceptions import NetlinkError
from pyroute2.netlink.rtnl import TC_H_ROOT
from pyroute2.netlink.rtnl.tcmsg.common import percent2u32, time2tick

from nslab.backend.base import (
    ExecResult,
    InterfaceInventory,
    LiveInventory,
    NamespaceInventory,
)
from nslab.errors import NslabError, OperationCancelled
from nslab.planner import (
    BondDevicePlan,
    BridgeVlanPlan,
    DummyDevicePlan,
    EndpointPlan,
    FqCodelPlan,
    GeneveDevicePlan,
    GreDevicePlan,
    IPInterface,
    IpipDevicePlan,
    IpvlanDevicePlan,
    LinkPlan,
    MacvlanDevicePlan,
    NeighborPlan,
    NeighborState,
    NetemPlan,
    NodePlan,
    PolicyRulePlan,
    QdiscPlan,
    RouteNextHopPlan,
    RoutePlan,
    TbfPlan,
    TopologyPlan,
    VlanDevicePlan,
    VrfDevicePlan,
    VxlanDevicePlan,
    bond_device_mtu,
    dummy_device_mtu,
    geneve_device_mtu,
    gre_device_mtu,
    ipip_device_mtu,
    ipvlan_device_mtu,
    macvlan_device_mtu,
    node_interface_addresses,
    node_interface_master,
    node_route_tables,
    vxlan_device_mtu,
)
from nslab.routing import FrrRuntime
from nslab.tc import format_rate

_BRIDGE_VLAN_INFO_PVID = 2
_BRIDGE_VLAN_INFO_UNTAGGED = 4
_VETH_VISIBILITY_TIMEOUT = 5.0
_VETH_VISIBILITY_INTERVAL = 0.05
_PYROUTE2_CONFIG_LOCK = threading.Lock()

_IFF_UP = 1
_ARPHRD_LOOPBACK = 772
_NTF_PROXY = 0x08
_NUD_NONE = 0x00
_NEIGHBOR_STATE_TO_NETLINK: dict[NeighborState, int] = {
    "incomplete": 0x01,
    "reachable": 0x02,
    "stale": 0x04,
    "delay": 0x08,
    "probe": 0x10,
    "failed": 0x20,
    "noarp": 0x40,
    "permanent": 0x80,
}
_NEIGHBOR_STATE_FROM_NETLINK = {value: key for key, value in _NEIGHBOR_STATE_TO_NETLINK.items()}
_ALLOWED_NEIGHBOR_ATTRIBUTES = frozenset(
    {
        "NDA_DST",
        "NDA_LLADDR",
        "NDA_CACHEINFO",
        "NDA_PROBES",
        "NDA_PROTOCOL",
    }
)
_RT_TABLE_MAIN = 254
_RTN_UNICAST = 1
_RTPROT_KERNEL = 2
_RT_SCOPE_LINK = 253
_RTM_F_CLONED = 0x200
_FR_ACT_TO_TBL = 1
_FIB_RULE_INVERT = 2
_RULE_ACTION_TO_NETLINK = {
    "lookup": "to_tbl",
    "goto": "goto",
    "nop": "nop",
    "blackhole": "blackhole",
    "unreachable": "unreachable",
    "prohibit": "prohibit",
}
_RULE_ACTION_FROM_NETLINK = {
    1: "lookup",
    2: "goto",
    3: "nop",
    6: "blackhole",
    7: "unreachable",
    8: "prohibit",
}
_BOND_MODE_TO_NETLINK = {"active-backup": 1, "802.3ad": 4}
_BOND_MODE_FROM_NETLINK = {value: key for key, value in _BOND_MODE_TO_NETLINK.items()}
_BOND_LACP_RATE_TO_NETLINK = {"slow": 0, "fast": 1}
_BOND_LACP_RATE_FROM_NETLINK = {value: key for key, value in _BOND_LACP_RATE_TO_NETLINK.items()}
_BOND_XMIT_HASH_POLICY_TO_NETLINK = {
    "layer2": 0,
    "layer3+4": 1,
    "layer2+3": 2,
}
_BOND_XMIT_HASH_POLICY_FROM_NETLINK = {
    value: key for key, value in _BOND_XMIT_HASH_POLICY_TO_NETLINK.items()
}
_MACVLAN_MODE_TO_NETLINK = {
    "private": 1,
    "vepa": 2,
    "bridge": 4,
    "passthru": 8,
    "source": 16,
}
_MACVLAN_MODE_FROM_NETLINK = {value: key for key, value in _MACVLAN_MODE_TO_NETLINK.items()}
_IPVLAN_MODE_TO_NETLINK = {"l2": 0, "l3": 1, "l3s": 2}
_IPVLAN_MODE_FROM_NETLINK = {value: key for key, value in _IPVLAN_MODE_TO_NETLINK.items()}
_GRE_KEY_NETLINK_FLAG = 0x2000
_KERNEL_TUNNEL_FALLBACKS = frozenset(
    {
        ("gre0", "gre"),
        ("gretap0", "gretap"),
        ("erspan0", "erspan"),
        ("tunl0", "ipip"),
    }
)
# FRR uses the Linux-assigned protocol identifiers for protocol-originated
# routes.  ``RTPROT_ZEBRA`` is included for FRR releases that use the generic
# Zebra identifier for an imported/redistributed route.
_FRR_ROUTE_PROTOCOLS = frozenset({11, 186, 188})
_EXEC_SIGNALS = frozenset((signal.SIGINT, signal.SIGTERM))
_UNSUPPORTED_ROUTE_TYPES = {
    2: "local",
    3: "broadcast",
    4: "anycast",
    5: "multicast",
    6: "blackhole",
    7: "unreachable",
    8: "prohibit",
    9: "throw",
    10: "nat",
    11: "xresolve",
}
_VRF_KERNEL_LOCAL_ROUTE_TYPES = frozenset({2, 3})
_ALLOWED_RULE_ATTRIBUTES = frozenset(
    {
        "FRA_DST",
        "FRA_SRC",
        "FRA_IIFNAME",
        "FRA_GOTO",
        "FRA_PRIORITY",
        "FRA_FWMARK",
        "FRA_FLOW",
        "FRA_TUN_ID",
        "FRA_SUPPRESS_IFGROUP",
        "FRA_SUPPRESS_PREFIXLEN",
        "FRA_TABLE",
        "FRA_FWMASK",
        "FRA_OIFNAME",
        "FRA_PAD",
        "FRA_L3MDEV",
        "FRA_UID_RANGE",
        "FRA_PROTOCOL",
        "FRA_IP_PROTO",
        "FRA_SPORT_RANGE",
        "FRA_DPORT_RANGE",
    }
)
_ALLOWED_ROUTE_ATTRIBUTES = frozenset(
    {
        "RTA_TABLE",
        "RTA_DST",
        "RTA_OIF",
        "RTA_GATEWAY",
        "RTA_MULTIPATH",
        "RTA_PREFSRC",
        "RTA_CACHEINFO",
        "RTA_PAD",
    }
)
_UNSUPPORTED_ROUTE_ATTRIBUTES = (
    ("RTA_IIF", "input_interface"),
    ("RTA_METRICS", "metrics"),
    ("RTA_PROTOINFO", "protocol_info"),
    ("RTA_FLOW", "flow"),
    ("RTA_MARK", "mark"),
    ("RTA_VIA", "via"),
    ("RTA_NEWDST", "new_destination"),
    ("RTA_ENCAP_TYPE", "encapsulation"),
    ("RTA_ENCAP", "encapsulation"),
    ("RTA_EXPIRES", "expires"),
    ("RTA_UID", "uid"),
    ("RTA_TTL_PROPAGATE", "ttl_propagate"),
    ("RTA_IP_PROTO", "ip_protocol"),
    ("RTA_SPORT", "source_port"),
    ("RTA_DPORT", "destination_port"),
    ("RTA_PREF", "preference"),
    ("RTA_SESSION", "session"),
    ("RTA_MFC_STATS", "multicast_stats"),
)


def _open_existing_namespace(namespace: str) -> Any:
    # pyroute2 forks while opening a namespace socket. Its child must not
    # inherit nslab's SIGTERM cancellation handler during library cleanup.
    if not hasattr(pyroute2_config, "disable_mp_signal"):
        return NetNS(namespace, flags=0)
    with _PYROUTE2_CONFIG_LOCK:
        previous = pyroute2_config.disable_mp_signal
        pyroute2_config.disable_mp_signal = True
        try:
            return NetNS(namespace, flags=0)
        finally:
            pyroute2_config.disable_mp_signal = previous


def _enter_existing_namespace(namespace: str) -> None:
    netns.pushns()
    try:
        netns.setns(namespace, flags=0)
    except (Exception, KeyboardInterrupt):
        with _cleanup_context(netns.popns, "namespace pop"):
            raise


def _translate_netlink_error(
    error: NetlinkError,
    *,
    operation: str,
    resource: str,
) -> NslabError:
    return _translate_errno(
        error.code,
        operation=operation,
        resource=resource,
    )


def _translate_os_error(
    error: OSError,
    *,
    operation: str,
    resource: str,
) -> NslabError:
    return _translate_errno(
        error.errno,
        operation=operation,
        resource=resource,
    )


def _translate_errno(
    error_number: int | None,
    *,
    operation: str,
    resource: str,
) -> NslabError:
    normalized_errno = abs(error_number) if error_number is not None else None
    if normalized_errno == errno.EEXIST:
        code = "RESOURCE_EXISTS"
        message = f"network resource already exists: {resource}"
    elif normalized_errno in {errno.ENOENT, errno.ENODEV, errno.ENXIO, errno.ESRCH}:
        code = "RESOURCE_MISSING"
        message = f"network resource is missing: {resource}"
    else:
        code = "NETLINK_ERROR"
        message = f"netlink operation failed for network resource: {resource}"
    return NslabError(
        code=code,
        message=message,
        details={
            "errno": normalized_errno,
            "operation": operation,
            "resource": resource,
        },
    )


def _required_index(handle: Any, name: str, operation: str, resource: str) -> int:
    indexes = handle.link_lookup(ifname=name)
    if indexes:
        return int(indexes[0])
    raise NslabError(
        code="RESOURCE_MISSING",
        message=f"network resource is missing: {resource}",
        details={"operation": operation, "resource": resource},
    )


def _veth_missing_error(
    operation: str,
    resource: str,
    *,
    phase: str,
    interface: str,
    namespace: str | None = None,
    cause: BaseException | None = None,
) -> NslabError:
    location = interface if namespace is None else f"{namespace}:{interface}"
    details: dict[str, object] = {
        "operation": operation,
        "resource": resource,
        "phase": phase,
        "interface": interface,
    }
    if namespace is not None:
        details["namespace"] = namespace
    if isinstance(cause, NetlinkError):
        details["errno"] = abs(cause.code)
    elif isinstance(cause, OSError) and cause.errno is not None:
        details["errno"] = abs(cause.errno)
    if cause is not None:
        details["last_error"] = type(cause).__name__
    return NslabError(
        code="RESOURCE_MISSING",
        message=f"network resource is missing during {phase}: {location} ({resource})",
        details=details,
    )


def _is_transient_veth_error(error: BaseException) -> bool:
    if isinstance(error, NslabError):
        return error.code == "RESOURCE_MISSING"
    if isinstance(error, NetlinkError):
        return _is_missing_error(error.code)
    if isinstance(error, TimeoutError):
        return True
    return isinstance(error, OSError) and error.errno is not None and _is_missing_error(error.errno)


def _wait_for_required_index(
    handle: Any,
    name: str,
    operation: str,
    resource: str,
    *,
    phase: str,
    namespace: str | None = None,
    deadline: float | None = None,
) -> int:
    if deadline is None:
        deadline = time.monotonic() + _VETH_VISIBILITY_TIMEOUT
    last_error: BaseException | None = None
    while True:
        try:
            indexes = handle.link_lookup(ifname=name)
        except (NetlinkError, OSError) as error:
            if not _is_transient_veth_error(error):
                raise
            indexes = ()
            last_error = error
        if indexes:
            return int(indexes[0])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            missing = _veth_missing_error(
                operation,
                resource,
                phase=phase,
                interface=name,
                namespace=namespace,
                cause=last_error,
            )
            if last_error is not None:
                raise missing from last_error
            raise missing
        time.sleep(min(_VETH_VISIBILITY_INTERVAL, remaining))


def _veth_resource(link: LinkPlan) -> str:
    return f"{link.left.temporary_name}<->{link.right.temporary_name}"


def _new_ownership_token() -> str:
    return f"nslab-{uuid4().hex}"


def _attribute_pair(attribute: Any) -> tuple[Any, Any] | None:
    try:
        return attribute[0], attribute[1]
    except (IndexError, KeyError, TypeError):
        attribute_name = getattr(attribute, "name", None)
        if attribute_name is None or not hasattr(attribute, "value"):
            return None
        return attribute_name, attribute.value


def _attribute(message: Any, name: str) -> Any | None:
    getter = getattr(message, "get_attr", None)
    if callable(getter):
        value = getter(name)
        if value is not None:
            return value
    if isinstance(message, dict):
        if name in message:
            return message[name]
        for attribute in message.get("attrs", ()):
            pair = _attribute_pair(attribute)
            if pair is not None and pair[0] == name:
                return pair[1]
    return None


def _attribute_names(message: Any) -> frozenset[str]:
    names: set[str] = set()
    for attribute in _value(message, "attrs", ()):
        pair = _attribute_pair(attribute)
        if pair is not None:
            names.add(str(pair[0]))
    return frozenset(names)


def _value(message: Any, name: str, default: object = None) -> Any:
    getter = getattr(message, "get", None)
    if callable(getter):
        return getter(name, default)
    return default


def _unsupported_inventory_route(namespace: str, reason: str) -> NslabError:
    return NslabError(
        code="INVENTORY_UNSUPPORTED",
        message=f"unsupported route in network inventory: {namespace}",
        details={
            "operation": "inventory",
            "resource": namespace,
            "reason": reason,
        },
    )


def _unsupported_inventory_neighbor(namespace: str, reason: str) -> NslabError:
    return NslabError(
        code="INVENTORY_UNSUPPORTED",
        message=f"unsupported neighbor in network inventory: {namespace}",
        details={"namespace": namespace, "reason": reason},
    )


def _decode_multipath_nexthops(
    value: object,
    names_by_index: Mapping[int, str],
    namespace: str,
    family: int,
) -> tuple[RouteNextHopPlan, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) < 2
    ):
        raise _unsupported_inventory_route(namespace, "multipath")

    nexthops: list[RouteNextHopPlan] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise _unsupported_inventory_route(namespace, "multipath_nexthop")
        if not _attribute_names(item) <= {"RTA_GATEWAY"}:
            raise _unsupported_inventory_route(namespace, "multipath_nexthop_attribute")
        try:
            flags = int(_value(item, "flags", 0))
            hops = int(_value(item, "hops", 0))
            output_index = int(_value(item, "oif"))
        except (TypeError, ValueError):
            raise _unsupported_inventory_route(namespace, "multipath_nexthop") from None
        if flags != 0:
            raise _unsupported_inventory_route(namespace, "multipath_nexthop_flags")
        if not 0 <= hops <= 255:
            raise _unsupported_inventory_route(namespace, "multipath_nexthop_weight")
        interface = names_by_index.get(output_index)
        if interface is None:
            raise _unsupported_inventory_route(namespace, "unknown_ifindex")
        gateway_value = _attribute(item, "RTA_GATEWAY")
        try:
            gateway = (
                None
                if gateway_value is None
                else (
                    IPv6Address(str(gateway_value))
                    if family == socket.AF_INET6
                    else IPv4Address(str(gateway_value))
                )
            )
        except (TypeError, ValueError):
            raise _unsupported_inventory_route(namespace, "invalid_gateway") from None
        nexthops.append(
            RouteNextHopPlan(
                via=gateway,
                dev=interface,
                weight=hops + 1,
            )
        )

    identities = tuple((nexthop.via, nexthop.dev) for nexthop in nexthops)
    if len(set(identities)) != len(identities):
        raise _unsupported_inventory_route(namespace, "multipath_nexthop")
    return tuple(nexthops)


def _unsupported_inventory_rule(namespace: str, reason: str) -> NslabError:
    return NslabError(
        code="INVENTORY_UNSUPPORTED",
        message=f"unsupported policy rule in network inventory: {namespace}",
        details={
            "operation": "inventory",
            "resource": namespace,
            "reason": reason,
        },
    )


def _unsupported_inventory_qdisc(namespace: str, reason: str) -> NslabError:
    return NslabError(
        code="INVENTORY_UNSUPPORTED",
        message=f"unsupported qdisc in network inventory: {namespace}",
        details={
            "operation": "inventory",
            "resource": namespace,
            "reason": reason,
        },
    )


def _is_root_qdisc(message: Any) -> bool:
    parent = _value(message, "parent")
    if parent is None:
        return True
    try:
        return bool(int(parent) == TC_H_ROOT)
    except (TypeError, ValueError):
        return False


def _qdisc_option(options: Any, name: str, default: object = None) -> Any:
    value = _attribute(options, name)
    if value is not None:
        return value
    return _value(options, name, default)


def _netem_request_filter(index: int, netem: NetemPlan) -> Any:
    """Build a netem request without pyroute2's duplicate top-level rate attr.

    pyroute2's netem plugin correctly puts ``rate`` inside ``TCA_NETEM_RATE``
    but also leaves the input keyword in the generic ``TCA_RATE`` attribute.
    Recent kernels reject that combination with ``ERANGE``.  Reusing the
    library's request processor and removing only the generic keyword keeps
    the plugin's version-specific encoding (including clock conversion) while
    producing the request accepted by the kernel.
    """

    arguments: dict[str, object] = {
        "kind": "netem",
        "index": index,
        "handle": "1:",
        "delay": netem.delay_ms * 1000,
        "jitter": netem.jitter_ms * 1000,
        "loss": netem.loss_percent,
        "rate": netem.rate,
    }
    request_filter = get_arguments_processor("tc", "add", arguments)
    request_filter.pop("rate", None)
    return request_filter


def _decode_milliseconds(raw_value: int, namespace: str, field: str) -> int:
    """Decode a kernel microsecond value, allowing one-microsecond rounding."""

    if raw_value <= 0:
        raise _unsupported_inventory_qdisc(namespace, "parameters")
    milliseconds = round(raw_value / 1000)
    if milliseconds <= 0 or abs(raw_value - milliseconds * 1000) > 1:
        raise _unsupported_inventory_qdisc(namespace, f"{field}_precision")
    return milliseconds


def _decode_tbf_burst(raw_buffer: int, raw_rate: int, namespace: str) -> int:
    ticks_per_us = float(time2tick(1))
    if ticks_per_us <= 0:
        raise _unsupported_inventory_qdisc(namespace, "clock")
    burst = round(raw_buffer * raw_rate / (ticks_per_us * 1_000_000))
    if burst <= 0:
        raise _unsupported_inventory_qdisc(namespace, "buffer")
    return burst


def _decode_tbf_latency(raw_limit: int, burst: int, raw_rate: int, namespace: str) -> int:
    if raw_limit < burst or raw_rate <= 0:
        raise _unsupported_inventory_qdisc(namespace, "parameters")
    return round((raw_limit - burst) * 1000 / raw_rate)


def _is_missing_error(error_number: int) -> bool:
    return abs(error_number) in {
        errno.ENOENT,
        errno.ENODEV,
        errno.ENXIO,
        errno.ESRCH,
    }


@contextmanager
def _cleanup_context(cleanup: Callable[[], None], label: str) -> Iterator[None]:
    primary: BaseException | None = None
    try:
        yield
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            cleanup()
        except BaseException as cleanup_error:
            if primary is None:
                raise
            primary.add_note(f"{label} cleanup failed: {cleanup_error!r}")


def _unblock_exec_signals() -> None:
    signal.pthread_sigmask(signal.SIG_UNBLOCK, _EXEC_SIGNALS)


def _restore_exec_signal_mask(
    previous_mask: set[int | signal.Signals],
) -> None:
    signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def _signal_exec_target(
    timeout_pidfd: int,
    selected_signal: signal.Signals,
) -> None:
    with suppress(ProcessLookupError):
        signal.pidfd_send_signal(timeout_pidfd, selected_signal)


def _cleanup_exec_process(
    process: Any,
    timeout_pidfd: int | None,
    selected_signal: signal.Signals | None,
) -> list[tuple[str, BaseException]]:
    failures: list[tuple[str, BaseException]] = []
    if selected_signal is not None:
        try:
            if timeout_pidfd is None:
                process.terminate()
            else:
                _signal_exec_target(timeout_pidfd, selected_signal)
        except BaseException as cleanup_error:
            failures.append(("exec target termination", cleanup_error))
    try:
        process.release()
    except BaseException as cleanup_error:
        failures.append(("NSPopen release", cleanup_error))
    if timeout_pidfd is not None:
        try:
            os.close(timeout_pidfd)
        except BaseException as cleanup_error:
            failures.append(("exec pidfd close", cleanup_error))
    return failures


@contextmanager
def _spawn_nspopen(
    factory: Callable[..., Any],
    namespace: str,
    argv: Sequence[str],
    options: dict[str, Any],
    *,
    inherit_stdin: bool = False,
) -> Iterator[tuple[Any, Callable[[], None], Callable[[], None]]]:
    previous_mask: set[int | signal.Signals] = set(
        signal.pthread_sigmask(signal.SIG_BLOCK, _EXEC_SIGNALS)
    )
    process: Any | None = None
    timeout_pidfd: int | None = None
    stdin_dup_error: BaseException | None = None
    stdin_close_error: BaseException | None = None
    factory_error: BaseException | None = None
    pid_error: BaseException | None = None
    stdin_fd: int | None = None
    popen_options = dict(options)
    if inherit_stdin:
        try:
            stdin_fd = os.dup(0)
            popen_options["stdin"] = stdin_fd
        except BaseException as error:
            stdin_dup_error = error
    if stdin_dup_error is None:
        try:
            process = factory(
                namespace,
                list(argv),
                preexec_fn=_unblock_exec_signals,
                **popen_options,
            )
        except BaseException as error:
            factory_error = error
    if stdin_fd is not None:
        owned_stdin_fd = stdin_fd
        stdin_fd = None
        try:
            os.close(owned_stdin_fd)
        except BaseException as error:
            stdin_close_error = error
    if process is not None:
        try:
            timeout_pid = int(process.pid)
            if timeout_pid <= 0:
                raise ValueError(f"NSPopen PID must be positive, got {timeout_pid}")
            timeout_pidfd = os.pidfd_open(timeout_pid)
        except BaseException as error:
            pid_error = error

    signals_blocked = True
    mask_restore_attempted = False
    mask_restore_completed = False
    release_signals_blocked = False

    def restore_signal_mask() -> None:
        nonlocal signals_blocked, mask_restore_attempted, mask_restore_completed
        mask_restore_attempted = True
        signals_blocked = False
        _restore_exec_signal_mask(previous_mask)
        mask_restore_completed = True
        if stdin_dup_error is not None:
            raise stdin_dup_error
        if factory_error is not None:
            if isinstance(factory_error, NetlinkError):
                raise _translate_netlink_error(
                    factory_error,
                    operation="execute",
                    resource=namespace,
                ) from factory_error
            if isinstance(factory_error, OSError):
                raise _translate_os_error(
                    factory_error,
                    operation="execute",
                    resource=namespace,
                ) from factory_error
            raise factory_error
        if pid_error is not None:
            raise pid_error
        if stdin_close_error is not None:
            raise stdin_close_error
        if process is None:
            raise RuntimeError("NSPopen factory returned no process")
        if timeout_pidfd is None:
            raise RuntimeError("NSPopen target pidfd is unavailable")

    def block_release_signals() -> None:
        nonlocal signals_blocked, release_signals_blocked
        signal.pthread_sigmask(signal.SIG_BLOCK, _EXEC_SIGNALS)
        signals_blocked = True
        release_signals_blocked = True

    body_error: BaseException | None = None
    body_completed = False
    try:
        yield process, restore_signal_mask, block_release_signals
        body_completed = True
    except BaseException as error:
        body_error = error

    if body_completed and not release_signals_blocked:
        body_error = RuntimeError("exec release signals were not blocked")

    cleanup_failures: list[tuple[str, BaseException]] = []
    if stdin_close_error is not None and stdin_close_error is not body_error:
        cleanup_failures.append(("exec stdin fd close", stdin_close_error))
    replacement_cause: BaseException | None = None

    if body_error is not None and not mask_restore_attempted:
        signals_blocked = False
        try:
            _restore_exec_signal_mask(previous_mask)
        except (KeyboardInterrupt, OperationCancelled) as cancellation:
            replacement_cause = body_error
            body_error = cancellation
        except BaseException as cleanup_error:
            cleanup_failures.append(("exec signal mask restoration", cleanup_error))
        else:
            mask_restore_completed = True

    if process is not None:
        if not signals_blocked:
            try:
                signal.pthread_sigmask(signal.SIG_BLOCK, _EXEC_SIGNALS)
            except (KeyboardInterrupt, OperationCancelled) as cancellation:
                if body_error is not None:
                    replacement_cause = body_error
                body_error = cancellation
            except BaseException as cleanup_error:
                cleanup_failures.append(("exec signal block", cleanup_error))
            else:
                signals_blocked = True

        selected_signal: signal.Signals | None = None
        if body_error is not None and not release_signals_blocked:
            selected_signal = (
                signal.SIGINT if isinstance(body_error, KeyboardInterrupt) else signal.SIGTERM
            )
        cleanup_failures.extend(_cleanup_exec_process(process, timeout_pidfd, selected_signal))

    if signals_blocked or not mask_restore_completed:
        signals_blocked = False
        try:
            _restore_exec_signal_mask(previous_mask)
        except (KeyboardInterrupt, OperationCancelled) as cancellation:
            if body_error is not None:
                replacement_cause = body_error
            body_error = cancellation
        except BaseException as cleanup_error:
            cleanup_failures.append(("exec signal mask restoration", cleanup_error))

    if body_error is not None:
        for label, failure in cleanup_failures:
            body_error.add_note(f"{label} cleanup failed: {failure!r}")
        if replacement_cause is not None and replacement_cause is not body_error:
            raise body_error from replacement_cause
        raise body_error

    if cleanup_failures:
        (_, primary), *secondary = cleanup_failures
        for label, failure in secondary:
            primary.add_note(f"{label} cleanup failed: {failure!r}")
        raise primary


@contextmanager
def _managed_handle(handle: Any) -> Iterator[Any]:
    with _cleanup_context(handle.close, "handle close"):
        yield handle


class Pyroute2Backend:
    """Production network backend built on pyroute2."""

    def __init__(
        self,
        *,
        iproute_factory: Callable[[], Any] = IPRoute,
        netns_factory: Callable[[str], Any] = _open_existing_namespace,
        namespace_create: Callable[[str], None] = netns.create,
        namespace_remove: Callable[[str], None] = netns.remove,
        pushns: Callable[[str], None] = _enter_existing_namespace,
        popns: Callable[[], None] = netns.popns,
        nspopen_factory: Callable[..., Any] = NSPopen,
        ownership_token_factory: Callable[[], str] = _new_ownership_token,
        sysctl_root: Path = Path("/proc/sys"),
        routing_runtime: FrrRuntime | None = None,
        routing_root: Path = Path("/run/nslab"),
        frr_state_root: Path = Path("/var/run/frr"),
        frr_config_root: Path = Path("/etc/frr"),
    ) -> None:
        self._iproute_factory = iproute_factory
        self._netns_factory = netns_factory
        self._namespace_create = namespace_create
        self._namespace_remove = namespace_remove
        self._pushns = pushns
        self._popns = popns
        self._nspopen_factory = nspopen_factory
        self._ownership_token_factory = ownership_token_factory
        self._sysctl_root = sysctl_root
        self._routing_runtime = (
            routing_runtime
            if routing_runtime is not None
            else FrrRuntime(
                runtime_root=routing_root,
                frr_state_root=frr_state_root,
                frr_config_root=frr_config_root,
            )
        )

    def create_namespace(self, node: NodePlan) -> None:
        created = False
        try:
            self._namespace_create(node.namespace)
            created = True
            with _managed_handle(self._netns_factory(node.namespace)) as namespace:
                loopback = _required_index(
                    namespace,
                    "lo",
                    "create_namespace",
                    f"{node.namespace}:lo",
                )
                namespace.link("set", index=loopback, state="up")
        except (Exception, KeyboardInterrupt) as error:
            if created:
                with suppress(Exception, KeyboardInterrupt):
                    self._namespace_remove(node.namespace)
            if isinstance(error, NetlinkError):
                raise _translate_netlink_error(
                    error,
                    operation="create_namespace",
                    resource=node.namespace,
                ) from error
            if isinstance(error, OSError):
                raise _translate_os_error(
                    error,
                    operation="create_namespace",
                    resource=node.namespace,
                ) from error
            raise

    def delete_namespace(self, namespace: str) -> None:
        try:
            self._namespace_remove(namespace)
        except NetlinkError as error:
            raise _translate_netlink_error(
                error,
                operation="delete_namespace",
                resource=namespace,
            ) from error
        except OSError as error:
            raise _translate_os_error(
                error,
                operation="delete_namespace",
                resource=namespace,
            ) from error

    def create_bridge(self, node: NodePlan) -> None:
        bridge_name = node.bridge_name
        assert bridge_name is not None
        resource = f"{node.namespace}:{bridge_name}"
        bridge_arguments: dict[str, object] = {
            "ifname": bridge_name,
            "kind": "bridge",
            "br_stp_state": int(bool(node.stp)),
            "br_vlan_filtering": int(bool(node.vlan_filtering)),
        }
        if node.bridge_priority is not None:
            bridge_arguments["br_priority"] = node.bridge_priority
        try:
            with _managed_handle(self._netns_factory(node.namespace)) as namespace:
                namespace.link("add", **bridge_arguments)
        except NetlinkError as error:
            raise _translate_netlink_error(
                error,
                operation="create_bridge",
                resource=resource,
            ) from error

    def create_veth(self, link: LinkPlan) -> None:
        resource = _veth_resource(link)
        ownership_token = self._ownership_token_factory()
        root_names_verified_absent = False
        creation_attempted = False
        created = False
        moved_endpoints: set[EndpointPlan] = set()
        renamed_endpoints: set[EndpointPlan] = set()
        try:
            with _managed_handle(self._iproute_factory()) as root:
                for temporary_name in (
                    link.left.temporary_name,
                    link.right.temporary_name,
                ):
                    if root.link_lookup(ifname=temporary_name):
                        raise NslabError(
                            code="RESOURCE_EXISTS",
                            message=f"network resource already exists: {resource}",
                            details={
                                "operation": "create_veth",
                                "resource": resource,
                            },
                        )
                root_names_verified_absent = True
                creation_attempted = True
                root.link(
                    "add",
                    ifname=link.left.temporary_name,
                    kind="veth",
                    ifalias=ownership_token,
                    peer={
                        "ifname": link.right.temporary_name,
                        "ifalias": ownership_token,
                    },
                )
                created = True
                for endpoint in (link.left, link.right):
                    self._move_veth_endpoint(
                        root,
                        endpoint,
                        resource,
                        ownership_token,
                    )
                    moved_endpoints.add(endpoint)

            self._configure_veth_endpoint(
                link.left,
                link.mtu,
                link.netem,
                link.qdisc,
                resource,
                renamed_endpoints,
                ownership_token,
            )
            self._configure_veth_endpoint(
                link.right,
                link.mtu,
                link.netem,
                link.qdisc,
                resource,
                renamed_endpoints,
                ownership_token,
            )
        except (Exception, KeyboardInterrupt) as error:
            add_reported_existing = (
                isinstance(error, NetlinkError) and abs(error.code) == errno.EEXIST
            ) or (isinstance(error, OSError) and error.errno == errno.EEXIST)
            failed_owned_add = (
                root_names_verified_absent and creation_attempted and not add_reported_existing
            )
            if created or failed_owned_add:
                self._cleanup_veth(
                    link,
                    moved_endpoints,
                    renamed_endpoints,
                    ownership_token,
                )
            if isinstance(error, NetlinkError):
                raise _translate_netlink_error(
                    error,
                    operation="create_veth",
                    resource=resource,
                ) from error
            if isinstance(error, OSError):
                raise _translate_os_error(
                    error,
                    operation="create_veth",
                    resource=resource,
                ) from error
            raise

    @staticmethod
    def _move_veth_endpoint(
        root: Any,
        endpoint: EndpointPlan,
        resource: str,
        ownership_token: str,
    ) -> None:
        deadline = time.monotonic() + _VETH_VISIBILITY_TIMEOUT
        phase = "root-after-create"
        while True:
            try:
                phase = "root-after-create"
                index = _wait_for_required_index(
                    root,
                    endpoint.temporary_name,
                    "create_veth",
                    resource,
                    phase=phase,
                    deadline=deadline,
                )
                phase = "root-set-ownership"
                root.link("set", index=index, ifalias=ownership_token)
                phase = "root-move-to-namespace"
                root.link("set", index=index, net_ns_fd=endpoint.namespace)
                return
            except (Exception, KeyboardInterrupt) as error:
                if not _is_transient_veth_error(error):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    missing = _veth_missing_error(
                        "create_veth",
                        resource,
                        phase=phase,
                        interface=endpoint.temporary_name,
                        namespace=endpoint.namespace if phase == "root-move-to-namespace" else None,
                        cause=error,
                    )
                    raise missing from error
                time.sleep(min(_VETH_VISIBILITY_INTERVAL, remaining))

    def _configure_veth_endpoint(
        self,
        endpoint: EndpointPlan,
        mtu: int,
        netem: NetemPlan | None,
        qdisc: QdiscPlan | None,
        resource: str,
        renamed_endpoints: set[EndpointPlan],
        ownership_token: str,
    ) -> None:
        deadline = time.monotonic() + _VETH_VISIBILITY_TIMEOUT
        phase = "namespace-after-move"
        while True:
            try:
                with _managed_handle(self._netns_factory(endpoint.namespace)) as namespace:
                    phase = "namespace-after-move"
                    renamed = endpoint in renamed_endpoints
                    lookup_name = endpoint.interface if renamed else endpoint.temporary_name
                    indexes = namespace.link_lookup(ifname=lookup_name)

                    if not indexes and not renamed:
                        final_indexes = namespace.link_lookup(ifname=endpoint.interface)
                        if final_indexes:
                            final_index = int(final_indexes[0])
                            messages = namespace.get_links(final_index)
                            if (
                                messages
                                and _attribute(messages[0], "IFLA_IFALIAS") == ownership_token
                            ):
                                indexes = (final_index,)
                                renamed_endpoints.add(endpoint)
                                renamed = True

                    if not indexes:
                        raise _veth_missing_error(
                            "create_veth",
                            resource,
                            phase=phase,
                            interface=lookup_name,
                            namespace=endpoint.namespace,
                        )

                    index = int(indexes[0])
                    if not renamed:
                        phase = "namespace-rename"
                        namespace.link("set", index=index, ifname=endpoint.interface)
                        renamed_endpoints.add(endpoint)

                    phase = "namespace-set-mtu"
                    namespace.link("set", index=index, mtu=mtu)
                    phase = "namespace-set-up"
                    namespace.link("set", index=index, state="up")
                    if netem is not None:
                        phase = "namespace-set-netem"
                        if netem.rate is not None:
                            namespace.tc(
                                "add",
                                "netem",
                                index,
                                "1:",
                                request_filter=_netem_request_filter(index, netem),
                            )
                        else:
                            namespace.tc(
                                "add",
                                "netem",
                                index,
                                "1:",
                                delay=netem.delay_ms * 1000,
                                jitter=netem.jitter_ms * 1000,
                                loss=netem.loss_percent,
                            )
                    elif qdisc is not None:
                        phase = "namespace-set-qdisc"
                        if isinstance(qdisc, TbfPlan):
                            namespace.tc(
                                "add",
                                "tbf",
                                index,
                                "1:",
                                rate=qdisc.rate,
                                burst=qdisc.burst_bytes,
                                latency=f"{qdisc.latency_ms}ms",
                            )
                        else:
                            assert isinstance(qdisc, FqCodelPlan)
                            namespace.tc(
                                "add",
                                "fq_codel",
                                index,
                                "1:",
                                fqc_limit=qdisc.limit,
                                fqc_target=f"{qdisc.target_ms}ms",
                                fqc_interval=f"{qdisc.interval_ms}ms",
                                fqc_ecn=int(qdisc.ecn),
                            )
                return
            except (Exception, KeyboardInterrupt) as error:
                if not _is_transient_veth_error(error):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    missing = _veth_missing_error(
                        "create_veth",
                        resource,
                        phase=phase,
                        interface=(
                            endpoint.interface
                            if endpoint in renamed_endpoints
                            else endpoint.temporary_name
                        ),
                        namespace=endpoint.namespace,
                        cause=error,
                    )
                    raise missing from error
                time.sleep(min(_VETH_VISIBILITY_INTERVAL, remaining))

    def _cleanup_veth(
        self,
        link: LinkPlan,
        moved_endpoints: set[EndpointPlan],
        renamed_endpoints: set[EndpointPlan],
        ownership_token: str,
    ) -> None:
        root_names: list[str] = []
        namespace_names: dict[str, list[str]] = {}
        for endpoint in (link.left, link.right):
            if endpoint in renamed_endpoints:
                namespace_names.setdefault(endpoint.namespace, []).append(endpoint.interface)
            elif endpoint in moved_endpoints:
                namespace_names.setdefault(endpoint.namespace, []).append(endpoint.temporary_name)
            else:
                root_names.append(endpoint.temporary_name)

        if root_names:
            self._cleanup_veth_location(
                self._iproute_factory,
                root_names,
                ownership_token,
            )
        for namespace, names in namespace_names.items():
            self._cleanup_veth_location(
                partial(self._netns_factory, namespace),
                names,
                ownership_token,
            )

    @staticmethod
    def _cleanup_veth_location(
        factory: Callable[[], Any],
        names: Sequence[str],
        ownership_token: str,
    ) -> None:
        try:
            with _managed_handle(factory()) as handle:
                for name in names:
                    try:
                        indexes = handle.link_lookup(ifname=name)
                        if indexes:
                            index = int(indexes[0])
                            messages = handle.get_links(index)
                            if not messages:
                                continue
                            alias = _attribute(messages[0], "IFLA_IFALIAS")
                            if alias != ownership_token:
                                continue
                            handle.link("del", index=index)
                            return
                    except (Exception, KeyboardInterrupt):
                        continue
        except (Exception, KeyboardInterrupt):
            return

    def configure_node(self, node: NodePlan, plan: TopologyPlan) -> None:
        try:
            with _managed_handle(self._netns_factory(node.namespace)) as namespace:
                indexes = {
                    interface: _required_index(
                        namespace,
                        interface,
                        "configure_node",
                        f"{node.namespace}:{interface}",
                    )
                    for interface in node.interfaces
                }
                for interface, mac in node.mac_addresses.items():
                    if interface in node.devices:
                        continue
                    index = indexes.get(interface)
                    if index is None:
                        index = _required_index(
                            namespace,
                            interface,
                            "configure_node",
                            f"{node.namespace}:{interface}",
                        )
                        indexes[interface] = index
                    namespace.link("set", index=index, address=mac)

                for device in node.devices.values():
                    if not isinstance(device, BondDevicePlan):
                        continue
                    bond_arguments: dict[str, object] = {
                        "ifname": device.name,
                        "kind": "bond",
                        "bond_mode": _BOND_MODE_TO_NETLINK[device.mode],
                        "bond_miimon": device.miimon_ms,
                    }
                    if device.lacp_rate is not None:
                        bond_arguments["bond_ad_lacp_rate"] = _BOND_LACP_RATE_TO_NETLINK[
                            device.lacp_rate
                        ]
                    if device.xmit_hash_policy is not None:
                        bond_arguments["bond_xmit_hash_policy"] = _BOND_XMIT_HASH_POLICY_TO_NETLINK[
                            device.xmit_hash_policy
                        ]
                    if device.min_links is not None:
                        bond_arguments["bond_min_links"] = device.min_links
                    namespace.link("add", **bond_arguments)
                    bond_index = _required_index(
                        namespace,
                        device.name,
                        "configure_node",
                        f"{node.namespace}:{device.name}",
                    )
                    indexes[device.name] = bond_index
                    namespace.link(
                        "set",
                        index=bond_index,
                        mtu=bond_device_mtu(node, plan, device),
                    )
                    for member in device.interfaces:
                        member_index = indexes.get(member)
                        if member_index is None:
                            member_index = _required_index(
                                namespace,
                                member,
                                "configure_node",
                                f"{node.namespace}:{member}",
                            )
                            indexes[member] = member_index
                        namespace.link("set", index=member_index, state="down")
                        namespace.link("set", index=member_index, master=bond_index)
                        namespace.link("set", index=member_index, state="up")
                    if device.primary is not None:
                        namespace.link(
                            "set",
                            index=bond_index,
                            kind="bond",
                            bond_primary=indexes[device.primary],
                        )

                for device in node.devices.values():
                    if not isinstance(device, VrfDevicePlan):
                        continue
                    namespace.link(
                        "add",
                        ifname=device.name,
                        kind="vrf",
                        vrf_table=device.table,
                    )
                    indexes[device.name] = _required_index(
                        namespace,
                        device.name,
                        "configure_node",
                        f"{node.namespace}:{device.name}",
                    )

                for device in node.devices.values():
                    if not isinstance(device, VlanDevicePlan):
                        continue
                    parent_index = indexes.get(device.link)
                    if parent_index is None:
                        parent_index = _required_index(
                            namespace,
                            device.link,
                            "configure_node",
                            f"{node.namespace}:{device.link}",
                        )
                        indexes[device.link] = parent_index
                    namespace.link(
                        "add",
                        ifname=device.name,
                        kind="vlan",
                        link=parent_index,
                        vlan_id=device.vlan_id,
                    )
                    indexes[device.name] = _required_index(
                        namespace,
                        device.name,
                        "configure_node",
                        f"{node.namespace}:{device.name}",
                    )

                for device in node.devices.values():
                    if not isinstance(device, VxlanDevicePlan):
                        continue
                    underlay_index = indexes.get(device.link)
                    if underlay_index is None:
                        underlay_index = _required_index(
                            namespace,
                            device.link,
                            "configure_node",
                            f"{node.namespace}:{device.link}",
                        )
                        indexes[device.link] = underlay_index
                    vxlan_arguments: dict[str, object] = {
                        "ifname": device.name,
                        "kind": "vxlan",
                        "vxlan_id": device.vni,
                        "vxlan_link": underlay_index,
                        "vxlan_port": device.dst_port,
                        "vxlan_learning": int(device.learning),
                    }
                    if device.local.version == 4:
                        vxlan_arguments.update(
                            vxlan_local=str(device.local),
                            vxlan_group=str(device.remote),
                        )
                    else:
                        vxlan_arguments.update(
                            vxlan_local6=str(device.local),
                            vxlan_group6=str(device.remote),
                        )
                    namespace.link("add", **vxlan_arguments)
                    vxlan_index = _required_index(
                        namespace,
                        device.name,
                        "configure_node",
                        f"{node.namespace}:{device.name}",
                    )
                    indexes[device.name] = vxlan_index
                    namespace.link(
                        "set",
                        index=vxlan_index,
                        mtu=vxlan_device_mtu(node, plan, device),
                    )
                    namespace.link("set", index=vxlan_index, state="up")

                for device in node.devices.values():
                    if not isinstance(device, DummyDevicePlan):
                        continue
                    namespace.link("add", ifname=device.name, kind="dummy")
                    dummy_index = _required_index(
                        namespace,
                        device.name,
                        "configure_node",
                        f"{node.namespace}:{device.name}",
                    )
                    indexes[device.name] = dummy_index
                    namespace.link(
                        "set",
                        index=dummy_index,
                        mtu=dummy_device_mtu(node, plan, device),
                        state="up",
                    )

                for device in node.devices.values():
                    if not isinstance(device, GreDevicePlan):
                        continue
                    underlay_index = indexes.get(device.link)
                    if underlay_index is None:
                        underlay_index = _required_index(
                            namespace,
                            device.link,
                            "configure_node",
                            f"{node.namespace}:{device.link}",
                        )
                        indexes[device.link] = underlay_index
                    gre_arguments: dict[str, object] = {
                        "ifname": device.name,
                        "kind": "gre",
                        "gre_link": underlay_index,
                        "gre_local": str(device.local),
                        "gre_remote": str(device.remote),
                        "gre_ttl": device.ttl,
                    }
                    if device.key is not None:
                        gre_arguments.update(
                            gre_ikey=device.key,
                            gre_okey=device.key,
                            gre_iflags=_GRE_KEY_NETLINK_FLAG,
                            gre_oflags=_GRE_KEY_NETLINK_FLAG,
                        )
                    namespace.link("add", **gre_arguments)
                    gre_index = _required_index(
                        namespace,
                        device.name,
                        "configure_node",
                        f"{node.namespace}:{device.name}",
                    )
                    indexes[device.name] = gre_index
                    namespace.link(
                        "set",
                        index=gre_index,
                        mtu=gre_device_mtu(node, plan, device),
                        state="up",
                    )

                for device in node.devices.values():
                    if not isinstance(device, IpipDevicePlan):
                        continue
                    underlay_index = indexes.get(device.link)
                    if underlay_index is None:
                        underlay_index = _required_index(
                            namespace,
                            device.link,
                            "configure_node",
                            f"{node.namespace}:{device.link}",
                        )
                        indexes[device.link] = underlay_index
                    namespace.link(
                        "add",
                        ifname=device.name,
                        kind="ipip",
                        ipip_link=underlay_index,
                        ipip_local=str(device.local),
                        ipip_remote=str(device.remote),
                        ipip_ttl=device.ttl,
                    )
                    ipip_index = _required_index(
                        namespace,
                        device.name,
                        "configure_node",
                        f"{node.namespace}:{device.name}",
                    )
                    indexes[device.name] = ipip_index
                    namespace.link(
                        "set",
                        index=ipip_index,
                        mtu=ipip_device_mtu(node, plan, device),
                        state="up",
                    )

                for device in node.devices.values():
                    if not isinstance(device, GeneveDevicePlan):
                        continue
                    # Geneve has no IFLA_GENEVE_LINK attribute.  The kernel
                    # selects the egress interface by routing the remote
                    # endpoint, so ``link`` is a declarative underlay
                    # reference used for validation and MTU calculation.
                    if device.link not in indexes:
                        indexes[device.link] = _required_index(
                            namespace,
                            device.link,
                            "configure_node",
                            f"{node.namespace}:{device.link}",
                        )
                    geneve_arguments: dict[str, object] = {
                        "ifname": device.name,
                        "kind": "geneve",
                        "geneve_id": device.vni,
                        "geneve_port": device.dst_port,
                    }
                    if device.remote.version == 4:
                        geneve_arguments["geneve_remote"] = str(device.remote)
                    else:
                        geneve_arguments["geneve_remote6"] = str(device.remote)
                    namespace.link("add", **geneve_arguments)
                    geneve_index = _required_index(
                        namespace,
                        device.name,
                        "configure_node",
                        f"{node.namespace}:{device.name}",
                    )
                    indexes[device.name] = geneve_index
                    namespace.link(
                        "set",
                        index=geneve_index,
                        mtu=geneve_device_mtu(node, plan, device),
                        state="up",
                    )

                for device in node.devices.values():
                    if not isinstance(device, MacvlanDevicePlan):
                        continue
                    parent_index = indexes.get(device.link)
                    if parent_index is None:
                        parent_index = _required_index(
                            namespace,
                            device.link,
                            "configure_node",
                            f"{node.namespace}:{device.link}",
                        )
                        indexes[device.link] = parent_index
                    namespace.link(
                        "add",
                        ifname=device.name,
                        kind="macvlan",
                        link=parent_index,
                        macvlan_mode=_MACVLAN_MODE_TO_NETLINK[device.mode],
                    )
                    macvlan_index = _required_index(
                        namespace,
                        device.name,
                        "configure_node",
                        f"{node.namespace}:{device.name}",
                    )
                    indexes[device.name] = macvlan_index
                    namespace.link(
                        "set",
                        index=macvlan_index,
                        mtu=macvlan_device_mtu(node, plan, device),
                        state="up",
                    )

                for device in node.devices.values():
                    if not isinstance(device, IpvlanDevicePlan):
                        continue
                    parent_index = indexes.get(device.link)
                    if parent_index is None:
                        parent_index = _required_index(
                            namespace,
                            device.link,
                            "configure_node",
                            f"{node.namespace}:{device.link}",
                        )
                        indexes[device.link] = parent_index
                    namespace.link(
                        "add",
                        ifname=device.name,
                        kind="ipvlan",
                        link=parent_index,
                        ipvlan_mode=_IPVLAN_MODE_TO_NETLINK[device.mode],
                    )
                    ipvlan_index = _required_index(
                        namespace,
                        device.name,
                        "configure_node",
                        f"{node.namespace}:{device.name}",
                    )
                    indexes[device.name] = ipvlan_index
                    namespace.link(
                        "set",
                        index=ipvlan_index,
                        mtu=ipvlan_device_mtu(node, plan, device),
                        state="up",
                    )

                for interface, mac in node.mac_addresses.items():
                    if interface not in node.devices:
                        continue
                    namespace.link("set", index=indexes[interface], address=mac)

                for device in node.devices.values():
                    if not isinstance(device, VrfDevicePlan):
                        continue
                    vrf_index = indexes[device.name]
                    namespace.link("set", index=vrf_index, state="up")
                    for member in device.interfaces:
                        # Refresh the index after all devices have been created.
                        # This avoids using a stale lookup result on kernels that
                        # recycle interface indexes while veths are moved.
                        member_index = _required_index(
                            namespace,
                            member,
                            "configure_node",
                            f"{node.namespace}:{member}",
                        )
                        indexes[member] = member_index
                        # Linux VRF accepts a member in either state on most
                        # kernels, but older runner kernels reject an UP veth.
                        # Keep the attach sequence deterministic and let the
                        # address pass below bring the member back up.
                        namespace.link("set", index=member_index, state="down")
                        namespace.link("set", index=member_index, master=vrf_index)

                if node.kind == "bridge":
                    bridge_name = node.bridge_name
                    assert bridge_name is not None
                    bridge_index = indexes.get(bridge_name)
                    if bridge_index is None:
                        bridge_index = _required_index(
                            namespace,
                            bridge_name,
                            "configure_node",
                            f"{node.namespace}:{bridge_name}",
                        )
                        indexes[bridge_name] = bridge_index
                    for interface in self._node_link_interfaces(node, plan):
                        port_index = _required_index(
                            namespace,
                            interface,
                            "configure_node",
                            f"{node.namespace}:{interface}",
                        )
                        namespace.link(
                            "set",
                            index=port_index,
                            master=bridge_index,
                        )
                        port = node.bridge_ports.get(interface)
                        if port is not None:
                            port_arguments: dict[str, object] = {
                                "index": port_index,
                                "kind": "bridge_slave",
                            }
                            if port.path_cost is not None:
                                port_arguments["cost"] = port.path_cost
                            if port.priority is not None:
                                port_arguments["priority"] = port.priority
                            if port.hairpin is not None:
                                port_arguments["mode"] = int(port.hairpin)
                            if port.isolated is not None:
                                port_arguments["isolated"] = int(port.isolated)
                            if port.learning is not None:
                                port_arguments["learning"] = int(port.learning)
                            if port.flood is not None:
                                port_arguments["unicast_flood"] = int(port.flood)
                            if port.multicast_flood is not None:
                                port_arguments["mcast_flood"] = int(port.multicast_flood)
                            namespace.link("set", **port_arguments)
                            if port.vlans:
                                namespace.vlan_filter(
                                    "del",
                                    index=port_index,
                                    vlan_info={"vid": 1},
                                )
                                for vlan in port.vlans:
                                    flags = 0
                                    if vlan.pvid:
                                        flags |= _BRIDGE_VLAN_INFO_PVID
                                    if vlan.untagged:
                                        flags |= _BRIDGE_VLAN_INFO_UNTAGGED
                                    namespace.vlan_filter(
                                        "add",
                                        index=port_index,
                                        vlan_info={"vid": vlan.vid, "flags": flags},
                                    )
                        if isinstance(
                            node.devices.get(interface), (VxlanDevicePlan, GeneveDevicePlan)
                        ):
                            namespace.link("set", index=port_index, state="up")
                    if bridge_name not in node.interfaces:
                        namespace.link("set", index=bridge_index, state="up")

                for interface, addresses in node_interface_addresses(node).items():
                    index = indexes[interface]
                    for address in addresses:
                        address_arguments: dict[str, object] = {
                            "index": index,
                            "address": str(address.ip),
                            "prefixlen": address.network.prefixlen,
                        }
                        if address.version == 6:
                            address_arguments["family"] = socket.AF_INET6
                        namespace.addr("add", **address_arguments)
                    namespace.link("set", index=index, state="up")

                for route in node.routes:
                    route_arguments: dict[str, object] = {"dst": str(route.dst)}
                    if route.nexthops:
                        multipath: list[dict[str, object]] = []
                        for nexthop in route.nexthops:
                            route_index = indexes.get(nexthop.dev)
                            if route_index is None:
                                route_index = _required_index(
                                    namespace,
                                    nexthop.dev,
                                    "configure_node",
                                    f"{node.namespace}:{nexthop.dev}",
                                )
                                indexes[nexthop.dev] = route_index
                            item: dict[str, object] = {
                                "oif": route_index,
                                "hops": nexthop.weight - 1,
                            }
                            if nexthop.via is not None:
                                item["gateway"] = str(nexthop.via)
                            multipath.append(item)
                        route_arguments["multipath"] = multipath
                    else:
                        assert route.dev is not None
                        route_index = indexes.get(route.dev)
                        if route_index is None:
                            route_index = _required_index(
                                namespace,
                                route.dev,
                                "configure_node",
                                f"{node.namespace}:{route.dev}",
                            )
                            indexes[route.dev] = route_index
                        route_arguments["oif"] = route_index
                        if route.via is not None:
                            route_arguments["gateway"] = str(route.via)
                    if route.dst.version == 6:
                        route_arguments["family"] = socket.AF_INET6
                    if route.table != _RT_TABLE_MAIN:
                        route_arguments["table"] = route.table
                    namespace.route("add", **route_arguments)

                for neighbor in node.neighbors:
                    neighbor_index = indexes.get(neighbor.dev)
                    if neighbor_index is None:
                        neighbor_index = _required_index(
                            namespace,
                            neighbor.dev,
                            "configure_node",
                            f"{node.namespace}:{neighbor.dev}",
                        )
                        indexes[neighbor.dev] = neighbor_index
                    neighbor_arguments: dict[str, object] = {
                        "family": (
                            socket.AF_INET if neighbor.dst.version == 4 else socket.AF_INET6
                        ),
                        "dst": str(neighbor.dst),
                        "ifindex": neighbor_index,
                    }
                    if neighbor.proxy:
                        neighbor_arguments.update(flags=_NTF_PROXY, state=_NUD_NONE)
                    else:
                        assert neighbor.lladdr is not None
                        assert neighbor.state is not None
                        neighbor_arguments.update(
                            lladdr=neighbor.lladdr,
                            state=_NEIGHBOR_STATE_TO_NETLINK[neighbor.state],
                        )
                    namespace.neigh("add", **neighbor_arguments)

                for rule in sorted(
                    node.rules,
                    key=lambda item: (item.family, -item.priority),
                ):
                    rule_arguments: dict[str, object] = {
                        "family": socket.AF_INET if rule.family == 4 else socket.AF_INET6,
                        "priority": rule.priority,
                        "action": _RULE_ACTION_TO_NETLINK[rule.action],
                    }
                    if rule.table is not None:
                        rule_arguments["table"] = rule.table
                    if rule.goto is not None:
                        rule_arguments["goto"] = rule.goto
                    if rule.source is not None:
                        rule_arguments.update(
                            src=str(rule.source.network_address),
                            src_len=rule.source.prefixlen,
                        )
                    if rule.destination is not None:
                        rule_arguments.update(
                            dst=str(rule.destination.network_address),
                            dst_len=rule.destination.prefixlen,
                        )
                    if rule.invert:
                        rule_arguments["flags"] = _FIB_RULE_INVERT
                    if rule.tos is not None:
                        rule_arguments["tos"] = rule.tos
                    for key, value in (
                        ("fwmark", rule.fwmark),
                        ("fwmask", rule.fwmask),
                        ("iifname", rule.iif),
                        ("oifname", rule.oif),
                        ("ip_proto", rule.ip_protocol),
                        ("tun_id", rule.tunnel_id),
                        ("suppress_prefixlen", rule.suppress_prefix_length),
                        ("suppress_ifgroup", rule.suppress_interface_group),
                    ):
                        if value is not None:
                            rule_arguments[key] = value
                    if rule.l3mdev:
                        rule_arguments["l3mdev"] = 1
                    if rule.uid_range is not None:
                        rule_arguments["uid_range"] = f"{rule.uid_range[0]}:{rule.uid_range[1]}"
                    if rule.protocol:
                        rule_arguments["protocol"] = rule.protocol
                    if rule.source_port is not None:
                        rule_arguments["sport_range"] = (
                            f"{rule.source_port[0]}:{rule.source_port[1]}"
                        )
                    if rule.destination_port is not None:
                        rule_arguments["dport_range"] = (
                            f"{rule.destination_port[0]}:{rule.destination_port[1]}"
                        )
                    if rule.realms is not None:
                        rule_arguments["flow"] = (rule.realms[0] << 16) | rule.realms[1]
                    namespace.rule("add", **rule_arguments)
        except NetlinkError as error:
            raise _translate_netlink_error(
                error,
                operation="configure_node",
                resource=node.namespace,
            ) from error

        self._write_sysctls(node)

    @staticmethod
    def _node_link_interfaces(node: NodePlan, plan: TopologyPlan) -> tuple[str, ...]:
        linked_interfaces = (
            endpoint.interface
            for link in plan.links
            for endpoint in (link.left, link.right)
            if endpoint.namespace == node.namespace
            and node_interface_master(node, endpoint.interface) == node.bridge_name
        )
        overlay_interfaces = (
            device.name
            for device in node.devices.values()
            if isinstance(device, (VxlanDevicePlan, GeneveDevicePlan))
        )
        return tuple(dict.fromkeys((*linked_interfaces, *overlay_interfaces)))

    def _write_sysctls(self, node: NodePlan) -> None:
        if not node.sysctls:
            return
        self._pushns(node.namespace)
        with _cleanup_context(self._popns, "namespace pop"):
            for key, value in node.sysctls.items():
                path = self._sysctl_root.joinpath(*key.split("."))
                path.write_text(f"{value}\n", encoding="ascii")

    def start_routing(self, plan: TopologyPlan) -> None:
        self._routing_runtime.start(plan)

    def stop_routing(self, plan: TopologyPlan) -> None:
        self._routing_runtime.stop(plan)

    def routing_ready(self, plan: TopologyPlan) -> bool:
        return self._routing_runtime.ready(plan)

    def inventory(self, plan: TopologyPlan) -> LiveInventory:
        root_interfaces = self._inventory_root_interfaces(plan)
        inspect_qdiscs = any(
            link.netem is not None or link.qdisc is not None for link in plan.links
        )
        namespaces: dict[str, NamespaceInventory] = {}
        for node in plan.nodes.values():
            try:
                namespace = self._netns_factory(node.namespace)
            except FileNotFoundError:
                observed = self._absent_namespace(node)
            except NetlinkError as error:
                if _is_missing_error(error.code):
                    observed = self._absent_namespace(node)
                else:
                    raise _translate_netlink_error(
                        error,
                        operation="inventory",
                        resource=node.namespace,
                    ) from error
            except OSError as error:
                if error.errno is not None and _is_missing_error(error.errno):
                    observed = self._absent_namespace(node)
                else:
                    raise _translate_os_error(
                        error,
                        operation="inventory",
                        resource=node.namespace,
                    ) from error
            else:
                try:
                    observed = self._inventory_namespace(
                        node,
                        plan,
                        namespace,
                        inspect_qdiscs=inspect_qdiscs,
                    )
                except NetlinkError as error:
                    raise _translate_netlink_error(
                        error,
                        operation="inventory",
                        resource=node.namespace,
                    ) from error
                except OSError as error:
                    raise _translate_os_error(
                        error,
                        operation="inventory",
                        resource=node.namespace,
                    ) from error
            namespaces[node.namespace] = observed
        return LiveInventory(
            namespaces=namespaces,
            root_interfaces=root_interfaces,
        )

    def _inventory_root_interfaces(
        self,
        plan: TopologyPlan,
    ) -> dict[str, InterfaceInventory]:
        temporary_names = tuple(
            dict.fromkeys(
                endpoint.temporary_name
                for link in plan.links
                for endpoint in (link.left, link.right)
            )
        )
        if not temporary_names:
            return {}

        observed: dict[str, InterfaceInventory] = {}
        resource = f"root:{temporary_names[0]}"
        try:
            with _managed_handle(self._iproute_factory()) as root:
                for temporary_name in temporary_names:
                    resource = f"root:{temporary_name}"
                    try:
                        indexes = root.link_lookup(ifname=temporary_name)
                        if not indexes:
                            continue
                        index = int(indexes[0])
                        messages = tuple(root.get_links(index))
                    except NetlinkError as error:
                        if _is_missing_error(error.code):
                            continue
                        raise
                    except OSError as error:
                        if error.errno is not None and _is_missing_error(error.errno):
                            continue
                        raise
                    interfaces, _ = self._inventory_interfaces(messages, ())
                    interface = interfaces.get(temporary_name)
                    if interface is not None and interface.ifindex == index:
                        observed[temporary_name] = interface
        except NetlinkError as error:
            raise _translate_netlink_error(
                error,
                operation="inventory",
                resource=resource,
            ) from error
        except OSError as error:
            raise _translate_os_error(
                error,
                operation="inventory",
                resource=resource,
            ) from error
        return observed

    def _inventory_namespace(
        self,
        node: NodePlan,
        plan: TopologyPlan,
        namespace: Any,
        *,
        inspect_qdiscs: bool,
    ) -> NamespaceInventory:
        neighbor_messages: tuple[Any, ...] = ()
        proxy_neighbor_messages: list[Any] = []
        with _managed_handle(namespace) as handle:
            link_messages = tuple(handle.get_links())
            families = self._inventory_families(node)
            address_messages = tuple(
                message for family in families for message in handle.get_addr(family=family)
            )
            vlan_messages = (
                tuple(handle.get_vlans()) if node.kind == "bridge" and node.vlan_filtering else ()
            )
            qdisc_messages = tuple(handle.get_qdiscs()) if inspect_qdiscs else ()
            route_messages = tuple(
                (
                    family,
                    table,
                    tuple(
                        handle.get_routes(
                            family=family,
                            table=table,
                        )
                    ),
                )
                for family in families
                for table in node_route_tables(node)
            )
            rule_families = tuple(
                dict.fromkeys(
                    socket.AF_INET if rule.family == 4 else socket.AF_INET6 for rule in node.rules
                )
            )
            rule_messages = tuple(
                (family, tuple(handle.get_rules(family=family))) for family in rule_families
            )
            regular_neighbor_families = tuple(
                dict.fromkeys(
                    socket.AF_INET if neighbor.dst.version == 4 else socket.AF_INET6
                    for neighbor in node.neighbors
                    if not neighbor.proxy
                )
            )
            neighbor_messages = tuple(
                message
                for family in regular_neighbor_families
                for message in handle.get_neighbours(family=family)
            )
            indexes_by_name = {
                str(name): int(_value(message, "index"))
                for message in link_messages
                if (name := _attribute(message, "IFLA_IFNAME")) is not None
            }
            for neighbor in node.neighbors:
                if not neighbor.proxy:
                    continue
                index = indexes_by_name.get(neighbor.dev)
                if index is None:
                    continue
                try:
                    messages = handle.neigh(
                        "get",
                        family=(socket.AF_INET if neighbor.dst.version == 4 else socket.AF_INET6),
                        dst=str(neighbor.dst),
                        ifindex=index,
                        flags=_NTF_PROXY,
                    )
                except NetlinkError as error:
                    if _is_missing_error(error.code):
                        continue
                    raise
                proxy_neighbor_messages.extend(messages)
        interfaces, names_by_index = self._inventory_interfaces(
            link_messages,
            address_messages,
            vlan_messages,
            qdisc_messages,
            namespace=node.namespace,
            declared_addresses=node_interface_addresses(node),
            declared_netem_interfaces={
                endpoint.interface
                for link in plan.links
                if link.netem is not None
                for endpoint in (link.left, link.right)
                if endpoint.namespace == node.namespace
            },
            declared_qdiscs={
                endpoint.interface: link.qdisc
                for link in plan.links
                if link.qdisc is not None
                for endpoint in (link.left, link.right)
                if endpoint.namespace == node.namespace
            },
        )
        observed_routes = tuple(
            route
            for family, table, messages in route_messages
            for route in self._inventory_routes(
                messages,
                names_by_index,
                interfaces,
                node.namespace,
                family=family,
                expected_table=table,
                allow_dynamic=node.routing is not None,
            )
        )
        routes = tuple(
            dict.fromkeys(
                (
                    *self._inventory_connected_routes(interfaces),
                    *observed_routes,
                )
            )
        )
        rules = tuple(
            rule
            for family, messages in rule_messages
            for rule in self._inventory_rules(
                messages,
                node.namespace,
                family=family,
                ignore_l3mdev_kernel_rule=any(
                    isinstance(device, VrfDevicePlan) for device in node.devices.values()
                ),
            )
        )
        neighbors = self._inventory_neighbors(
            (*neighbor_messages, *proxy_neighbor_messages),
            names_by_index,
            node.neighbors,
            node.namespace,
        )
        sysctls = self._read_sysctls(node)
        return NamespaceInventory(
            node=node.name,
            kind=node.kind,
            namespace=node.namespace,
            exists=True,
            interfaces=interfaces,
            routes=routes,
            sysctls=sysctls,
            rules=rules,
            neighbors=neighbors,
        )

    @staticmethod
    def _inventory_families(node: NodePlan) -> tuple[int, ...]:
        has_ipv6 = (
            any(
                address.version == 6
                for addresses in node_interface_addresses(node).values()
                for address in addresses
            )
            or any(route.dst.version == 6 for route in node.routes)
            or any(rule.family == 6 for rule in node.rules)
            or any(neighbor.dst.version == 6 for neighbor in node.neighbors)
            or "net.ipv6.conf.all.forwarding" in node.sysctls
        )
        if has_ipv6:
            return socket.AF_INET, socket.AF_INET6
        return (socket.AF_INET,)

    @staticmethod
    def _absent_namespace(node: NodePlan) -> NamespaceInventory:
        return NamespaceInventory(
            node=node.name,
            kind=node.kind,
            namespace=node.namespace,
            exists=False,
            interfaces={},
            routes=(),
            sysctls={},
            rules=(),
            neighbors=(),
        )

    @staticmethod
    def _inventory_interfaces(
        link_messages: Sequence[Any],
        address_messages: Sequence[Any],
        vlan_messages: Sequence[Any] = (),
        qdisc_messages: Sequence[Any] = (),
        *,
        namespace: str = "network namespace",
        declared_addresses: Mapping[str, Sequence[IPInterface]] | None = None,
        declared_netem_interfaces: set[str] | None = None,
        declared_qdiscs: Mapping[str, QdiscPlan | None] | None = None,
    ) -> tuple[dict[str, InterfaceInventory], dict[int, str]]:
        names_by_index: dict[int, str] = {}
        for message in link_messages:
            name = _attribute(message, "IFLA_IFNAME")
            if name is None:
                continue
            names_by_index[int(_value(message, "index"))] = str(name)

        addresses_by_index: dict[int, list[IPInterface]] = {}
        for message in address_messages:
            address = _attribute(message, "IFA_LOCAL")
            if address is None:
                address = _attribute(message, "IFA_ADDRESS")
            if address is None:
                continue
            index = int(_value(message, "index"))
            prefixlen = int(_value(message, "prefixlen"))
            observed = ip_interface(f"{address}/{prefixlen}")
            interface_name = names_by_index.get(index)
            declared = (
                ()
                if declared_addresses is None or interface_name is None
                else declared_addresses.get(interface_name, ())
            )
            if (
                observed.version == 6
                and (observed.ip.is_link_local or observed.ip.is_loopback)
                and observed not in declared
            ):
                continue
            addresses = addresses_by_index.setdefault(index, [])
            if observed not in addresses:
                addresses.append(observed)

        vlans_by_index: dict[int, list[BridgeVlanPlan]] = {}
        for message in vlan_messages:
            index = int(_value(message, "index"))
            af_spec = _attribute(message, "IFLA_AF_SPEC")
            for attribute in _value(af_spec, "attrs", ()):
                pair = _attribute_pair(attribute)
                if pair is None or pair[0] != "IFLA_BRIDGE_VLAN_INFO":
                    continue
                value = pair[1]
                vid = int(_value(value, "vid"))
                flags = int(_value(value, "flags", 0))
                vlan = BridgeVlanPlan(
                    vid=vid,
                    pvid=bool(flags & _BRIDGE_VLAN_INFO_PVID),
                    untagged=bool(flags & _BRIDGE_VLAN_INFO_UNTAGGED),
                )
                entries = vlans_by_index.setdefault(index, [])
                if vlan not in entries:
                    entries.append(vlan)

        netem_by_index: dict[int, NetemPlan] = {}
        qdisc_by_index: dict[int, QdiscPlan] = {}
        declared_names: set[str] | None = None
        if declared_netem_interfaces is not None or declared_qdiscs is not None:
            declared_names = set(declared_netem_interfaces or ()) | set(declared_qdiscs or ())

        for message in qdisc_messages:
            if not _is_root_qdisc(message):
                continue
            index = int(_value(message, "index"))
            interface_name = names_by_index.get(index)
            if declared_names is not None and interface_name not in declared_names:
                continue
            kind = _attribute(message, "TCA_KIND")
            if kind not in {"netem", "tbf", "fq_codel"}:
                continue
            options = _attribute(message, "TCA_OPTIONS")
            if options is None:
                raise _unsupported_inventory_qdisc(namespace, "missing_options")

            if kind == "netem":
                if index in netem_by_index or index in qdisc_by_index:
                    raise _unsupported_inventory_qdisc(namespace, "multiple_root_qdiscs")
                try:
                    limit = int(_qdisc_option(options, "limit", 0))
                    gap = int(_qdisc_option(options, "gap", 0))
                    duplicate = int(_qdisc_option(options, "duplicate", 0))
                    raw_delay = int(_qdisc_option(options, "delay", 0))
                    raw_jitter = int(_qdisc_option(options, "jitter", 0))
                    raw_loss = int(_qdisc_option(options, "loss", 0))
                except (TypeError, ValueError):
                    raise _unsupported_inventory_qdisc(namespace, "invalid_options") from None
                if limit != 1000:
                    raise _unsupported_inventory_qdisc(namespace, "limit")
                if gap != 0:
                    raise _unsupported_inventory_qdisc(namespace, "gap")
                if duplicate != 0:
                    raise _unsupported_inventory_qdisc(namespace, "duplicate")

                ticks_per_ms = float(time2tick(1000))
                if ticks_per_ms <= 0:
                    raise _unsupported_inventory_qdisc(namespace, "clock")
                delay_ms = round(raw_delay / ticks_per_ms)
                jitter_ms = round(raw_jitter / ticks_per_ms)
                if int(time2tick(delay_ms * 1000)) != raw_delay:
                    raise _unsupported_inventory_qdisc(namespace, "delay_precision")
                if int(time2tick(jitter_ms * 1000)) != raw_jitter:
                    raise _unsupported_inventory_qdisc(namespace, "jitter_precision")
                loss_percent = round(raw_loss * 100 / (2**32 - 1))
                if percent2u32(loss_percent) != raw_loss:
                    raise _unsupported_inventory_qdisc(namespace, "loss_precision")

                rate = None
                nested_rate = _attribute(options, "TCA_NETEM_RATE")
                if nested_rate is not None:
                    raw_rate = int(_qdisc_option(nested_rate, "rate", 0))
                    packet_overhead = int(_qdisc_option(nested_rate, "packet_overhead", 0))
                    cell_size = int(_qdisc_option(nested_rate, "cell_size", 0))
                    cell_overhead = int(_qdisc_option(nested_rate, "cell_overhead", 0))
                    if packet_overhead or cell_size or cell_overhead:
                        raise _unsupported_inventory_qdisc(namespace, "extended_options")
                    if raw_rate:
                        try:
                            rate = format_rate(raw_rate)
                        except ValueError:
                            raise _unsupported_inventory_qdisc(namespace, "rate") from None
                for attribute_name, field_names in (
                    ("TCA_NETEM_CORR", ("delay_corr", "loss_corr", "dup_corr")),
                    ("TCA_NETEM_REORDER", ("prob_reorder", "corr_reorder")),
                    ("TCA_NETEM_CORRUPT", ("prob_corrupt", "corr_corrupt")),
                ):
                    nested = _attribute(options, attribute_name)
                    if nested is not None and any(
                        int(_value(nested, field_name, 0)) != 0 for field_name in field_names
                    ):
                        raise _unsupported_inventory_qdisc(namespace, "extended_options")
                netem_by_index[index] = NetemPlan(
                    delay_ms=delay_ms,
                    jitter_ms=jitter_ms,
                    loss_percent=loss_percent,
                    rate=rate,
                )
                continue

            if index in netem_by_index or index in qdisc_by_index:
                raise _unsupported_inventory_qdisc(namespace, "multiple_root_qdiscs")
            if kind == "tbf":
                parameters = _attribute(options, "TCA_TBF_PARMS")
                if parameters is None and _qdisc_option(options, "rate") is not None:
                    parameters = options
                if parameters is None:
                    raise _unsupported_inventory_qdisc(namespace, "missing_parameters")
                try:
                    raw_rate = int(_qdisc_option(parameters, "rate", 0))
                    raw_buffer = int(_qdisc_option(parameters, "buffer", 0))
                    raw_limit = int(_qdisc_option(parameters, "limit", 0))
                except (TypeError, ValueError):
                    raise _unsupported_inventory_qdisc(namespace, "invalid_parameters") from None
                if raw_rate <= 0 or raw_buffer <= 0 or raw_limit <= 0:
                    raise _unsupported_inventory_qdisc(namespace, "parameters")
                try:
                    rate = format_rate(raw_rate)
                except ValueError:
                    raise _unsupported_inventory_qdisc(namespace, "rate") from None
                burst = _decode_tbf_burst(raw_buffer, raw_rate, namespace)
                latency_ms = _decode_tbf_latency(raw_limit, burst, raw_rate, namespace)
                qdisc_by_index[index] = TbfPlan(
                    rate=rate,
                    burst_bytes=burst,
                    latency_ms=latency_ms,
                )
                continue

            # fq_codel stores time values in microseconds. The kernel rounds
            # the values to its internal clock, so a requested 5ms commonly
            # comes back as 4999us (and 100ms as 99999us).
            try:
                raw_target = int(float(_qdisc_option(options, "TCA_FQ_CODEL_TARGET", 5_000)))
                raw_interval = int(float(_qdisc_option(options, "TCA_FQ_CODEL_INTERVAL", 100_000)))
                limit = int(_qdisc_option(options, "TCA_FQ_CODEL_LIMIT", 10_240))
                ecn = bool(int(_qdisc_option(options, "TCA_FQ_CODEL_ECN", 1)))
            except (TypeError, ValueError):
                raise _unsupported_inventory_qdisc(namespace, "invalid_parameters") from None
            target_ms = _decode_milliseconds(raw_target, namespace, "target")
            interval_ms = _decode_milliseconds(raw_interval, namespace, "interval")
            if limit <= 0:
                raise _unsupported_inventory_qdisc(namespace, "parameters")

            # These options are intentionally not part of the manifest. Keep
            # inventory honest if an operator changes them outside nslab.
            unsupported_defaults = (
                ("TCA_FQ_CODEL_FLOWS", 1024),
                ("TCA_FQ_CODEL_QUANTUM", None),
                ("TCA_FQ_CODEL_CE_THRESHOLD", 0),
                ("TCA_FQ_CODEL_DROP_BATCH_SIZE", 64),
                ("TCA_FQ_CODEL_MEMORY_LIMIT", 32 * 1024 * 1024),
            )
            for option_name, default in unsupported_defaults:
                option_value = _qdisc_option(options, option_name)
                if option_value is None:
                    continue
                try:
                    option_value_int = int(option_value)
                except (TypeError, ValueError):
                    raise _unsupported_inventory_qdisc(namespace, "invalid_parameters") from None
                if default is not None and option_value_int != default:
                    raise _unsupported_inventory_qdisc(namespace, "unsupported_options")
            qdisc_by_index[index] = FqCodelPlan(
                target_ms=target_ms,
                interval_ms=interval_ms,
                limit=limit,
                ecn=ecn,
            )

        interfaces: dict[str, InterfaceInventory] = {}
        for message in link_messages:
            index = int(_value(message, "index"))
            name = names_by_index.get(index)
            if name is None:
                continue
            link_info = _attribute(message, "IFLA_LINKINFO")
            kind_value = _attribute(link_info, "IFLA_INFO_KIND")
            if kind_value is None:
                if name == "lo" or int(_value(message, "ifi_type", 0)) == _ARPHRD_LOOPBACK:
                    kind = "loopback"
                else:
                    kind = "unknown"
            else:
                kind = str(kind_value)
            if (name, kind) in _KERNEL_TUNNEL_FALLBACKS:
                continue
            master_value = _attribute(message, "IFLA_MASTER")
            master = names_by_index.get(int(master_value)) if master_value is not None else None
            stp: bool | None = None
            vlan_filtering: bool | None = None
            bridge_priority: int | None = None
            path_cost: int | None = None
            port_priority: int | None = None
            hairpin: bool | None = None
            isolated: bool | None = None
            learning: bool | None = None
            flood: bool | None = None
            multicast_flood: bool | None = None
            parent: str | None = None
            vlan_id: int | None = None
            vrf_table: int | None = None
            bond_mode: str | None = None
            bond_miimon_ms: int | None = None
            bond_primary: str | None = None
            bond_lacp_rate: str | None = None
            bond_xmit_hash_policy: str | None = None
            bond_min_links: int | None = None
            vxlan_vni: int | None = None
            vxlan_link: str | None = None
            vxlan_local: IPv4Address | IPv6Address | None = None
            vxlan_remote: IPv4Address | IPv6Address | None = None
            vxlan_dst_port: int | None = None
            vxlan_learning: bool | None = None
            geneve_vni: int | None = None
            geneve_link: str | None = None
            geneve_remote: IPv4Address | IPv6Address | None = None
            geneve_dst_port: int | None = None
            gre_link: str | None = None
            gre_local: IPv4Address | None = None
            gre_remote: IPv4Address | None = None
            gre_key: int | None = None
            gre_ttl: int | None = None
            ipip_link: str | None = None
            ipip_local: IPv4Address | None = None
            ipip_remote: IPv4Address | None = None
            ipip_ttl: int | None = None
            macvlan_mode: str | None = None
            ipvlan_mode: str | None = None
            if kind == "bridge":
                info_data = _attribute(link_info, "IFLA_INFO_DATA")
                stp_value = _attribute(info_data, "IFLA_BR_STP_STATE")
                vlan_value = _attribute(info_data, "IFLA_BR_VLAN_FILTERING")
                priority_value = _attribute(info_data, "IFLA_BR_PRIORITY")
                if stp_value is not None:
                    stp = bool(int(stp_value))
                if vlan_value is not None:
                    vlan_filtering = bool(int(vlan_value))
                if priority_value is not None:
                    bridge_priority = int(priority_value)
            if kind == "vlan":
                parent_value = _attribute(message, "IFLA_LINK")
                if parent_value is not None:
                    parent = names_by_index.get(int(parent_value))
                info_data = _attribute(link_info, "IFLA_INFO_DATA")
                vlan_id_value = _attribute(info_data, "IFLA_VLAN_ID")
                if vlan_id_value is not None:
                    vlan_id = int(vlan_id_value)
            if kind == "vrf":
                info_data = _attribute(link_info, "IFLA_INFO_DATA")
                vrf_table_value = _attribute(info_data, "IFLA_VRF_TABLE")
                if vrf_table_value is not None:
                    vrf_table = int(vrf_table_value)
            if kind == "bond":
                info_data = _attribute(link_info, "IFLA_INFO_DATA")
                mode_value = _attribute(info_data, "IFLA_BOND_MODE")
                if mode_value is not None:
                    raw_mode = int(mode_value)
                    bond_mode = _BOND_MODE_FROM_NETLINK.get(raw_mode, f"unknown:{raw_mode}")
                miimon_value = _attribute(info_data, "IFLA_BOND_MIIMON")
                if miimon_value is not None:
                    bond_miimon_ms = int(miimon_value)
                if bond_mode == "active-backup":
                    primary_value = _attribute(info_data, "IFLA_BOND_PRIMARY")
                    if primary_value is not None and int(primary_value) != 0:
                        bond_primary = names_by_index.get(int(primary_value))
                if bond_mode == "802.3ad":
                    lacp_rate_value = _attribute(info_data, "IFLA_BOND_AD_LACP_RATE")
                    if lacp_rate_value is not None:
                        bond_lacp_rate = _BOND_LACP_RATE_FROM_NETLINK.get(
                            int(lacp_rate_value), f"unknown:{int(lacp_rate_value)}"
                        )
                    xmit_hash_value = _attribute(info_data, "IFLA_BOND_XMIT_HASH_POLICY")
                    if xmit_hash_value is not None:
                        bond_xmit_hash_policy = _BOND_XMIT_HASH_POLICY_FROM_NETLINK.get(
                            int(xmit_hash_value), f"unknown:{int(xmit_hash_value)}"
                        )
                    min_links_value = _attribute(info_data, "IFLA_BOND_MIN_LINKS")
                    if min_links_value is not None:
                        bond_min_links = int(min_links_value)
            if kind == "vxlan":
                info_data = _attribute(link_info, "IFLA_INFO_DATA")
                vni_value = _attribute(info_data, "IFLA_VXLAN_ID")
                link_value = _attribute(info_data, "IFLA_VXLAN_LINK")
                local_value = _attribute(info_data, "IFLA_VXLAN_LOCAL")
                if local_value is None:
                    local_value = _attribute(info_data, "IFLA_VXLAN_LOCAL6")
                remote_value = _attribute(info_data, "IFLA_VXLAN_GROUP")
                if remote_value is None:
                    remote_value = _attribute(info_data, "IFLA_VXLAN_GROUP6")
                port_value = _attribute(info_data, "IFLA_VXLAN_PORT")
                learning_value = _attribute(info_data, "IFLA_VXLAN_LEARNING")
                if vni_value is not None:
                    vxlan_vni = int(vni_value)
                if link_value is not None:
                    vxlan_link = names_by_index.get(int(link_value))
                if local_value is not None:
                    vxlan_local = ip_address(local_value)
                if remote_value is not None:
                    vxlan_remote = ip_address(remote_value)
                if port_value is not None:
                    vxlan_dst_port = int(port_value)
                if learning_value is not None:
                    vxlan_learning = bool(int(learning_value))
            if kind == "geneve":
                info_data = _attribute(link_info, "IFLA_INFO_DATA")
                vni_value = _attribute(info_data, "IFLA_GENEVE_ID")
                link_value = _attribute(info_data, "IFLA_GENEVE_LINK")
                if link_value is None:
                    link_value = _attribute(message, "IFLA_LINK")
                remote_value = _attribute(info_data, "IFLA_GENEVE_REMOTE")
                if remote_value is None:
                    remote_value = _attribute(info_data, "IFLA_GENEVE_REMOTE6")
                port_value = _attribute(info_data, "IFLA_GENEVE_PORT")
                if vni_value is not None:
                    geneve_vni = int(vni_value)
                if link_value is not None:
                    geneve_link = names_by_index.get(int(link_value))
                if remote_value is not None:
                    geneve_remote = ip_address(remote_value)
                if port_value is not None:
                    geneve_dst_port = int(port_value)
            if kind == "gre":
                info_data = _attribute(link_info, "IFLA_INFO_DATA")
                link_value = _attribute(info_data, "IFLA_GRE_LINK")
                local_value = _attribute(info_data, "IFLA_GRE_LOCAL")
                remote_value = _attribute(info_data, "IFLA_GRE_REMOTE")
                ttl_value = _attribute(info_data, "IFLA_GRE_TTL")
                input_flags = _attribute(info_data, "IFLA_GRE_IFLAGS")
                output_flags = _attribute(info_data, "IFLA_GRE_OFLAGS")
                input_key = _attribute(info_data, "IFLA_GRE_IKEY")
                output_key = _attribute(info_data, "IFLA_GRE_OKEY")
                if link_value is not None:
                    gre_link = names_by_index.get(int(link_value))
                if local_value is not None:
                    gre_local = IPv4Address(local_value)
                if remote_value is not None:
                    gre_remote = IPv4Address(remote_value)
                if ttl_value is not None:
                    gre_ttl = int(ttl_value)
                key_enabled = any(
                    value is not None and bool(int(value) & _GRE_KEY_NETLINK_FLAG)
                    for value in (input_flags, output_flags)
                )
                if key_enabled:
                    key_values = [
                        int(value) for value in (input_key, output_key) if value is not None
                    ]
                    if key_values and len(set(key_values)) == 1:
                        gre_key = key_values[0]
            if kind == "ipip":
                info_data = _attribute(link_info, "IFLA_INFO_DATA")
                link_value = _attribute(info_data, "IFLA_IPIP_LINK")
                local_value = _attribute(info_data, "IFLA_IPIP_LOCAL")
                remote_value = _attribute(info_data, "IFLA_IPIP_REMOTE")
                ttl_value = _attribute(info_data, "IFLA_IPIP_TTL")
                if link_value is not None:
                    ipip_link = names_by_index.get(int(link_value))
                if local_value is not None:
                    ipip_local = IPv4Address(local_value)
                if remote_value is not None:
                    ipip_remote = IPv4Address(remote_value)
                if ttl_value is not None:
                    ipip_ttl = int(ttl_value)
            if kind == "macvlan":
                parent_value = _attribute(message, "IFLA_LINK")
                if parent_value is not None:
                    parent = names_by_index.get(int(parent_value))
                info_data = _attribute(link_info, "IFLA_INFO_DATA")
                mode_value = _attribute(info_data, "IFLA_MACVLAN_MODE")
                if mode_value is not None:
                    if isinstance(mode_value, str):
                        macvlan_mode = mode_value
                    else:
                        raw_mode = int(mode_value)
                        macvlan_mode = _MACVLAN_MODE_FROM_NETLINK.get(
                            raw_mode, f"unknown:{raw_mode}"
                        )
            if kind == "ipvlan":
                parent_value = _attribute(message, "IFLA_LINK")
                if parent_value is not None:
                    parent = names_by_index.get(int(parent_value))
                info_data = _attribute(link_info, "IFLA_INFO_DATA")
                mode_value = _attribute(info_data, "IFLA_IPVLAN_MODE")
                if mode_value is not None:
                    if isinstance(mode_value, str):
                        ipvlan_mode = mode_value
                    else:
                        raw_mode = int(mode_value)
                        ipvlan_mode = _IPVLAN_MODE_FROM_NETLINK.get(raw_mode, f"unknown:{raw_mode}")
            slave_kind = _attribute(link_info, "IFLA_INFO_SLAVE_KIND")
            if slave_kind == "bridge":
                slave_data = _attribute(link_info, "IFLA_INFO_SLAVE_DATA")
                cost_value = _attribute(slave_data, "IFLA_BRPORT_COST")
                port_priority_value = _attribute(slave_data, "IFLA_BRPORT_PRIORITY")
                hairpin_value = _attribute(slave_data, "IFLA_BRPORT_MODE")
                isolated_value = _attribute(slave_data, "IFLA_BRPORT_ISOLATED")
                learning_value = _attribute(slave_data, "IFLA_BRPORT_LEARNING")
                flood_value = _attribute(slave_data, "IFLA_BRPORT_UNICAST_FLOOD")
                multicast_flood_value = _attribute(slave_data, "IFLA_BRPORT_MCAST_FLOOD")
                if cost_value is not None:
                    path_cost = int(cost_value)
                if port_priority_value is not None:
                    port_priority = int(port_priority_value)
                if hairpin_value is not None:
                    hairpin = bool(int(hairpin_value))
                if isolated_value is not None:
                    isolated = bool(int(isolated_value))
                if learning_value is not None:
                    learning = bool(int(learning_value))
                if flood_value is not None:
                    flood = bool(int(flood_value))
                if multicast_flood_value is not None:
                    multicast_flood = bool(int(multicast_flood_value))
            mtu_value = _attribute(message, "IFLA_MTU")
            mac_value = _attribute(message, "IFLA_ADDRESS")
            link_id_value = _attribute(message, "IFLA_IFALIAS")
            interfaces[name] = InterfaceInventory(
                name=name,
                kind=kind,
                ifindex=index,
                master=master,
                mtu=int(mtu_value) if mtu_value is not None else 0,
                up=bool(int(_value(message, "flags", 0)) & _IFF_UP),
                addresses=tuple(addresses_by_index.get(index, ())),
                mac=None if mac_value is None else str(mac_value).lower(),
                stp=stp,
                vlan_filtering=vlan_filtering,
                bridge_priority=bridge_priority,
                path_cost=path_cost,
                port_priority=port_priority,
                hairpin=hairpin,
                isolated=isolated,
                learning=learning,
                flood=flood,
                multicast_flood=multicast_flood,
                bridge_vlans=(
                    tuple(sorted(vlans_by_index.get(index, ()), key=lambda vlan: vlan.vid))
                    if slave_kind == "bridge"
                    else ()
                ),
                netem=netem_by_index.get(index),
                qdisc=qdisc_by_index.get(index),
                link_id=(str(link_id_value) if link_id_value is not None else None),
                parent=parent,
                vlan_id=vlan_id,
                vrf_table=vrf_table,
                bond_mode=bond_mode,
                bond_miimon_ms=bond_miimon_ms,
                bond_primary=bond_primary,
                bond_lacp_rate=bond_lacp_rate,
                bond_xmit_hash_policy=bond_xmit_hash_policy,
                bond_min_links=bond_min_links,
                vxlan_vni=vxlan_vni,
                vxlan_link=vxlan_link,
                vxlan_local=vxlan_local,
                vxlan_remote=vxlan_remote,
                vxlan_dst_port=vxlan_dst_port,
                vxlan_learning=vxlan_learning,
                geneve_vni=geneve_vni,
                geneve_link=geneve_link,
                geneve_remote=geneve_remote,
                geneve_dst_port=geneve_dst_port,
                gre_link=gre_link,
                gre_local=gre_local,
                gre_remote=gre_remote,
                gre_key=gre_key,
                gre_ttl=gre_ttl,
                ipip_link=ipip_link,
                ipip_local=ipip_local,
                ipip_remote=ipip_remote,
                ipip_ttl=ipip_ttl,
                macvlan_mode=macvlan_mode,
                ipvlan_mode=ipvlan_mode,
            )
        return interfaces, names_by_index

    @staticmethod
    def _inventory_neighbors(
        messages: Sequence[Any],
        names_by_index: Mapping[int, str],
        declared: Sequence[NeighborPlan],
        namespace: str,
    ) -> tuple[NeighborPlan, ...]:
        declared_by_identity = {
            (neighbor.dst, neighbor.dev, neighbor.proxy): neighbor for neighbor in declared
        }
        observed_by_identity: dict[tuple[object, str, bool], NeighborPlan] = {}

        for message in messages:
            try:
                family = int(_value(message, "family", 0))
                index = int(_value(message, "ifindex"))
                flags = int(_value(message, "flags", 0))
                state_value = int(_value(message, "state", 0))
                neighbor_type = int(_value(message, "ndm_type", _RTN_UNICAST))
            except (TypeError, ValueError):
                continue
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            interface = names_by_index.get(index)
            destination_value = _attribute(message, "NDA_DST")
            if interface is None or destination_value is None:
                continue
            try:
                destination = ip_address(str(destination_value))
            except ValueError:
                continue
            proxy = bool(flags & _NTF_PROXY)
            identity = (destination, interface, proxy)
            if identity not in declared_by_identity:
                continue
            if identity in observed_by_identity:
                raise _unsupported_inventory_neighbor(namespace, "duplicate")
            if flags != (_NTF_PROXY if proxy else 0):
                raise _unsupported_inventory_neighbor(namespace, "flags")
            if neighbor_type != _RTN_UNICAST:
                raise _unsupported_inventory_neighbor(namespace, "type")
            if not _attribute_names(message) <= _ALLOWED_NEIGHBOR_ATTRIBUTES:
                raise _unsupported_inventory_neighbor(namespace, "attribute")

            link_layer_value = _attribute(message, "NDA_LLADDR")
            if proxy:
                if state_value != _NUD_NONE or link_layer_value is not None:
                    raise _unsupported_inventory_neighbor(namespace, "proxy")
                observed = NeighborPlan(
                    dst=destination,
                    dev=interface,
                    lladdr=None,
                    state=None,
                    proxy=True,
                )
            else:
                state = _NEIGHBOR_STATE_FROM_NETLINK.get(state_value)
                if state is None:
                    raise _unsupported_inventory_neighbor(namespace, "state")
                observed = NeighborPlan(
                    dst=destination,
                    dev=interface,
                    lladdr=(None if link_layer_value is None else str(link_layer_value).lower()),
                    state=state,
                    proxy=False,
                )
            observed_by_identity[identity] = observed

        return tuple(
            observed_by_identity[identity]
            for neighbor in declared
            if (identity := (neighbor.dst, neighbor.dev, neighbor.proxy)) in observed_by_identity
        )

    @staticmethod
    def _inventory_connected_routes(
        interfaces: dict[str, InterfaceInventory],
    ) -> tuple[RoutePlan, ...]:
        def route_table(interface: InterfaceInventory) -> int:
            if interface.master is None:
                return _RT_TABLE_MAIN
            master = interfaces.get(interface.master)
            if master is None or master.kind != "vrf" or master.vrf_table is None:
                return _RT_TABLE_MAIN
            return master.vrf_table

        ordered_interfaces = sorted(
            interfaces.values(),
            key=lambda interface: (
                interface.ifindex is None,
                interface.ifindex if interface.ifindex is not None else 0,
                interface.name,
            ),
        )
        routes = (
            RoutePlan(
                dst=address.network,
                via=None,
                dev=interface.name,
                table=route_table(interface),
            )
            for interface in ordered_interfaces
            for address in sorted(
                interface.addresses,
                key=lambda address: (int(address.ip), address.network.prefixlen),
            )
        )
        return tuple(dict.fromkeys(routes))

    @staticmethod
    def _inventory_routes(
        route_messages: Sequence[Any],
        names_by_index: dict[int, str],
        interfaces: dict[str, InterfaceInventory],
        namespace: str,
        *,
        family: int = socket.AF_INET,
        expected_table: int = _RT_TABLE_MAIN,
        allow_dynamic: bool = False,
    ) -> tuple[RoutePlan, ...]:
        routes: list[RoutePlan] = []
        for message in route_messages:
            try:
                route_protocol = int(_value(message, "proto", 0))
            except (TypeError, ValueError):
                route_protocol = 0
            dynamic_route = allow_dynamic and route_protocol in _FRR_ROUTE_PROTOCOLS
            table_value = _attribute(message, "RTA_TABLE")
            if table_value is None:
                table_value = _value(message, "table", _RT_TABLE_MAIN)
            try:
                table = int(table_value)
            except (TypeError, ValueError):
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, "invalid_table") from None
            if table != expected_table:
                continue

            try:
                route_flags = int(_value(message, "flags", 0))
            except (TypeError, ValueError):
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, "route_flags") from None
            if route_flags & _RTM_F_CLONED:
                continue

            try:
                route_type = int(_value(message, "type", _RTN_UNICAST))
            except (TypeError, ValueError):
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, "route_type") from None
            if (
                expected_table != _RT_TABLE_MAIN
                and route_protocol == _RTPROT_KERNEL
                and route_type in _VRF_KERNEL_LOCAL_ROUTE_TYPES
            ):
                continue
            if route_type != _RTN_UNICAST:
                if dynamic_route:
                    continue
                reason = _UNSUPPORTED_ROUTE_TYPES.get(route_type, "route_type")
                raise _unsupported_inventory_route(namespace, reason)

            attribute_names = _attribute_names(message)
            semantic_attribute_names = attribute_names
            if family == socket.AF_INET6:
                if "RTA_PRIORITY" in attribute_names:
                    if dynamic_route:
                        semantic_attribute_names = semantic_attribute_names.difference(
                            {"RTA_PRIORITY"}
                        )
                    else:
                        try:
                            metric_value = _attribute(message, "RTA_PRIORITY")
                            if metric_value is None:
                                raise ValueError
                            metric = int(metric_value)
                            expected_metric = 256 if route_protocol == _RTPROT_KERNEL else 1024
                        except (TypeError, ValueError):
                            raise _unsupported_inventory_route(namespace, "priority") from None
                        if metric != expected_metric:
                            raise _unsupported_inventory_route(namespace, "priority")
                if "RTA_PREF" in attribute_names:
                    if dynamic_route:
                        semantic_attribute_names = semantic_attribute_names.difference({"RTA_PREF"})
                    else:
                        try:
                            preference_value = _attribute(message, "RTA_PREF")
                            if preference_value is None:
                                raise ValueError
                            preference = int(preference_value)
                        except (TypeError, ValueError):
                            raise _unsupported_inventory_route(namespace, "preference") from None
                        if preference != 0:
                            raise _unsupported_inventory_route(namespace, "preference")
                semantic_attribute_names = semantic_attribute_names.difference(
                    {"RTA_PRIORITY", "RTA_PREF"}
                )
            elif "RTA_PRIORITY" in attribute_names:
                if dynamic_route:
                    semantic_attribute_names = semantic_attribute_names.difference({"RTA_PRIORITY"})
                else:
                    raise _unsupported_inventory_route(namespace, "priority")

            if "RTA_MP_ALGO" in semantic_attribute_names:
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, "multipath_algorithm")
            if "RTA_NH_ID" in semantic_attribute_names:
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, "nexthop_id")
            try:
                source_prefixlen = int(_value(message, "src_len", 0))
            except (TypeError, ValueError):
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, "source_specific") from None
            if source_prefixlen != 0 or "RTA_SRC" in semantic_attribute_names:
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, "source_specific")
            unsupported_reason = next(
                (
                    reason
                    for attribute, reason in _UNSUPPORTED_ROUTE_ATTRIBUTES
                    if attribute in semantic_attribute_names
                ),
                None,
            )
            if unsupported_reason is not None:
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, unsupported_reason)
            try:
                tos = int(_value(message, "tos", 0))
            except (TypeError, ValueError):
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, "tos") from None
            if tos != 0:
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, "tos")
            if route_flags != 0:
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, "route_flags")
            if not semantic_attribute_names <= _ALLOWED_ROUTE_ATTRIBUTES:
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, "unsupported_attribute")

            has_preferred_source = "RTA_PREFSRC" in semantic_attribute_names
            if has_preferred_source:
                try:
                    protocol = int(_value(message, "proto", 0))
                    scope = int(_value(message, "scope", 0))
                except (TypeError, ValueError):
                    if dynamic_route:
                        continue
                    raise _unsupported_inventory_route(
                        namespace,
                        "preferred_source",
                    ) from None
                if (
                    protocol != _RTPROT_KERNEL
                    or scope != _RT_SCOPE_LINK
                    or "RTA_GATEWAY" in semantic_attribute_names
                ):
                    if dynamic_route:
                        continue
                    raise _unsupported_inventory_route(namespace, "preferred_source")

            if "RTA_MULTIPATH" in semantic_attribute_names:
                if (
                    has_preferred_source
                    or "RTA_OIF" in semantic_attribute_names
                    or "RTA_GATEWAY" in semantic_attribute_names
                ):
                    if dynamic_route:
                        continue
                    raise _unsupported_inventory_route(namespace, "multipath")
                try:
                    nexthops = _decode_multipath_nexthops(
                        _attribute(message, "RTA_MULTIPATH"),
                        names_by_index,
                        namespace,
                        family,
                    )
                    prefixlen = int(_value(message, "dst_len", 0))
                except NslabError:
                    if dynamic_route:
                        continue
                    raise
                except (TypeError, ValueError):
                    if dynamic_route:
                        continue
                    raise _unsupported_inventory_route(namespace, "invalid_destination") from None
                destination = _attribute(message, "RTA_DST")
                if destination is None:
                    if prefixlen != 0:
                        if dynamic_route:
                            continue
                        raise _unsupported_inventory_route(namespace, "missing_destination")
                    destination = "::" if family == socket.AF_INET6 else "0.0.0.0"
                try:
                    network = (
                        IPv6Network(f"{destination}/{prefixlen}", strict=False)
                        if family == socket.AF_INET6
                        else IPv4Network(f"{destination}/{prefixlen}", strict=False)
                    )
                except (TypeError, ValueError):
                    if dynamic_route:
                        continue
                    raise _unsupported_inventory_route(namespace, "invalid_destination") from None
                route = RoutePlan(
                    dst=network,
                    via=None,
                    dev=None,
                    table=table,
                    nexthops=nexthops,
                )
                if route not in routes:
                    routes.append(route)
                continue

            output_value = _attribute(message, "RTA_OIF")
            if output_value is None:
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, "missing_oif")
            try:
                output_index = int(output_value)
            except (TypeError, ValueError):
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, "invalid_oif") from None
            interface = names_by_index.get(output_index)
            if interface is None:
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, "unknown_ifindex")
            try:
                prefixlen = int(_value(message, "dst_len", 0))
            except (TypeError, ValueError):
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, "invalid_destination") from None
            destination = _attribute(message, "RTA_DST")
            if destination is None:
                if prefixlen != 0:
                    if dynamic_route:
                        continue
                    raise _unsupported_inventory_route(namespace, "missing_destination")
                destination = "::" if family == socket.AF_INET6 else "0.0.0.0"
            gateway = _attribute(message, "RTA_GATEWAY")
            try:
                if family == socket.AF_INET6:
                    route = RoutePlan(
                        dst=IPv6Network(f"{destination}/{prefixlen}", strict=False),
                        via=IPv6Address(str(gateway)) if gateway is not None else None,
                        dev=interface,
                        table=table,
                    )
                else:
                    route = RoutePlan(
                        dst=IPv4Network(f"{destination}/{prefixlen}", strict=False),
                        via=IPv4Address(str(gateway)) if gateway is not None else None,
                        dev=interface,
                        table=table,
                    )
            except (TypeError, ValueError):
                if dynamic_route:
                    continue
                reason = "invalid_gateway" if gateway is not None else "invalid_destination"
                raise _unsupported_inventory_route(namespace, reason) from None
            if has_preferred_source:
                preferred_source = _attribute(message, "RTA_PREFSRC")
                try:
                    preferred_address = (
                        IPv6Address(str(preferred_source))
                        if family == socket.AF_INET6
                        else IPv4Address(str(preferred_source))
                    )
                except (TypeError, ValueError):
                    if dynamic_route:
                        continue
                    raise _unsupported_inventory_route(
                        namespace,
                        "preferred_source",
                    ) from None
                observed_interface = interfaces.get(interface)
                if observed_interface is None or not any(
                    address.ip == preferred_address and address.network == route.dst
                    for address in observed_interface.addresses
                ):
                    if dynamic_route:
                        continue
                    raise _unsupported_inventory_route(namespace, "preferred_source")
            if (
                family == socket.AF_INET6
                and int(_value(message, "proto", 0)) == _RTPROT_KERNEL
                and route.via is None
                and (route.dst.is_link_local or route.dst.is_multicast)
            ):
                continue
            if route not in routes:
                routes.append(route)
        return tuple(routes)

    @staticmethod
    def _inventory_rules(
        rule_messages: Sequence[Any],
        namespace: str,
        *,
        family: int = socket.AF_INET,
        ignore_l3mdev_kernel_rule: bool = False,
    ) -> tuple[PolicyRulePlan, ...]:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise _unsupported_inventory_rule(namespace, "address_family")

        def integer_attribute(message: Any, name: str) -> int | None:
            value = _attribute(message, name)
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                raise _unsupported_inventory_rule(namespace, name.lower()) from None

        def range_attribute(
            message: Any,
            name: str,
            *,
            maximum: int,
        ) -> tuple[int, int] | None:
            value = _attribute(message, name)
            if value is None:
                return None
            parts = str(value).replace("-", ":").split(":")
            if len(parts) != 2:
                raise _unsupported_inventory_rule(namespace, name.lower())
            try:
                start, end = (int(part) for part in parts)
            except ValueError:
                raise _unsupported_inventory_rule(namespace, name.lower()) from None
            if start < 0 or end < start or end > maximum:
                raise _unsupported_inventory_rule(namespace, name.lower())
            return start, end

        def network_selector(
            message: Any,
            name: str,
            prefixlen: int,
        ) -> IPv4Network | IPv6Network | None:
            maximum = 32 if family == socket.AF_INET else 128
            if prefixlen < 0 or prefixlen > maximum:
                raise _unsupported_inventory_rule(namespace, f"{name.lower()}_prefixlen")
            value = _attribute(message, name)
            if value is None:
                if prefixlen != 0:
                    raise _unsupported_inventory_rule(namespace, f"missing_{name.lower()}")
                return None
            if prefixlen == 0:
                return None
            try:
                if family == socket.AF_INET:
                    return IPv4Network(f"{value}/{prefixlen}", strict=False)
                return IPv6Network(f"{value}/{prefixlen}", strict=False)
            except (TypeError, ValueError):
                raise _unsupported_inventory_rule(namespace, name.lower()) from None

        rules: list[PolicyRulePlan] = []
        for message in rule_messages:
            try:
                table_value = _attribute(message, "FRA_TABLE")
                if table_value is None:
                    table_value = _value(message, "table", 0)
                raw_table = int(table_value)
                priority_value = _attribute(message, "FRA_PRIORITY")
                priority = 0 if priority_value is None else int(priority_value)
                protocol_value = _attribute(message, "FRA_PROTOCOL")
                protocol = 0 if protocol_value is None else int(protocol_value)
                action_value = int(_value(message, "action", _FR_ACT_TO_TBL))
                flags = int(_value(message, "flags", 0))
                tos = int(_value(message, "tos", 0))
                source_prefixlen = int(_value(message, "src_len", 0))
                destination_prefixlen = int(_value(message, "dst_len", 0))
            except (TypeError, ValueError):
                raise _unsupported_inventory_rule(namespace, "invalid_identity") from None

            l3mdev_value = integer_attribute(message, "FRA_L3MDEV")
            l3mdev = l3mdev_value == 1
            if l3mdev_value not in {None, 0, 1}:
                raise _unsupported_inventory_rule(namespace, "l3mdev")
            attribute_names = _attribute_names(message)
            default_attribute_names = {
                "FRA_TABLE",
                "FRA_SUPPRESS_IFGROUP",
                "FRA_SUPPRESS_PREFIXLEN",
                "FRA_PROTOCOL",
                "FRA_PRIORITY",
                "FRA_PAD",
            }
            suppress_prefix_value = integer_attribute(message, "FRA_SUPPRESS_PREFIXLEN")
            suppress_ifgroup_value = integer_attribute(message, "FRA_SUPPRESS_IFGROUP")
            if (
                ignore_l3mdev_kernel_rule
                and l3mdev
                and priority == 1000
                and raw_table == 0
                and protocol in {0, _RTPROT_KERNEL}
                and action_value == _FR_ACT_TO_TBL
                and flags == 0
                and tos == 0
                and source_prefixlen == 0
                and destination_prefixlen == 0
                and attribute_names <= default_attribute_names | {"FRA_L3MDEV"}
                and suppress_prefix_value in {None, 4_294_967_295}
                and suppress_ifgroup_value in {None, 4_294_967_295}
            ):
                continue
            if (
                (priority, raw_table) in {(0, 255), (32766, 254), (32767, 253)}
                and protocol in {0, _RTPROT_KERNEL}
                and action_value == _FR_ACT_TO_TBL
                and flags == 0
                and tos == 0
                and source_prefixlen == 0
                and destination_prefixlen == 0
                and not l3mdev
                and attribute_names <= default_attribute_names
                and suppress_prefix_value in {None, 4_294_967_295}
                and suppress_ifgroup_value in {None, 4_294_967_295}
            ):
                continue

            if not attribute_names <= _ALLOWED_RULE_ATTRIBUTES:
                raise _unsupported_inventory_rule(namespace, "unsupported_attribute")
            if priority < 1 or priority > 4_294_967_295:
                raise _unsupported_inventory_rule(namespace, "priority")
            if protocol < 0 or protocol > 255:
                raise _unsupported_inventory_rule(namespace, "protocol")
            action = _RULE_ACTION_FROM_NETLINK.get(action_value)
            if action is None:
                raise _unsupported_inventory_rule(namespace, "action")
            if flags & ~_FIB_RULE_INVERT:
                raise _unsupported_inventory_rule(namespace, "flags")
            if action != "goto" and "FRA_GOTO" in attribute_names:
                raise _unsupported_inventory_rule(namespace, "unexpected_goto")
            if tos < 0 or tos > 255:
                raise _unsupported_inventory_rule(namespace, "tos")

            suppress_prefix_length = integer_attribute(message, "FRA_SUPPRESS_PREFIXLEN")
            if suppress_prefix_length == 4_294_967_295:
                suppress_prefix_length = None
            maximum_prefixlen = 32 if family == socket.AF_INET else 128
            if suppress_prefix_length is not None and not (
                0 <= suppress_prefix_length <= maximum_prefixlen
            ):
                raise _unsupported_inventory_rule(namespace, "suppress_prefixlen")
            suppress_interface_group = integer_attribute(message, "FRA_SUPPRESS_IFGROUP")
            if suppress_interface_group == 4_294_967_295:
                suppress_interface_group = None

            if action == "lookup":
                if l3mdev:
                    if raw_table != 0:
                        raise _unsupported_inventory_rule(namespace, "l3mdev_table")
                    table: int | None = None
                else:
                    if raw_table < 1 or raw_table > 4_294_967_295:
                        raise _unsupported_inventory_rule(namespace, "table")
                    table = raw_table
                goto = None
            elif action == "goto":
                if raw_table != 0:
                    raise _unsupported_inventory_rule(namespace, "goto_table")
                table = None
                goto = integer_attribute(message, "FRA_GOTO")
                if goto is None or goto < 1 or goto > 4_294_967_295:
                    raise _unsupported_inventory_rule(namespace, "goto")
            else:
                if raw_table != 0:
                    raise _unsupported_inventory_rule(namespace, "action_table")
                table = None
                goto = None
            if action != "lookup" and (
                suppress_prefix_length is not None or suppress_interface_group is not None
            ):
                raise _unsupported_inventory_rule(namespace, "suppress_action")

            fwmark = integer_attribute(message, "FRA_FWMARK")
            fwmask = integer_attribute(message, "FRA_FWMASK")
            if fwmask is not None and fwmark is None:
                raise _unsupported_inventory_rule(namespace, "fwmask")
            ip_protocol = integer_attribute(message, "FRA_IP_PROTO")
            if ip_protocol is not None and not 0 <= ip_protocol <= 255:
                raise _unsupported_inventory_rule(namespace, "ip_protocol")
            source_port = range_attribute(message, "FRA_SPORT_RANGE", maximum=65535)
            destination_port = range_attribute(message, "FRA_DPORT_RANGE", maximum=65535)
            if (source_port is not None or destination_port is not None) and ip_protocol is None:
                raise _unsupported_inventory_rule(namespace, "port_protocol")
            uid_range = range_attribute(message, "FRA_UID_RANGE", maximum=4_294_967_295)

            realms_value = integer_attribute(message, "FRA_FLOW")
            if realms_value is None or realms_value == 0:
                realms = None
            else:
                realms = ((realms_value >> 16) & 65535, realms_value & 65535)
            tunnel_id = integer_attribute(message, "FRA_TUN_ID")
            if tunnel_id is not None and not 0 <= tunnel_id <= 18_446_744_073_709_551_615:
                raise _unsupported_inventory_rule(namespace, "tunnel_id")

            rule = PolicyRulePlan(
                priority=priority,
                family=4 if family == socket.AF_INET else 6,
                action=action,
                table=table,
                goto=goto,
                source=network_selector(message, "FRA_SRC", source_prefixlen),
                destination=network_selector(message, "FRA_DST", destination_prefixlen),
                invert=bool(flags & _FIB_RULE_INVERT),
                tos=None if tos == 0 else tos,
                fwmark=fwmark,
                fwmask=fwmask,
                iif=(
                    None
                    if _attribute(message, "FRA_IIFNAME") is None
                    else str(_attribute(message, "FRA_IIFNAME"))
                ),
                oif=(
                    None
                    if _attribute(message, "FRA_OIFNAME") is None
                    else str(_attribute(message, "FRA_OIFNAME"))
                ),
                l3mdev=l3mdev,
                uid_range=uid_range,
                protocol=protocol,
                ip_protocol=ip_protocol,
                source_port=source_port,
                destination_port=destination_port,
                tunnel_id=tunnel_id,
                suppress_prefix_length=suppress_prefix_length,
                suppress_interface_group=suppress_interface_group,
                realms=realms,
            )
            if rule not in rules:
                rules.append(rule)
        return tuple(rules)

    def _read_sysctls(self, node: NodePlan) -> dict[str, int]:
        if not node.sysctls:
            return {}
        self._pushns(node.namespace)
        with _cleanup_context(self._popns, "namespace pop"):
            return {
                key: int(self._sysctl_root.joinpath(*key.split(".")).read_text(encoding="ascii"))
                for key in node.sysctls
            }

    def execute(
        self,
        namespace: str,
        argv: Sequence[str],
        *,
        capture_output: bool = True,
    ) -> ExecResult:
        immutable_argv = tuple(argv)
        popen_options: dict[str, Any] = {"text": True, "shell": False}
        if capture_output:
            popen_options.update(stdout=PIPE, stderr=PIPE)
        with _spawn_nspopen(
            self._nspopen_factory,
            namespace,
            [
                "/usr/bin/timeout",
                "--foreground",
                "--kill-after=1s",
                "--",
                "0",
                *immutable_argv,
            ],
            popen_options,
            inherit_stdin=not capture_output,
        ) as (process, restore_signal_mask, block_release_signals):
            restore_signal_mask()
            stdout, stderr = process.communicate()
            returncode = int(process.returncode)
            block_release_signals()
        return ExecResult(
            argv=immutable_argv,
            returncode=returncode,
            stdout="" if stdout is None else str(stdout),
            stderr="" if stderr is None else str(stderr),
        )
