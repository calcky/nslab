from __future__ import annotations

import errno
import grp
import json
import os
import pwd
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from ipaddress import IPv4Network
from pathlib import Path
from typing import Any

from nslab.errors import NslabError
from nslab.planner import (
    EndpointPlan,
    NodePlan,
    RoutingPlan,
    TopologyPlan,
    node_interface_addresses,
)

_FRR_DAEMONS = ("zebra", "ospfd", "bgpd", "pimd")
_ROUTING_RUNTIME_VERSION = 1
_DEFAULT_STARTUP_TIMEOUT = 5.0
_DEFAULT_STOP_TIMEOUT = 3.0
_VTY_CONFIG_MARKER = "nslab-managed-pathspace-v1"
_RUNTIME_MARKER = "nslab-managed-runtime-v1"
_RUNTIME_NODE_MARKER = "nslab-managed-runtime-node-v1"
_STATE_MARKER = "nslab-managed-state-pathspace-v1"
_MARKER_FILE = ".nslab-marker"


def _connected_ipv4_networks(node: NodePlan) -> tuple[IPv4Network, ...]:
    networks = {
        address.network
        for addresses in node_interface_addresses(node).values()
        for address in addresses
        if address.version == 4
    }
    return tuple(
        sorted(networks, key=lambda network: (int(network.network_address), network.prefixlen))
    )


def _configured_or_connected(
    configured: Sequence[IPv4Network],
    node: NodePlan,
) -> tuple[IPv4Network, ...]:
    values = tuple(configured) if configured else _connected_ipv4_networks(node)
    return tuple(
        dict.fromkeys(
            sorted(values, key=lambda network: (int(network.network_address), network.prefixlen))
        )
    )


def _routing_hostname(node: NodePlan) -> str:
    return f"nslab-{node.name}"


def _ospf_point_to_point_interfaces(
    node: NodePlan,
    plan: TopologyPlan | None,
) -> tuple[str, ...]:
    """Return OSPF interfaces that connect two routed Linux nodes directly."""

    if plan is None or node.routing is None or node.routing.ospf is None:
        return ()

    interfaces: list[str] = []
    for link in plan.links:
        endpoint: EndpointPlan | None = None
        peer: EndpointPlan | None = None
        if link.left.node == node.name:
            endpoint, peer = link.left, link.right
        elif link.right.node == node.name:
            endpoint, peer = link.right, link.left
        if endpoint is None or peer is None:
            continue
        peer_node = plan.nodes.get(peer.node)
        if (
            peer_node is not None
            and peer_node.kind == "linux"
            and peer_node.routing is not None
            and peer_node.routing.ospf is not None
        ):
            interfaces.append(endpoint.interface)
    return tuple(dict.fromkeys(interfaces))


def render_frr_config(node: NodePlan, plan: TopologyPlan | None = None) -> str:
    """Render the supported OSPFv2, eBGP, and PIM-SM FRR config."""

    routing = node.routing
    if routing is None:
        raise ValueError(f"node has no routing configuration: {node.name}")

    lines = [
        "frr defaults traditional",
        f"hostname {_routing_hostname(node)}",
        "service integrated-vtysh-config",
        "!",
    ]

    if routing.ospf is not None:
        ospf = routing.ospf
        lines.extend(
            [
                "router ospf",
                f" ospf router-id {ospf.router_id}",
            ]
        )
        for network in _configured_or_connected(ospf.networks, node):
            lines.append(f" network {network} area {ospf.area}")
        for interface in ospf.passive_interfaces:
            lines.append(f" passive-interface {interface}")
        for interface in _ospf_point_to_point_interfaces(node, plan):
            lines.extend(
                [
                    f"interface {interface}",
                    " ip ospf network point-to-point",
                    "!",
                ]
            )
        lines.extend(["!", ""])

    if routing.bgp is not None:
        bgp = routing.bgp
        lines.extend(
            [
                f"router bgp {bgp.local_as}",
                f" bgp router-id {bgp.router_id}",
                " no bgp ebgp-requires-policy",
                " bgp log-neighbor-changes",
            ]
        )
        for neighbor in bgp.neighbors:
            lines.append(f" neighbor {neighbor.address} remote-as {neighbor.remote_as}")
        lines.extend([" address-family ipv4 unicast"])
        for network in _configured_or_connected(bgp.networks, node):
            lines.append(f"  network {network}")
        for neighbor in bgp.neighbors:
            lines.append(f"  neighbor {neighbor.address} activate")
        lines.extend([" exit-address-family", "!", ""])

    if routing.pim is not None:
        pim = routing.pim
        lines.extend([f"ip pim rp {pim.rp_address} 224.0.0.0/4", "!"])
        igmp_interfaces = frozenset(pim.igmp_interfaces)
        for interface in pim.interfaces:
            lines.extend([f"interface {interface}", " ip pim"])
            if interface in igmp_interfaces:
                lines.append(" ip igmp")
            lines.append("!")
        lines.append("")

    lines.extend(["line vty", ""])
    return "\n".join(lines)


