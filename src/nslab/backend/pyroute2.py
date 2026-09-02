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
from pyroute2.netlink.exceptions import NetlinkError
from pyroute2.netlink.rtnl.tcmsg.common import percent2u32, time2tick

from nslab.backend.base import (
    ExecResult,
    InterfaceInventory,
    LiveInventory,
    NamespaceInventory,
)
from nslab.errors import NslabError, OperationCancelled
from nslab.planner import (
    BridgeVlanPlan,
    EndpointPlan,
    IPInterface,
    LinkPlan,
    NetemPlan,
    NodePlan,
    RoutePlan,
    TopologyPlan,
)
from nslab.routing import FrrRuntime

_BRIDGE_VLAN_INFO_PVID = 2
_BRIDGE_VLAN_INFO_UNTAGGED = 4
_VETH_VISIBILITY_TIMEOUT = 5.0
_VETH_VISIBILITY_INTERVAL = 0.05
_PYROUTE2_CONFIG_LOCK = threading.Lock()

_IFF_UP = 1
_ARPHRD_LOOPBACK = 772
_RT_TABLE_MAIN = 254
_RTN_UNICAST = 1
_RTPROT_KERNEL = 2
_RT_SCOPE_LINK = 253
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
_ALLOWED_ROUTE_ATTRIBUTES = frozenset(
    {
        "RTA_TABLE",
        "RTA_DST",
        "RTA_OIF",
        "RTA_GATEWAY",
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
                resource,
                renamed_endpoints,
                ownership_token,
            )
            self._configure_veth_endpoint(
                link.right,
                link.mtu,
                link.netem,
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
                        namespace.tc(
                            "add",
                            "netem",
                            index,
                            "1:",
                            delay=netem.delay_ms * 1000,
                            jitter=netem.jitter_ms * 1000,
                            loss=netem.loss_percent,
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
                    if bridge_name not in node.interfaces:
                        namespace.link("set", index=bridge_index, state="up")

                for interface, addresses in node.interfaces.items():
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
                    route_index = indexes.get(route.dev)
                    if route_index is None:
                        route_index = _required_index(
                            namespace,
                            route.dev,
                            "configure_node",
                            f"{node.namespace}:{route.dev}",
                        )
                    route_arguments: dict[str, object] = {
                        "dst": str(route.dst),
                        "oif": route_index,
                    }
                    if route.via is not None:
                        route_arguments["gateway"] = str(route.via)
                    if route.dst.version == 6:
                        route_arguments["family"] = socket.AF_INET6
                    namespace.route("add", **route_arguments)
        except NetlinkError as error:
            raise _translate_netlink_error(
                error,
                operation="configure_node",
                resource=node.namespace,
            ) from error

        self._write_sysctls(node)

    @staticmethod
    def _node_link_interfaces(node: NodePlan, plan: TopologyPlan) -> tuple[str, ...]:
        interfaces = (
            endpoint.interface
            for link in plan.links
            for endpoint in (link.left, link.right)
            if endpoint.namespace == node.namespace
        )
        return tuple(dict.fromkeys(interfaces))

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
        inspect_netem = any(link.netem is not None for link in plan.links)
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
                        namespace,
                        inspect_netem=inspect_netem,
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
        namespace: Any,
        *,
        inspect_netem: bool,
    ) -> NamespaceInventory:
        with _managed_handle(namespace) as handle:
            link_messages = tuple(handle.get_links())
            families = self._inventory_families(node)
            address_messages = tuple(
                message for family in families for message in handle.get_addr(family=family)
            )
            vlan_messages = (
                tuple(handle.get_vlans()) if node.kind == "bridge" and node.vlan_filtering else ()
            )
            qdisc_messages = tuple(handle.get_qdiscs()) if inspect_netem else ()
            route_messages = tuple(
                (
                    family,
                    tuple(
                        handle.get_routes(
                            family=family,
                            table=_RT_TABLE_MAIN,
                        )
                    ),
                )
                for family in families
            )
        interfaces, names_by_index = self._inventory_interfaces(
            link_messages,
            address_messages,
            vlan_messages,
            qdisc_messages,
            namespace=node.namespace,
            declared_addresses=node.interfaces,
        )
        observed_routes = tuple(
            route
            for family, messages in route_messages
            for route in self._inventory_routes(
                messages,
                names_by_index,
                interfaces,
                node.namespace,
                family=family,
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
        sysctls = self._read_sysctls(node)
        return NamespaceInventory(
            node=node.name,
            kind=node.kind,
            namespace=node.namespace,
            exists=True,
            interfaces=interfaces,
            routes=routes,
            sysctls=sysctls,
        )

    @staticmethod
    def _inventory_families(node: NodePlan) -> tuple[int, ...]:
        has_ipv6 = (
            any(
                address.version == 6
                for addresses in node.interfaces.values()
                for address in addresses
            )
            or any(route.dst.version == 6 for route in node.routes)
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
        for message in qdisc_messages:
            if _attribute(message, "TCA_KIND") != "netem":
                continue
            index = int(_value(message, "index"))
            if index in netem_by_index:
                raise _unsupported_inventory_qdisc(namespace, "multiple_netem_qdiscs")
            options = _attribute(message, "TCA_OPTIONS")
            if options is None:
                raise _unsupported_inventory_qdisc(namespace, "missing_options")
            try:
                limit = int(_value(options, "limit", 0))
                gap = int(_value(options, "gap", 0))
                duplicate = int(_value(options, "duplicate", 0))
                raw_delay = int(_value(options, "delay", 0))
                raw_jitter = int(_value(options, "jitter", 0))
                raw_loss = int(_value(options, "loss", 0))
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

            nested_options = (
                ("TCA_NETEM_CORR", ("delay_corr", "loss_corr", "dup_corr")),
                ("TCA_NETEM_REORDER", ("prob_reorder", "corr_reorder")),
                ("TCA_NETEM_CORRUPT", ("prob_corrupt", "corr_corrupt")),
                (
                    "TCA_NETEM_RATE",
                    ("rate", "packet_overhead", "cell_size", "cell_overhead"),
                ),
            )
            for attribute_name, field_names in nested_options:
                nested = _attribute(options, attribute_name)
                if nested is not None and any(
                    int(_value(nested, field_name, 0)) != 0 for field_name in field_names
                ):
                    raise _unsupported_inventory_qdisc(namespace, "extended_options")
            netem_by_index[index] = NetemPlan(
                delay_ms=delay_ms,
                jitter_ms=jitter_ms,
                loss_percent=loss_percent,
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
            master_value = _attribute(message, "IFLA_MASTER")
            master = names_by_index.get(int(master_value)) if master_value is not None else None
            stp: bool | None = None
            vlan_filtering: bool | None = None
            bridge_priority: int | None = None
            path_cost: int | None = None
            port_priority: int | None = None
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
            slave_kind = _attribute(link_info, "IFLA_INFO_SLAVE_KIND")
            if slave_kind == "bridge":
                slave_data = _attribute(link_info, "IFLA_INFO_SLAVE_DATA")
                cost_value = _attribute(slave_data, "IFLA_BRPORT_COST")
                port_priority_value = _attribute(slave_data, "IFLA_BRPORT_PRIORITY")
                if cost_value is not None:
                    path_cost = int(cost_value)
                if port_priority_value is not None:
                    port_priority = int(port_priority_value)
            mtu_value = _attribute(message, "IFLA_MTU")
            link_id_value = _attribute(message, "IFLA_IFALIAS")
            interfaces[name] = InterfaceInventory(
                name=name,
                kind=kind,
                ifindex=index,
                master=master,
                mtu=int(mtu_value) if mtu_value is not None else 0,
                up=bool(int(_value(message, "flags", 0)) & _IFF_UP),
                addresses=tuple(addresses_by_index.get(index, ())),
                stp=stp,
                vlan_filtering=vlan_filtering,
                bridge_priority=bridge_priority,
                path_cost=path_cost,
                port_priority=port_priority,
                bridge_vlans=(
                    tuple(sorted(vlans_by_index.get(index, ()), key=lambda vlan: vlan.vid))
                    if slave_kind == "bridge"
                    else ()
                ),
                netem=netem_by_index.get(index),
                link_id=(str(link_id_value) if link_id_value is not None else None),
            )
        return interfaces, names_by_index

    @staticmethod
    def _inventory_connected_routes(
        interfaces: dict[str, InterfaceInventory],
    ) -> tuple[RoutePlan, ...]:
        ordered_interfaces = sorted(
            interfaces.values(),
            key=lambda interface: (
                interface.ifindex is None,
                interface.ifindex if interface.ifindex is not None else 0,
                interface.name,
            ),
        )
        routes = (
            RoutePlan(dst=address.network, via=None, dev=interface.name)
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
            if table != _RT_TABLE_MAIN:
                continue

            try:
                route_type = int(_value(message, "type", _RTN_UNICAST))
            except (TypeError, ValueError):
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, "route_type") from None
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

            if (
                "RTA_MULTIPATH" in semantic_attribute_names
                or "RTA_MP_ALGO" in semantic_attribute_names
            ):
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, "multipath")
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
            try:
                route_flags = int(_value(message, "flags", 0))
            except (TypeError, ValueError):
                if dynamic_route:
                    continue
                raise _unsupported_inventory_route(namespace, "route_flags") from None
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
                    )
                else:
                    route = RoutePlan(
                        dst=IPv4Network(f"{destination}/{prefixlen}", strict=False),
                        via=IPv4Address(str(gateway)) if gateway is not None else None,
                        dev=interface,
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