def routing_protocols(routing: RoutingPlan | None) -> tuple[str, ...]:
    if routing is None:
        return ()
    protocols: list[str] = []
    if routing.ospf is not None:
        protocols.append("ospf")
    if routing.bgp is not None:
        protocols.append("bgp")
    if routing.pim is not None:
        protocols.append("pim")
    return tuple(protocols)


def routing_daemons(routing: RoutingPlan | None) -> tuple[str, ...]:
    """Return FRR daemon executable names for the enabled protocols."""

    if routing is None:
        return ()
    daemons: list[str] = []
    if routing.ospf is not None:
        daemons.append("ospfd")
    if routing.bgp is not None:
        daemons.append("bgpd")
    if routing.pim is not None:
        daemons.append("pimd")
    return tuple(daemons)


def routing_pathspace(plan: TopologyPlan, node: NodePlan) -> str:
    return f"nslab-{plan.name}-{node.name}"


@dataclass(frozen=True, slots=True)
class _ProcessRecord:
    daemon: str
    pid: int


@dataclass(frozen=True, slots=True)
class RoutingStatus:
    node: str
    protocols: tuple[str, ...]
    ready: bool
    pids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _FrrIdentity:
    uid: int
    gid: int


def _default_namespace_setter(namespace: str) -> None:
    # Import lazily so pure config rendering remains usable without pyroute2.
    from pyroute2 import netns

    netns.setns(namespace, flags=0)


def _default_binary_resolver(daemon: str) -> str | None:
    candidates = (
        Path("/usr/lib/frr") / daemon,
        Path("/usr/sbin") / daemon,
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(daemon)


class FrrRuntime:
    """Manage one isolated set of FRR daemons per nslab deployment."""

    def __init__(
        self,
        *,
        runtime_root: Path = Path("/run/nslab"),
        frr_state_root: Path = Path("/var/run/frr"),
        process_factory: Callable[..., Any] = subprocess.Popen,
        binary_resolver: Callable[[str], str | None] = _default_binary_resolver,
        namespace_setter: Callable[[str], None] = _default_namespace_setter,
        startup_timeout: float = _DEFAULT_STARTUP_TIMEOUT,
        stop_timeout: float = _DEFAULT_STOP_TIMEOUT,
        require_zebra_socket: bool = True,
        pid_exists: Callable[[int], bool] | None = None,
        frr_user: str = "frr",
        frr_group: str = "frr",
        frr_config_root: Path = Path("/etc/frr"),
    ) -> None:
        self.runtime_root = runtime_root
        self.frr_state_root = frr_state_root
        self.process_factory = process_factory
        self.binary_resolver = binary_resolver
        self.namespace_setter = namespace_setter
        self.startup_timeout = max(0.0, float(startup_timeout))
        self.stop_timeout = max(0.0, float(stop_timeout))
        self.require_zebra_socket = require_zebra_socket
        self._pid_exists = pid_exists if pid_exists is not None else self._os_pid_exists
        self.frr_user = frr_user
        self.frr_group = frr_group
        self.frr_config_root = frr_config_root

    @staticmethod
    def _os_pid_exists(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError as error:
            return error.errno != errno.ESRCH
        return True

    def _deployment_dir(self, plan: TopologyPlan) -> Path:
        return self.runtime_root / plan.name

    def _node_dir(self, plan: TopologyPlan, node: NodePlan) -> Path:
        return self._deployment_dir(plan) / node.name

    def _metadata_path(self, plan: TopologyPlan, node: NodePlan) -> Path:
        return self._node_dir(plan, node) / "processes.json"

    @staticmethod
    def _runtime_marker(plan: TopologyPlan) -> str:
        return f"{_RUNTIME_MARKER}\nname={plan.name}\nfingerprint={plan.fingerprint}\n"

    @staticmethod
    def _runtime_node_marker(plan: TopologyPlan, node: NodePlan) -> str:
        return (
            f"{_RUNTIME_NODE_MARKER}\n"
            f"name={plan.name}\n"
            f"node={node.name}\n"
            f"fingerprint={plan.fingerprint}\n"
        )

    @staticmethod
    def _state_marker(plan: TopologyPlan, node: NodePlan) -> str:
        return (
            f"{_STATE_MARKER}\n"
            f"name={plan.name}\n"
            f"node={node.name}\n"
            f"pathspace={routing_pathspace(plan, node)}\n"
            f"fingerprint={plan.fingerprint}\n"
        )

    @classmethod
    def _marker_path(cls, directory: Path) -> Path:
        return directory / _MARKER_FILE

    @classmethod
    def _read_matching_marker(
        cls,
        directory: Path,
        expected: str,
        *,
        required: bool = True,
    ) -> bool:
        marker = cls._marker_path(directory)
        if marker.is_symlink():
            raise cls._invalid_path(marker, "routing runtime marker is a symlink")
        if not cls._path_exists(marker):
            if required:
                raise cls._conflicting_path(
                    directory,
                    "routing pathspace is not managed by nslab",
                )
            return False
        if not marker.is_file():
            raise cls._invalid_path(marker, "routing runtime marker is not a regular file")
        try:
            current = marker.read_text(encoding="ascii")
        except (OSError, UnicodeError) as error:
            raise NslabError(
                code="ROUTING_RUNTIME_INVALID",
                message=f"cannot read routing runtime marker: {marker}",
                details={"path": str(marker), "error": str(error)},
            ) from error
        if current != expected:
            raise cls._conflicting_path(
                directory,
                "routing pathspace is owned by another configuration",
            )
        return True

    @classmethod
    def _prepare_managed_directory(
        cls,
        path: Path,
        marker_text: str,
        identity: _FrrIdentity | None,
    ) -> None:
        existed = cls._path_exists(path)
        cls._ensure_directory(path)
        if existed:
            cls._read_matching_marker(path, marker_text)
        else:
            cls._write_text(cls._marker_path(path), marker_text)
        cls._prepare_owned_directory(path, identity)
        cls._prepare_owned_file(cls._marker_path(path), identity, 0o640)

    @classmethod
    def _validate_managed_directory(
        cls,
        path: Path,
        marker_text: str,
    ) -> bool:
        if not cls._path_exists(path):
            return False
        if path.is_symlink():
            raise cls._invalid_path(path, "routing pathspace is a symlink")
        if not path.is_dir():
            raise cls._invalid_path(path, "routing pathspace is not a directory")
        cls._read_matching_marker(path, marker_text)
        return True

    @classmethod
    def _legacy_runtime_node_safe(cls, path: Path) -> bool:
        """Recognize the narrow artifact set used by pre-marker runtimes.

        Older nslab versions did not write runtime markers.  Keeping a
        constrained recovery path lets ``destroy`` reclaim a torn metadata
        directory without turning an arbitrary directory into a deletion
        target.  FRR pathspaces under ``/var/run/frr`` and ``/etc/frr`` never
        use this compatibility path and still require markers unconditionally.
        """

        if path.is_symlink() or not path.is_dir():
            return False
        allowed = {
            "processes.json",
            "frr.conf",
            "zebra.api",
            "zebra.pid",
            "ospfd.pid",
            "bgpd.pid",
            "zebra.log",
            "ospfd.log",
            "bgpd.log",
        }
        children = tuple(path.iterdir())
        if not children or not any(item.name.endswith(".pid") for item in children):
            return False
        return all(
            not item.is_symlink() and item.name in allowed and not item.is_dir()
            for item in children
        )

    def _validate_runtime_artifacts(self, plan: TopologyPlan) -> bool:
        """Validate the deployment runtime and return whether it is legacy."""

        deployment_dir = self._deployment_dir(plan)
        if not self._path_exists(deployment_dir):
            return False
        if deployment_dir.is_symlink():
            raise self._invalid_path(deployment_dir, "routing pathspace is a symlink")
        if not deployment_dir.is_dir():
            raise self._invalid_path(deployment_dir, "routing pathspace is not a directory")
        marker = self._marker_path(deployment_dir)
        if marker.is_symlink():
            raise self._invalid_path(marker, "routing runtime marker is a symlink")
        if self._path_exists(marker):
            self._read_matching_marker(deployment_dir, self._runtime_marker(plan))
            expected_nodes = {node.name for node in plan.nodes.values() if node.routing is not None}
            for child in deployment_dir.iterdir():
                if child.name == _MARKER_FILE:
                    continue
                if child.name not in expected_nodes:
                    raise self._conflicting_path(
                        child,
                        "unexpected routing runtime path",
                    )
                if child.is_symlink():
                    raise self._invalid_path(child, "routing runtime node path is a symlink")
                if not child.is_dir():
                    raise self._invalid_path(child, "routing runtime node path is not a directory")
            return False

        # Compatibility for runtimes created before marker support.  Refuse
        # empty or unexpected trees so an unmarked path cannot be swept.
        expected_nodes = {node.name for node in plan.nodes.values() if node.routing is not None}
        children = tuple(deployment_dir.iterdir())
        if (
            not children
            or {child.name for child in children} - expected_nodes
            or any(not self._legacy_runtime_node_safe(child) for child in children)
        ):
            raise self._conflicting_path(
                deployment_dir,
                "routing runtime is not managed by nslab",
            )
        return True

    def _validate_artifacts(self, plan: TopologyPlan) -> bool:
        """Validate every existing FRR path before reading or removing it."""

        legacy_runtime = self._validate_runtime_artifacts(plan)
        for node in plan.nodes.values():
            if node.routing is None:
                continue
            node_dir = self._node_dir(plan, node)
            if not legacy_runtime:
                self._validate_managed_directory(node_dir, self._runtime_node_marker(plan, node))
            elif self._path_exists(node_dir) and not self._legacy_runtime_node_safe(node_dir):
                raise self._conflicting_path(
                    node_dir,
                    "routing runtime is not managed by nslab",
                )
            if self._path_exists(node_dir):
                for child in node_dir.iterdir():
                    if child.is_symlink():
                        raise self._invalid_path(
                            child,
                            "routing runtime file is a symlink",
                        )
            self._validate_managed_directory(
                self.frr_state_root / routing_pathspace(plan, node),
                self._state_marker(plan, node),
            )
            self._validate_managed_directory(
                self._vty_config_dir(plan, node),
                self._vty_config_marker(plan, node),
            )
        return legacy_runtime

    def _has_artifacts(self, plan: TopologyPlan) -> bool:
        if self._path_exists(self._deployment_dir(plan)):
            return True
        for node in plan.nodes.values():
            if node.routing is None:
                continue
            if self._path_exists(self.frr_state_root / routing_pathspace(plan, node)):
                return True
            if self._path_exists(self._vty_config_dir(plan, node)):
                return True
        return False

    @staticmethod
    def _record_from_fields(daemon: object, pid: object) -> _ProcessRecord | None:
        if (
            not isinstance(daemon, str)
            or daemon not in _FRR_DAEMONS
            or type(pid) is not int
            or pid <= 0
        ):
            return None
        return _ProcessRecord(daemon=daemon, pid=pid)

    @staticmethod
    def _path_exists(path: Path) -> bool:
        """Return whether a path exists, including a dangling symlink."""

        return os.path.lexists(path)

    @staticmethod
    def _invalid_path(path: Path, description: str) -> NslabError:
        return NslabError(
            code="ROUTING_RUNTIME_INVALID",
            message=f"{description}: {path}",
            details={"path": str(path)},
        )

    @staticmethod
    def _conflicting_path(path: Path, description: str) -> NslabError:
        return NslabError(
            code="ROUTING_RUNTIME_CONFLICT",
            message=f"{description}: {path}",
            details={"path": str(path)},
        )

    @classmethod
    def _ensure_directory(cls, path: Path) -> None:
        # ``Path.exists()`` is false for a dangling symlink.  Check the link
        # itself before mkdir() so a stale path can never be followed.
        if path.is_symlink():
            raise cls._invalid_path(path, "routing runtime path is a symlink")
        if cls._path_exists(path) and not path.is_dir():
            raise cls._invalid_path(path, "routing runtime path is not a directory")
        try:
            path.mkdir(parents=True, mode=0o755, exist_ok=True)
            os.chmod(path, 0o755)
        except OSError as error:
            raise NslabError(
                code="ROUTING_RUNTIME_INVALID",
                message=f"failed to prepare routing runtime directory: {path}",
                details={"path": str(path), "error": str(error)},
            ) from error

    @classmethod
    def _write_text(cls, path: Path, text: str, mode: int = 0o640) -> None:
        # os.replace() does not follow the destination symlink, but replacing
        # one would hide an ownership collision.  Reject it explicitly.
        if path.is_symlink():
            raise cls._invalid_path(path, "routing runtime file is a symlink")
        temporary: Path | None = None
        try:
            # mkstemp() creates the temporary file atomically and never follows
            # an attacker-provided path in the parent directory.
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.tmp-",
                dir=str(path.parent),
                text=True,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            if path.is_symlink():
                raise cls._invalid_path(path, "routing runtime file is a symlink")
            os.replace(temporary, path)
            temporary = None
        except NslabError:
            raise
        except OSError as error:
            raise NslabError(
                code="ROUTING_RUNTIME_INVALID",
                message=f"failed to write routing runtime file: {path}",
                details={"path": str(path), "error": str(error)},
            ) from error
        finally:
            if temporary is not None and os.path.lexists(temporary):
                with suppress(OSError):
                    temporary.unlink()

    def _read_records(self, plan: TopologyPlan, node: NodePlan) -> tuple[_ProcessRecord, ...]:
        metadata_path: Path | None = self._metadata_path(plan, node)
        records: list[_ProcessRecord] = []
        document: object | None = None
        node_dir = self._node_dir(plan, node)
        if node_dir.is_symlink() or not node_dir.is_dir():
            return ()
        if metadata_path is not None and metadata_path.is_symlink():
            metadata_path = None
        if metadata_path is not None:
            try:
                with metadata_path.open(encoding="utf-8") as handle:
                    document = json.load(handle)
            except (FileNotFoundError, OSError):
                document = None
            except (UnicodeError, json.JSONDecodeError):
                # The pid files are written by FRR before nslab records metadata.
                # A torn/old metadata file must not prevent destroy from recovering
                # those processes; readiness remains false via _metadata_matches().
                document = None

        if isinstance(document, dict):
            raw_records = document.get("processes", [])
            if isinstance(raw_records, list):
                recorded_daemons: set[str] = set()
                for raw in raw_records:
                    if not isinstance(raw, dict):
                        continue
                    record = self._record_from_fields(raw.get("daemon"), raw.get("pid"))
                    if record is not None and record.daemon not in recorded_daemons:
                        records.append(record)
                        recorded_daemons.add(record.daemon)

        # A crash between spawning a daemon and writing metadata must still be
        # recoverable by destroy; pid files are the secondary source of truth.
        recorded_daemons = {record.daemon for record in records}
        for daemon in _FRR_DAEMONS:
            pid_path = node_dir / f"{daemon}.pid"
            if pid_path.is_symlink():
                continue
            try:
                value = pid_path.read_text(encoding="ascii").strip()
                pid = int(value)
            except (FileNotFoundError, OSError, UnicodeError, ValueError):
                continue
            record = _ProcessRecord(daemon=daemon, pid=pid)
            if pid > 0 and daemon not in recorded_daemons:
                records.append(record)
                recorded_daemons.add(daemon)
        return tuple(records)

    def _metadata_matches(self, plan: TopologyPlan, node: NodePlan) -> bool:
        path = self._metadata_path(plan, node)
        try:
            if not self._path_exists(self._deployment_dir(plan)):
                return False
            self._validate_runtime_artifacts(plan)
            if not self._validate_managed_directory(
                self._node_dir(plan, node),
                self._runtime_node_marker(plan, node),
            ):
                return False
            if not self._validate_managed_directory(
                self.frr_state_root / routing_pathspace(plan, node),
                self._state_marker(plan, node),
            ):
                return False
            config_dir = self._vty_config_dir(plan, node)
            if self._path_exists(config_dir):
                self._validate_managed_directory(config_dir, self._vty_config_marker(plan, node))
        except NslabError:
            return False
        if path.is_symlink():
            return False
        try:
            with path.open(encoding="utf-8") as handle:
                document = json.load(handle)
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return False
        if not isinstance(document, dict):
            return False
        if document.get("version") != _ROUTING_RUNTIME_VERSION:
            return False
        if document.get("fingerprint") != plan.fingerprint:
            return False
        if document.get("node") != node.name or document.get("namespace") != node.namespace:
            return False
        if document.get("pathspace") != routing_pathspace(plan, node):
            return False
        expected_protocols = list(routing_protocols(node.routing))
        if document.get("protocols") != expected_protocols:
            return False
        raw_processes = document.get("processes")
        if not isinstance(raw_processes, list):
            return False
        expected = {"zebra", *routing_daemons(node.routing)}
        records: list[_ProcessRecord] = []
        for item in raw_processes:
            if not isinstance(item, dict):
                return False
            record = self._record_from_fields(item.get("daemon"), item.get("pid"))
            if record is None or record.daemon not in expected:
                return False
            if record.daemon in {item.daemon for item in records}:
                return False
            records.append(record)
        return len(records) == len(expected) and {record.daemon for record in records} == expected

    def _status_for_node(self, plan: TopologyPlan, node: NodePlan) -> RoutingStatus:
        protocols = routing_protocols(node.routing)
        records = self._read_records(plan, node)
        expected_daemons = {"zebra", *routing_daemons(node.routing)}
        ready = (
            self._metadata_matches(plan, node)
            and all(self._pid_exists(record.pid) for record in records)
            and {record.daemon for record in records} == expected_daemons
        )
        return RoutingStatus(
            node=node.name,
            protocols=protocols,
            ready=ready,
            pids=tuple(record.pid for record in records),
        )

    def status(self, plan: TopologyPlan) -> tuple[RoutingStatus, ...]:
        return tuple(
            self._status_for_node(plan, node)
            for node in plan.nodes.values()
            if node.routing is not None
        )

    def ready(self, plan: TopologyPlan) -> bool:
        return all(status.ready for status in self.status(plan))

    def _resolve_binaries(self, node: NodePlan) -> dict[str, str]:
        required = ("zebra", *routing_daemons(node.routing))
        binaries: dict[str, str] = {}
        for daemon in required:
            binary = self.binary_resolver(daemon)
            if binary is None:
                raise NslabError(
                    code="ROUTING_DEPENDENCY_MISSING",
                    message=f"FRRouting daemon is not installed: {daemon}",
                    details={"daemon": daemon, "package": "frr"},
                )
            binaries[daemon] = binary
        return binaries

    def _resolve_frr_identity(self) -> _FrrIdentity | None:
        """Resolve the packaged FRR account used by all managed daemons.

        FRR checks that its run user belongs to ``frrvty`` before starting. A
        direct ``-u root`` invocation therefore fails on a normal Ubuntu
        installation, even when nslab itself is running as root. Unit tests
        and non-root config-only callers do not need ownership changes; the
        live CLI always reaches this method as root.
        """

        if os.geteuid() != 0:
            return None
        try:
            user = pwd.getpwnam(self.frr_user)
            group = grp.getgrnam(self.frr_group)
            vty_group = grp.getgrnam("frrvty")
        except KeyError as error:
            missing = str(error).strip("'")
            raise NslabError(
                code="ROUTING_DEPENDENCY_MISSING",
                message=f"FRRouting account is not installed: {missing}",
                details={
                    "user": self.frr_user,
                    "group": self.frr_group,
                    "vty_group": "frrvty",
                    "package": "frr",
                },
            ) from error
        if user.pw_gid != vty_group.gr_gid and user.pw_name not in vty_group.gr_mem:
            raise NslabError(
                code="ROUTING_DEPENDENCY_MISSING",
                message=f"FRRouting user is not a member of frrvty: {self.frr_user}",
                details={
                    "user": self.frr_user,
                    "vty_group": "frrvty",
                    "package": "frr",
                },
            )
        return _FrrIdentity(uid=int(user.pw_uid), gid=int(group.gr_gid))

    @staticmethod
    def _prepare_owned_directory(path: Path, identity: _FrrIdentity | None) -> None:
        if identity is None:
            return
        if path.is_symlink():
            raise NslabError(
                code="ROUTING_RUNTIME_INVALID",
                message=f"cannot change ownership of symlink: {path}",
                details={"path": str(path)},
            )
        try:
            os.chown(path, identity.uid, identity.gid)
            # FRR's vtysh group needs execute permission to traverse each
            # pathspace directory; the daemon itself owns the directory and
            # retains write access through the owner bits.
            os.chmod(path, 0o755)
        except OSError as error:
            raise NslabError(
                code="ROUTING_RUNTIME_INVALID",
                message=f"failed to prepare FRRouting runtime directory: {path}",
                details={"path": str(path), "error": str(error)},
            ) from error

    @staticmethod
    def _prepare_owned_file(
        path: Path,
        identity: _FrrIdentity | None,
        mode: int,
    ) -> None:
        if identity is None:
            return
        if path.is_symlink():
            raise NslabError(
                code="ROUTING_RUNTIME_INVALID",
                message=f"cannot change ownership of symlink: {path}",
                details={"path": str(path)},
            )
        try:
            os.chown(path, identity.uid, identity.gid)
            os.chmod(path, mode)
        except OSError as error:
            raise NslabError(
                code="ROUTING_RUNTIME_INVALID",
                message=f"failed to prepare FRRouting runtime file: {path}",
                details={"path": str(path), "error": str(error)},
            ) from error

    def _vty_config_dir(self, plan: TopologyPlan, node: NodePlan) -> Path:
        return self.frr_config_root / routing_pathspace(plan, node)

    @staticmethod
    def _vty_config_marker(plan: TopologyPlan, node: NodePlan) -> str:
        return (
            f"{_VTY_CONFIG_MARKER}\n"
            f"name={plan.name}\n"
            f"node={node.name}\n"
            f"fingerprint={plan.fingerprint}\n"
        )

    def _prepare_vty_config(
        self,
        plan: TopologyPlan,
        node: NodePlan,
        identity: _FrrIdentity | None,
    ) -> None:
        """Create the pathspace files consumed by ``vtysh -N``.

        vtysh keeps its control file under ``/etc/frr/<pathspace>`` even when
        every daemon config and pid file is explicitly redirected elsewhere.
        A marker makes collision detection and cleanup safe for pre-existing
        administrator-owned FRR pathspaces.
        """

        # A non-root config-only/test runtime must still be able to exercise
        # an injected config root.  The system default is skipped because a
        # caller without root cannot safely create pathspaces under /etc/frr.
        if identity is None and self.frr_config_root == Path("/etc/frr"):
            return
        self._ensure_directory(self.frr_config_root)
        config_dir = self._vty_config_dir(plan, node)
        expected_marker = self._vty_config_marker(plan, node)
        self._prepare_managed_directory(config_dir, expected_marker, identity)
        vtysh_config = config_dir / "vtysh.conf"
        self._write_text(vtysh_config, "service integrated-vtysh-config\n")
        self._prepare_owned_file(vtysh_config, identity, 0o640)
        integrated_config = config_dir / "frr.conf"
        self._write_text(integrated_config, render_frr_config(node, plan))
        self._prepare_owned_file(integrated_config, identity, 0o640)

    def _spawn(
        self,
        *,
        node: NodePlan,
        daemon: str,
        binary: str,
        config: Path,
        zapi: Path,
        pidfile: Path,
        pathspace: str,
        log_path: Path,
        identity: _FrrIdentity | None,
        vty_socket: Path | None = None,
    ) -> Any:
        if vty_socket is None:
            vty_socket = self.frr_state_root / pathspace
        argv = (
            binary,
            "-N",
            pathspace,
            "-f",
            str(config),
            "-z",
            str(zapi),
            "-i",
            str(pidfile),
            "-u",
            self.frr_user,
            "-g",
            self.frr_group,
            "-P",
            "0",
            "--vty_socket",
            str(vty_socket),
            "--log",
            f"file:{log_path}",
        )
        if log_path.is_symlink():
            raise self._invalid_path(log_path, "FRRouting log path is a symlink")
        if self._path_exists(log_path) and not log_path.is_file():
            raise self._invalid_path(log_path, "FRRouting log path is not a regular file")
        for managed_path in (config, zapi, pidfile):
            if managed_path.is_symlink():
                raise self._invalid_path(
                    managed_path,
                    "FRRouting runtime file is a symlink",
                )
            if self._path_exists(managed_path) and managed_path.is_dir():
                raise self._invalid_path(
                    managed_path,
                    "FRRouting runtime file is a directory",
                )
        if not self._path_exists(log_path):
            try:
                log_path.touch(mode=0o660)
            except OSError as error:
                raise NslabError(
                    code="ROUTING_RUNTIME_INVALID",
                    message=f"failed to create FRRouting log file: {log_path}",
                    details={"path": str(log_path), "error": str(error)},
                ) from error
        self._prepare_owned_file(log_path, identity, 0o660)
        with log_path.open("ab") as log_file:
            options: dict[str, object] = {
                "stdin": subprocess.DEVNULL,
                "stdout": log_file,
                "stderr": subprocess.STDOUT,
                "close_fds": True,
                "start_new_session": True,
            }
            options["preexec_fn"] = partial(self.namespace_setter, node.namespace)
            try:
                return self.process_factory(argv, **options)
            except OSError as error:
                raise NslabError(
                    code="ROUTING_START_FAILED",
                    message=f"failed to start FRRouting daemon: {daemon}",
                    details={"daemon": daemon, "node": node.name, "error": str(error)},
                ) from error

    @staticmethod
    def _poll(process: Any) -> int | None:
        value = process.poll()
        return None if value is None else int(value)

    def _wait_started(
        self,
        process: Any,
        *,
        daemon: str,
        node: NodePlan,
        socket_path: Path | None = None,
    ) -> None:
        deadline = time.monotonic() + self.startup_timeout
        while True:
            returncode = self._poll(process)
            if returncode is not None:
                raise NslabError(
                    code="ROUTING_START_FAILED",
                    message=f"FRRouting daemon exited during startup: {daemon}",
                    details={
                        "daemon": daemon,
                        "node": node.name,
                        "returncode": returncode,
                    },
                )
            if socket_path is None or socket_path.exists() or not self.require_zebra_socket:
                return
            if time.monotonic() >= deadline:
                raise NslabError(
                    code="ROUTING_START_FAILED",
                    message=f"FRRouting zebra socket was not ready: {node.name}",
                    details={"node": node.name, "socket": str(socket_path)},
                )
            time.sleep(0.05)

    def _save_metadata(
        self,
        plan: TopologyPlan,
        node: NodePlan,
        records: Sequence[_ProcessRecord],
        pathspace: str,
    ) -> None:
        document = {
            "version": _ROUTING_RUNTIME_VERSION,
            "fingerprint": plan.fingerprint,
            "node": node.name,
            "namespace": node.namespace,
            "pathspace": pathspace,
            "protocols": list(routing_protocols(node.routing)),
            "processes": [{"daemon": record.daemon, "pid": record.pid} for record in records],
        }
        self._write_text(
            self._metadata_path(plan, node),
            json.dumps(document, sort_keys=True, indent=2) + "\n",
        )

    def start(self, plan: TopologyPlan) -> None:
        routing_nodes = tuple(node for node in plan.nodes.values() if node.routing is not None)
        if not routing_nodes:
            return
        if self._has_artifacts(plan):
            # Validate ownership before checking readiness or reading pid files.
            # A same-named FRR pathspace may belong to the administrator or to a
            # different topology fingerprint and must never be removed here.
            self._validate_artifacts(plan)
            if self.ready(plan):
                return
            self.stop(plan)

        started: list[tuple[Any, _ProcessRecord]] = []
        try:
            identity = self._resolve_frr_identity()
            binaries_by_node = {node.name: self._resolve_binaries(node) for node in routing_nodes}
            self._ensure_directory(self.runtime_root)
            self._ensure_directory(self.frr_state_root)
            self._prepare_managed_directory(
                self._deployment_dir(plan),
                self._runtime_marker(plan),
                identity,
            )
            for node in routing_nodes:
                binaries = binaries_by_node[node.name]
                node_dir = self._node_dir(plan, node)
                self._prepare_managed_directory(
                    node_dir,
                    self._runtime_node_marker(plan, node),
                    identity,
                )
                state_dir = self.frr_state_root / routing_pathspace(plan, node)
                self._prepare_managed_directory(
                    state_dir,
                    self._state_marker(plan, node),
                    identity,
                )
                self._prepare_vty_config(plan, node, identity)
                config = node_dir / "frr.conf"
                zapi = node_dir / "zebra.api"
                self._write_text(config, render_frr_config(node, plan))
                self._prepare_owned_file(config, identity, 0o640)
                pathspace = routing_pathspace(plan, node)
                records: list[_ProcessRecord] = []

                zebra = self._spawn(
                    node=node,
                    daemon="zebra",
                    binary=binaries["zebra"],
                    config=config,
                    zapi=zapi,
                    pidfile=node_dir / "zebra.pid",
                    pathspace=pathspace,
                    log_path=node_dir / "zebra.log",
                    identity=identity,
                    vty_socket=state_dir,
                )
                zebra_record = _ProcessRecord("zebra", int(zebra.pid))
                started.append((zebra, zebra_record))
                self._wait_started(
                    zebra,
                    daemon="zebra",
                    node=node,
                    socket_path=zapi,
                )
                records.append(zebra_record)
                self._save_metadata(plan, node, records, pathspace)

                for daemon in routing_daemons(node.routing):
                    process = self._spawn(
                        node=node,
                        daemon=daemon,
                        binary=binaries[daemon],
                        config=config,
                        zapi=zapi,
                        pidfile=node_dir / f"{daemon}.pid",
                        pathspace=pathspace,
                        log_path=node_dir / f"{daemon}.log",
                        identity=identity,
                        vty_socket=state_dir,
                    )
                    record = _ProcessRecord(daemon, int(process.pid))
                    started.append((process, record))
                    self._wait_started(process, daemon=daemon, node=node)
                    records.append(record)
                    self._save_metadata(plan, node, records, pathspace)
        except BaseException as error:
            try:
                self._terminate_processes(started)
            finally:
                self._cleanup_deployment(plan)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(error, NslabError):
                raise
            raise NslabError(
                code="ROUTING_START_FAILED",
                message=f"failed to start routing daemons: {plan.name}",
                details={"name": plan.name, "error": str(error)},
            ) from error

    def _terminate_processes(self, processes: Sequence[tuple[Any, _ProcessRecord]]) -> None:
        for process, record in reversed(tuple(processes)):
            pid = record.pid
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except OSError:
                # Older/runtime-recovered daemons may not have retained their
                # original process group; signal the recorded PID as a fallback.
                with suppress(OSError, ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + self.stop_timeout
            while self._pid_exists(pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            if self._pid_exists(pid):
                with suppress(OSError, ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
                with suppress(OSError, ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            del process

    def _cleanup_deployment(self, plan: TopologyPlan) -> None:
        deployment_dir = self._deployment_dir(plan)
        self._validate_artifacts(plan)
        for node in plan.nodes.values():
            if node.routing is None:
                continue
            state_dir = self.frr_state_root / routing_pathspace(plan, node)
            self._validate_managed_directory(state_dir, self._state_marker(plan, node))
            config_dir = self._vty_config_dir(plan, node)
            self._validate_managed_directory(config_dir, self._vty_config_marker(plan, node))

        # All targets have been checked before the first removal.  This keeps a
        # foreign pathspace intact even if another node has a valid marker.
        if self._path_exists(deployment_dir):
            if deployment_dir.is_symlink():
                raise self._invalid_path(deployment_dir, "routing pathspace is a symlink")
            try:
                shutil.rmtree(deployment_dir)
            except OSError as error:
                raise NslabError(
                    code="ROUTING_RUNTIME_INVALID",
                    message=f"failed to remove routing runtime directory: {deployment_dir}",
                    details={"path": str(deployment_dir), "error": str(error)},
                ) from error
        for node in plan.nodes.values():
            if node.routing is None:
                continue
            state_dir = self.frr_state_root / routing_pathspace(plan, node)
            if self._path_exists(state_dir):
                if state_dir.is_symlink():
                    raise self._invalid_path(state_dir, "routing pathspace is a symlink")
                try:
                    shutil.rmtree(state_dir)
                except OSError as error:
                    raise NslabError(
                        code="ROUTING_RUNTIME_INVALID",
                        message=f"failed to remove FRRouting state directory: {state_dir}",
                        details={"path": str(state_dir), "error": str(error)},
                    ) from error
            config_dir = self._vty_config_dir(plan, node)
            if self._path_exists(config_dir):
                if config_dir.is_symlink():
                    raise self._invalid_path(config_dir, "routing pathspace is a symlink")
                try:
                    shutil.rmtree(config_dir)
                except OSError as error:
                    raise NslabError(
                        code="ROUTING_RUNTIME_INVALID",
                        message=f"failed to remove FRRouting config directory: {config_dir}",
                        details={"path": str(config_dir), "error": str(error)},
                    ) from error

    def stop(self, plan: TopologyPlan) -> None:
        self._validate_artifacts(plan)
        processes: list[tuple[Any, _ProcessRecord]] = []
        for node in plan.nodes.values():
            if node.routing is None:
                continue
            for record in self._read_records(plan, node):
                if self._pid_exists(record.pid):
                    processes.append((None, record))
        self._terminate_processes(processes)
        self._cleanup_deployment(plan)
