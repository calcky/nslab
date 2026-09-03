from __future__ import annotations

from dataclasses import FrozenInstanceError, is_dataclass, replace
from inspect import signature
from ipaddress import IPv4Address, IPv4Interface, IPv4Network

import pytest

from nslab.backend.base import (
    ExecResult,
    InterfaceInventory,
    LiveInventory,
    NamespaceInventory,
    NetworkBackend,
    expected_main_table_routes,
    inventory_matches_plan,
    recorded_link_ids_match_inventory,
)
from nslab.backend.fake import FakeNetworkBackend
from nslab.errors import NslabError
from nslab.manifest import Manifest
from nslab.planner import (
    FqCodelPlan,
    NetemPlan,
    QdiscPlan,
    RoutePlan,
    TbfPlan,
    TopologyPlan,
    compile_plan,
)


@pytest.fixture
def bridge_plan() -> TopologyPlan:
    manifest = Manifest.model_validate(
        {
            "version": 1,
            "name": "bridge-fdb",
            "topology": {
                "nodes": {
                    "h1": {
                        "kind": "linux",
                        "interfaces": {
                            "eth0": {"addresses": ["10.10.0.1/24"]},
                        },
                        "routes": [
                            {
                                "dst": "default",
                                "via": "10.10.0.254",
                                "dev": "eth0",
                            }
                        ],
                        "sysctls": {"net.ipv4.ip_forward": 1},
                    },
                    "sw1": {
                        "kind": "bridge",
                        "bridge": {
                            "name": "br0",
                            "stp": True,
                            "vlan_filtering": False,
                            "priority": 4096,
                            "ports": {
                                "swp1": {
                                    "path_cost": 10,
                                    "priority": 16,
                                    "hairpin": True,
                                    "isolated": True,
                                    "learning": False,
                                    "flood": False,
                                    "multicast_flood": False,
                                },
                                "swp2": {"path_cost": 100},
                            },
                        },
                        "interfaces": {
                            "br0": {"addresses": ["192.0.2.1/24"]},
                        },
                    },
                    "h2": {
                        "kind": "linux",
                        "interfaces": {
                            "eth0": {"addresses": ["10.10.0.2/24"]},
                        },
                    },
                },
                "links": [
                    {"endpoints": ["h1:eth0", "sw1:swp1"], "mtu": 1500},
                    {"endpoints": ["h2:eth0", "sw1:swp2"], "mtu": 1400},
                ],
            },
        }
    )
    return compile_plan(manifest)


def _create_topology(backend: FakeNetworkBackend, plan: TopologyPlan) -> None:
    for node in plan.nodes.values():
        backend.create_namespace(node)
    for node in plan.nodes.values():
        if node.kind == "bridge":
            backend.create_bridge(node)
    for link in plan.links:
        backend.create_veth(link)
    for node in plan.nodes.values():
        backend.configure_node(node, plan)


def _replace_interface(
    inventory: LiveInventory,
    namespace: str,
    interface: str,
    **changes: object,
) -> LiveInventory:
    namespace_inventory = inventory.namespaces[namespace]
    interfaces = dict(namespace_inventory.interfaces)
    interfaces[interface] = replace(interfaces[interface], **changes)
    return _replace_namespace(inventory, namespace, interfaces=interfaces)


def _replace_namespace(
    inventory: LiveInventory,
    namespace: str,
    **changes: object,
) -> LiveInventory:
    namespace_inventory = inventory.namespaces[namespace]
    namespaces = dict(inventory.namespaces)
    namespaces[namespace] = replace(namespace_inventory, **changes)
    return LiveInventory(
        namespaces=namespaces,
        root_interfaces=inventory.root_interfaces,
    )


def test_backend_protocol_exposes_only_the_fixed_typed_operations() -> None:
    expected_parameters = {
        "create_namespace": ("self", "node"),
        "delete_namespace": ("self", "namespace"),
        "create_bridge": ("self", "node"),
        "create_veth": ("self", "link"),
        "configure_node": ("self", "node", "plan"),
        "inventory": ("self", "plan"),
        "execute": ("self", "namespace", "argv", "capture_output"),
    }

    for method_name, parameters in expected_parameters.items():
        assert tuple(signature(getattr(NetworkBackend, method_name)).parameters) == parameters

    assert isinstance(FakeNetworkBackend(), NetworkBackend)


def test_fake_exec_records_capture_mode_and_suppresses_passthrough_text(
    bridge_plan: TopologyPlan,
) -> None:
    configured = ExecResult(
        argv=("ignored",),
        returncode=7,
        stdout="captured out\n",
        stderr="captured err\n",
    )
    backend = FakeNetworkBackend(execute_result=configured)
    _create_topology(backend, bridge_plan)
    namespace = bridge_plan.nodes["h1"].namespace
    argv = ("iperf3", "-s")

    captured = backend.execute(namespace, argv)
    passthrough = backend.execute(namespace, argv, capture_output=False)

    assert (captured.stdout, captured.stderr) == (
        "captured out\n",
        "captured err\n",
    )
    assert (passthrough.stdout, passthrough.stderr) == ("", "")
    assert backend.execute_requests == [
        (namespace, argv, True),
        (namespace, argv, False),
    ]


def test_fake_backend_builds_semantically_matching_inventory_and_records_calls(
    bridge_plan: TopologyPlan,
) -> None:
    backend = FakeNetworkBackend()

    _create_topology(backend, bridge_plan)
    inventory = backend.inventory(bridge_plan)

    h1 = bridge_plan.nodes["h1"]
    sw1 = bridge_plan.nodes["sw1"]
    h2 = bridge_plan.nodes["h2"]
    first_link, second_link = bridge_plan.links
    assert backend.calls == [
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
        ("inventory", bridge_plan.name),
    ]
    assert isinstance(backend.namespaces, dict)
    assert isinstance(backend.namespaces[sw1.namespace].interfaces, dict)
    assert inventory_matches_plan(bridge_plan, inventory)

    first_left = inventory.namespaces[first_link.left.namespace].interfaces[
        first_link.left.interface
    ]
    first_right = inventory.namespaces[first_link.right.namespace].interfaces[
        first_link.right.interface
    ]
    second_left = inventory.namespaces[second_link.left.namespace].interfaces[
        second_link.left.interface
    ]
    second_right = inventory.namespaces[second_link.right.namespace].interfaces[
        second_link.right.interface
    ]
    assert first_left.link_id is not None
    assert first_left.link_id == first_right.link_id
    assert second_left.link_id is not None
    assert second_left.link_id == second_right.link_id
    assert first_left.link_id != second_left.link_id

    h1_inventory = inventory.namespaces[h1.namespace]
    assert h1_inventory.node == "h1"
    assert h1_inventory.kind == "linux"
    assert h1_inventory.exists is True
    assert h1_inventory.interfaces["eth0"].addresses == (IPv4Interface("10.10.0.1/24"),)
    assert h1_inventory.routes == (
        RoutePlan(
            dst=IPv4Network("127.0.0.0/8"),
            via=None,
            dev="lo",
        ),
        RoutePlan(
            dst=IPv4Network("10.10.0.0/24"),
            via=None,
            dev="eth0",
        ),
        bridge_plan.nodes["h1"].routes[0],
    )
    assert h1_inventory.sysctls == {"net.ipv4.ip_forward": 1}

    sw1_inventory = inventory.namespaces[sw1.namespace]
    assert tuple(sw1_inventory.interfaces) == ("lo", "br0", "swp1", "swp2")
    assert sw1_inventory.interfaces["br0"].kind == "bridge"
    assert sw1_inventory.interfaces["br0"].addresses == (IPv4Interface("192.0.2.1/24"),)
    assert sw1_inventory.interfaces["br0"].stp is True
    assert sw1_inventory.interfaces["br0"].vlan_filtering is False
    assert sw1_inventory.interfaces["br0"].bridge_priority == 4096
    assert sw1_inventory.interfaces["swp1"].master == "br0"
    assert sw1_inventory.interfaces["swp1"].mtu == 1500
    assert sw1_inventory.interfaces["swp1"].path_cost == 10
    assert sw1_inventory.interfaces["swp1"].port_priority == 16
    assert sw1_inventory.interfaces["swp1"].hairpin is True
    assert sw1_inventory.interfaces["swp1"].isolated is True
    assert sw1_inventory.interfaces["swp1"].learning is False
    assert sw1_inventory.interfaces["swp1"].flood is False
    assert sw1_inventory.interfaces["swp1"].multicast_flood is False
    assert sw1_inventory.interfaces["swp2"].master == "br0"
    assert sw1_inventory.interfaces["swp2"].mtu == 1400
    assert sw1_inventory.interfaces["swp2"].path_cost == 100
    assert sw1_inventory.interfaces["swp2"].port_priority is None
    assert all(interface.up for interface in sw1_inventory.interfaces.values())


def test_inventory_records_are_deeply_immutable_and_exec_argv_is_a_tuple(
    bridge_plan: TopologyPlan,
) -> None:
    backend = FakeNetworkBackend()
    _create_topology(backend, bridge_plan)
    inventory = backend.inventory(bridge_plan)
    h1_namespace = bridge_plan.nodes["h1"].namespace
    result = backend.execute(h1_namespace, ["ping", "-c", "1", "10.10.0.2"])

    for inventory_type in (
        NamespaceInventory,
        InterfaceInventory,
        LiveInventory,
        ExecResult,
    ):
        assert is_dataclass(inventory_type)
        assert inventory_type.__dataclass_params__.frozen is True

    assert result == ExecResult(
        argv=("ping", "-c", "1", "10.10.0.2"),
        returncode=0,
        stdout="",
        stderr="",
    )
    with pytest.raises(FrozenInstanceError):
        result.returncode = 1  # type: ignore[misc]
    with pytest.raises(TypeError):
        inventory.namespaces["extra"] = inventory.namespaces[h1_namespace]  # type: ignore[index]
    with pytest.raises(TypeError):
        inventory.namespaces[h1_namespace].interfaces["extra"] = (  # type: ignore[index,assignment]
            inventory.namespaces[h1_namespace].interfaces["eth0"]
        )
    with pytest.raises(TypeError):
        inventory.namespaces[h1_namespace].sysctls["net.ipv4.ip_forward"] = 0  # type: ignore[index]
    with pytest.raises(TypeError):
        inventory.root_interfaces["leftover"] = inventory.namespaces[  # type: ignore[index]
            h1_namespace
        ].interfaces["eth0"]


def test_inventory_snapshot_does_not_share_mutable_fake_state(
    bridge_plan: TopologyPlan,
) -> None:
    backend = FakeNetworkBackend()
    for node in bridge_plan.nodes.values():
        backend.create_namespace(node)
    sw1 = bridge_plan.nodes["sw1"]
    backend.create_bridge(sw1)
    snapshot = backend.inventory(bridge_plan)

    for link in bridge_plan.links:
        backend.create_veth(link)
    for node in bridge_plan.nodes.values():
        backend.configure_node(node, bridge_plan)

    snapshot_sw1 = snapshot.namespaces[sw1.namespace]
    assert tuple(snapshot_sw1.interfaces) == ("lo", "br0")
    assert snapshot_sw1.interfaces["br0"].up is False
    assert snapshot_sw1.interfaces["br0"].addresses == ()
    assert snapshot_sw1.routes == (
        RoutePlan(
            dst=IPv4Network("127.0.0.0/8"),
            via=None,
            dev="lo",
        ),
    )
    assert snapshot_sw1.sysctls == {}


def test_semantic_comparison_ignores_ifindex_but_detects_other_drift(
    bridge_plan: TopologyPlan,
) -> None:
    backend = FakeNetworkBackend()
    _create_topology(backend, bridge_plan)
    inventory = backend.inventory(bridge_plan)
    h1_namespace = bridge_plan.nodes["h1"].namespace

    changed_ifindex = _replace_interface(
        inventory,
        h1_namespace,
        "eth0",
        ifindex=9999,
    )
    assert inventory_matches_plan(bridge_plan, changed_ifindex)

    changed_mtu = _replace_interface(
        inventory,
        h1_namespace,
        "eth0",
        mtu=9000,
    )
    assert not inventory_matches_plan(bridge_plan, changed_mtu)

    changed_address = _replace_interface(
        inventory,
        h1_namespace,
        "eth0",
        addresses=(IPv4Interface("198.51.100.1/24"),),
    )
    assert not inventory_matches_plan(bridge_plan, changed_address)


def test_fake_backend_applies_link_netem_to_both_endpoints_and_detects_drift(
    bridge_plan: TopologyPlan,
) -> None:
    netem = NetemPlan(delay_ms=100, jitter_ms=10, loss_percent=5)
    first_link = replace(bridge_plan.links[0], netem=netem)
    plan = replace(bridge_plan, links=(first_link, bridge_plan.links[1]))
    backend = FakeNetworkBackend()
    _create_topology(backend, plan)

    inventory = backend.inventory(plan)

    left = inventory.namespaces[first_link.left.namespace].interfaces[first_link.left.interface]
    right = inventory.namespaces[first_link.right.namespace].interfaces[first_link.right.interface]
    assert left.netem == netem
    assert right.netem == netem
    assert inventory_matches_plan(plan, inventory)

    changed = _replace_interface(
        inventory,
        first_link.right.namespace,
        first_link.right.interface,
        netem=NetemPlan(delay_ms=50, jitter_ms=0, loss_percent=0),
    )
    assert not inventory_matches_plan(plan, changed)


@pytest.mark.parametrize(
    "qdisc",
    [
        TbfPlan(rate="10mbit", burst_bytes=32 * 1024, latency_ms=400),
        FqCodelPlan(target_ms=5, interval_ms=100, limit=10240, ecn=True),
    ],
)
def test_fake_backend_applies_link_qdisc_to_both_endpoints_and_detects_drift(
    bridge_plan: TopologyPlan, qdisc: QdiscPlan
) -> None:
    first_link = replace(bridge_plan.links[0], qdisc=qdisc)
    plan = replace(bridge_plan, links=(first_link, bridge_plan.links[1]))
    backend = FakeNetworkBackend()
    _create_topology(backend, plan)

    inventory = backend.inventory(plan)

    left = inventory.namespaces[first_link.left.namespace].interfaces[first_link.left.interface]
    right = inventory.namespaces[first_link.right.namespace].interfaces[first_link.right.interface]
    assert left.qdisc == qdisc
    assert right.qdisc == qdisc
    assert inventory_matches_plan(plan, inventory)

    changed = _replace_interface(
        inventory,
        first_link.right.namespace,
        first_link.right.interface,
        qdisc=FqCodelPlan(target_ms=10, interval_ms=100, limit=10240, ecn=True),
    )
    assert not inventory_matches_plan(plan, changed)


def test_semantic_comparison_detects_explicit_stp_tuning_drift(
    bridge_plan: TopologyPlan,
) -> None:
    backend = FakeNetworkBackend()
    _create_topology(backend, bridge_plan)
    inventory = backend.inventory(bridge_plan)
    sw1 = bridge_plan.nodes["sw1"]

    changed_bridge_priority = _replace_interface(
        inventory,
        sw1.namespace,
        "br0",
        bridge_priority=8192,
    )
    changed_path_cost = _replace_interface(
        inventory,
        sw1.namespace,
        "swp1",
        path_cost=20,
    )
    changed_port_priority = _replace_interface(
        inventory,
        sw1.namespace,
        "swp1",
        port_priority=32,
    )
    changed_hairpin = _replace_interface(
        inventory,
        sw1.namespace,
        "swp1",
        hairpin=False,
    )
    changed_isolated = _replace_interface(
        inventory,
        sw1.namespace,
        "swp1",
        isolated=False,
    )
    changed_learning = _replace_interface(
        inventory,
        sw1.namespace,
        "swp1",
        learning=True,
    )
    changed_flood = _replace_interface(
        inventory,
        sw1.namespace,
        "swp1",
        flood=True,
    )
    changed_multicast_flood = _replace_interface(
        inventory,
        sw1.namespace,
        "swp1",
        multicast_flood=True,
    )

    assert not inventory_matches_plan(bridge_plan, changed_bridge_priority)
    assert not inventory_matches_plan(bridge_plan, changed_path_cost)
    assert not inventory_matches_plan(bridge_plan, changed_port_priority)
    assert not inventory_matches_plan(bridge_plan, changed_hairpin)
    assert not inventory_matches_plan(bridge_plan, changed_isolated)
    assert not inventory_matches_plan(bridge_plan, changed_learning)
    assert not inventory_matches_plan(bridge_plan, changed_flood)
    assert not inventory_matches_plan(bridge_plan, changed_multicast_flood)


def test_semantic_comparison_requires_matching_unique_link_identities(
    bridge_plan: TopologyPlan,
) -> None:
    backend = FakeNetworkBackend()
    _create_topology(backend, bridge_plan)
    inventory = backend.inventory(bridge_plan)
    first_link, second_link = bridge_plan.links
    first_left = inventory.namespaces[first_link.left.namespace].interfaces[
        first_link.left.interface
    ]

    missing_identity = _replace_interface(
        inventory,
        first_link.left.namespace,
        first_link.left.interface,
        link_id=None,
    )
    assert not inventory_matches_plan(bridge_plan, missing_identity)

    mismatched_identity = _replace_interface(
        inventory,
        first_link.right.namespace,
        first_link.right.interface,
        link_id="different-link-id",
    )
    assert not inventory_matches_plan(bridge_plan, mismatched_identity)

    reused_identity = _replace_interface(
        inventory,
        second_link.left.namespace,
        second_link.left.interface,
        link_id=first_left.link_id,
    )
    reused_identity = _replace_interface(
        reused_identity,
        second_link.right.namespace,
        second_link.right.interface,
        link_id=first_left.link_id,
    )
    assert not inventory_matches_plan(bridge_plan, reused_identity)

    crossed_identity = _replace_interface(
        inventory,
        first_link.right.namespace,
        first_link.right.interface,
        link_id=inventory.namespaces[second_link.left.namespace]
        .interfaces[second_link.left.interface]
        .link_id,
    )
    crossed_identity = _replace_interface(
        crossed_identity,
        second_link.left.namespace,
        second_link.left.interface,
        link_id=first_left.link_id,
    )
    assert not inventory_matches_plan(bridge_plan, crossed_identity)

    third_endpoint = InterfaceInventory(
        name="unexpected-veth",
        kind="veth",
        ifindex=999,
        master=None,
        mtu=1500,
        up=True,
        link_id=first_left.link_id,
    )
    first_namespace = inventory.namespaces[first_link.left.namespace]
    interfaces = dict(first_namespace.interfaces)
    interfaces[third_endpoint.name] = third_endpoint
    third_endpoint_group = _replace_namespace(
        inventory,
        first_link.left.namespace,
        interfaces=interfaces,
    )
    assert not inventory_matches_plan(bridge_plan, third_endpoint_group)


def test_semantic_comparison_rejects_exact_temporary_root_artifacts(
    bridge_plan: TopologyPlan,
) -> None:
    backend = FakeNetworkBackend()
    _create_topology(backend, bridge_plan)
    inventory = backend.inventory(bridge_plan)
    temporary_name = bridge_plan.links[0].left.temporary_name
    leftover = InterfaceInventory(
        name=temporary_name,
        kind="veth",
        ifindex=700,
        master=None,
        mtu=1500,
        up=False,
        link_id="leftover-link-id",
    )

    drifted = replace(inventory, root_interfaces={temporary_name: leftover})

    assert not inventory_matches_plan(bridge_plan, drifted)
    assert inventory.root_interfaces == {}


def test_recorded_link_id_matcher_is_snapshot_aware_and_ignores_ifindex(
    bridge_plan: TopologyPlan,
) -> None:
    backend = FakeNetworkBackend()
    _create_topology(backend, bridge_plan)
    inventory = backend.inventory(bridge_plan)
    recorded: dict[str, object] = {}
    for link in bridge_plan.links:
        for endpoint in (link.left, link.right):
            observed = inventory.namespaces[endpoint.namespace].interfaces[endpoint.interface]
            recorded[f"{endpoint.node}:{endpoint.interface}"] = {
                "link_id": observed.link_id,
                "ifindex": observed.ifindex,
            }

    first = bridge_plan.links[0].left
    changed_ifindex = _replace_interface(
        inventory,
        first.namespace,
        first.interface,
        ifindex=9999,
    )
    assert recorded_link_ids_match_inventory(
        bridge_plan,
        changed_ifindex,
        recorded,
    )

    missing = dict(recorded)
    missing[f"{first.node}:{first.interface}"] = {"ifindex": 1}
    assert not recorded_link_ids_match_inventory(bridge_plan, inventory, missing)

    malformed = dict(recorded)
    malformed[f"{first.node}:{first.interface}"] = {"link_id": []}
    assert not recorded_link_ids_match_inventory(bridge_plan, inventory, malformed)


def test_semantic_comparison_detects_absence_master_up_and_bridge_drift(
    bridge_plan: TopologyPlan,
) -> None:
    backend = FakeNetworkBackend()
    _create_topology(backend, bridge_plan)
    inventory = backend.inventory(bridge_plan)
    h1_namespace = bridge_plan.nodes["h1"].namespace
    sw1_namespace = bridge_plan.nodes["sw1"].namespace

    absent_namespace = _replace_namespace(
        inventory,
        h1_namespace,
        exists=False,
    )
    assert not inventory_matches_plan(bridge_plan, absent_namespace)

    wrong_master = _replace_interface(
        inventory,
        sw1_namespace,
        "swp1",
        master=None,
    )
    assert not inventory_matches_plan(bridge_plan, wrong_master)

    interface_down = _replace_interface(
        inventory,
        h1_namespace,
        "eth0",
        up=False,
    )
    assert not inventory_matches_plan(bridge_plan, interface_down)

    changed_bridge_flags = _replace_interface(
        inventory,
        sw1_namespace,
        "br0",
        stp=False,
    )
    assert not inventory_matches_plan(bridge_plan, changed_bridge_flags)


def test_semantic_comparison_requires_exact_main_table_routes_and_declared_sysctls(
    bridge_plan: TopologyPlan,
) -> None:
    backend = FakeNetworkBackend()
    _create_topology(backend, bridge_plan)
    inventory = backend.inventory(bridge_plan)
    h1_namespace = bridge_plan.nodes["h1"].namespace
    h2_namespace = bridge_plan.nodes["h2"].namespace

    connected_routes = (
        RoutePlan(
            dst=IPv4Network("127.0.0.0/8"),
            via=None,
            dev="lo",
        ),
        RoutePlan(
            dst=IPv4Network("10.10.0.0/24"),
            via=None,
            dev="eth0",
        ),
    )
    kernel_enriched = _replace_namespace(
        inventory,
        h2_namespace,
        routes=connected_routes,
        sysctls={"net.ipv4.ip_forward": 0},
    )
    assert inventory_matches_plan(bridge_plan, kernel_enriched)

    h1_inventory = inventory.namespaces[h1_namespace]
    reordered_routes = _replace_namespace(
        inventory,
        h1_namespace,
        routes=tuple(reversed(h1_inventory.routes)),
    )
    assert inventory_matches_plan(bridge_plan, reordered_routes)

    for unexpected_route in (
        RoutePlan(
            dst=IPv4Network("198.51.100.0/24"),
            via=None,
            dev="eth0",
        ),
        RoutePlan(
            dst=IPv4Network("0.0.0.0/0"),
            via=IPv4Address("10.10.0.253"),
            dev="eth0",
        ),
    ):
        unexpected_route_drift = _replace_namespace(
            kernel_enriched,
            h2_namespace,
            routes=(*connected_routes, unexpected_route),
        )
        assert not inventory_matches_plan(bridge_plan, unexpected_route_drift)

    changed_route = replace(
        h1_inventory.routes[-1],
        via=IPv4Address("10.10.0.253"),
    )
    route_drift = _replace_namespace(
        inventory,
        h1_namespace,
        routes=(*h1_inventory.routes[:-1], changed_route),
    )
    assert not inventory_matches_plan(bridge_plan, route_drift)

    sysctl_drift = _replace_namespace(
        inventory,
        h1_namespace,
        sysctls={"net.ipv4.ip_forward": 0},
    )
    assert not inventory_matches_plan(bridge_plan, sysctl_drift)


def test_duplicate_creates_raise_resource_exists_and_do_not_mutate_state(
    bridge_plan: TopologyPlan,
) -> None:
    backend = FakeNetworkBackend()
    h1 = bridge_plan.nodes["h1"]
    sw1 = bridge_plan.nodes["sw1"]

    backend.create_namespace(h1)
    with pytest.raises(NslabError) as namespace_error:
        backend.create_namespace(h1)
    assert namespace_error.value.code == "RESOURCE_EXISTS"
    assert namespace_error.value.details["resource"] == h1.namespace

    backend.create_namespace(sw1)
    backend.create_bridge(sw1)
    with pytest.raises(NslabError) as bridge_error:
        backend.create_bridge(sw1)
    assert bridge_error.value.code == "RESOURCE_EXISTS"
    assert bridge_error.value.details["resource"] == f"{sw1.namespace}:br0"

    backend.create_veth(bridge_plan.links[0])
    before = backend.inventory(bridge_plan)
    with pytest.raises(NslabError) as veth_error:
        backend.create_veth(bridge_plan.links[0])
    assert veth_error.value.code == "RESOURCE_EXISTS"
    assert backend.inventory(bridge_plan) == before


def test_delete_namespace_is_idempotent_for_missing_resources(
    bridge_plan: TopologyPlan,
) -> None:
    backend = FakeNetworkBackend()
    namespace = bridge_plan.nodes["h1"].namespace

    backend.delete_namespace(namespace)
    backend.create_namespace(bridge_plan.nodes["h1"])
    backend.delete_namespace(namespace)
    backend.delete_namespace(namespace)

    inventory = backend.inventory(bridge_plan)
    assert inventory.namespaces[namespace].exists is False
    assert backend.calls[:4] == [
        ("delete_namespace", namespace),
        ("create_namespace", namespace),
        ("delete_namespace", namespace),
        ("delete_namespace", namespace),
    ]


def test_delete_namespace_removes_veth_peer_and_preserves_old_inventory(
    bridge_plan: TopologyPlan,
) -> None:
    backend = FakeNetworkBackend()
    _create_topology(backend, bridge_plan)
    snapshot = backend.inventory(bridge_plan)
    h1 = bridge_plan.nodes["h1"]
    sw1 = bridge_plan.nodes["sw1"]
    first_link = bridge_plan.links[0]

    backend.delete_namespace(h1.namespace)

    after_delete = backend.inventory(bridge_plan)
    assert after_delete.namespaces[h1.namespace].exists is False
    assert "swp1" not in after_delete.namespaces[sw1.namespace].interfaces
    assert "swp2" in after_delete.namespaces[sw1.namespace].interfaces

    backend.create_namespace(h1)
    backend.create_veth(first_link)

    after_recreate = backend.inventory(bridge_plan)
    assert "eth0" in after_recreate.namespaces[h1.namespace].interfaces
    assert "swp1" in after_recreate.namespaces[sw1.namespace].interfaces
    assert snapshot.namespaces[h1.namespace].exists is True
    assert "swp1" in snapshot.namespaces[sw1.namespace].interfaces
    assert "swp2" in snapshot.namespaces[sw1.namespace].interfaces

    backend.delete_namespace(h1.namespace)
    backend.delete_namespace(h1.namespace)
    assert backend.inventory(bridge_plan).namespaces[h1.namespace].exists is False


def test_delete_bridge_namespace_removes_surviving_peer_routes(
    bridge_plan: TopologyPlan,
) -> None:
    backend = FakeNetworkBackend()
    _create_topology(backend, bridge_plan)
    snapshot = backend.inventory(bridge_plan)
    sw1 = bridge_plan.nodes["sw1"]
    loopback_route = RoutePlan(
        dst=IPv4Network("127.0.0.0/8"),
        via=None,
        dev="lo",
    )

    backend.delete_namespace(sw1.namespace)

    after_delete = backend.inventory(bridge_plan)
    assert after_delete.namespaces[sw1.namespace].exists is False
    for host_name in ("h1", "h2"):
        host = bridge_plan.nodes[host_name]
        host_inventory = after_delete.namespaces[host.namespace]
        assert tuple(host_inventory.interfaces) == ("lo",)
        assert host_inventory.routes == (loopback_route,)

        snapshot_host = snapshot.namespaces[host.namespace]
        assert "eth0" in snapshot_host.interfaces
        assert any(route.dev == "eth0" for route in snapshot_host.routes)

    backend.create_namespace(sw1)
    backend.create_bridge(sw1)
    for link in bridge_plan.links:
        backend.create_veth(link)

    after_recreate = backend.inventory(bridge_plan)
    assert "swp1" in after_recreate.namespaces[sw1.namespace].interfaces
    assert "swp2" in after_recreate.namespaces[sw1.namespace].interfaces
    assert "eth0" in after_recreate.namespaces[bridge_plan.nodes["h1"].namespace].interfaces
    assert "eth0" in after_recreate.namespaces[bridge_plan.nodes["h2"].namespace].interfaces
    assert snapshot.namespaces[sw1.namespace].exists is True
    assert "swp1" in snapshot.namespaces[sw1.namespace].interfaces
    assert "swp2" in snapshot.namespaces[sw1.namespace].interfaces


def test_delete_namespace_handles_both_veth_endpoints_in_the_same_namespace(
    bridge_plan: TopologyPlan,
) -> None:
    backend = FakeNetworkBackend()
    h1 = bridge_plan.nodes["h1"]
    original_link = bridge_plan.links[0]
    same_namespace_link = replace(
        original_link,
        right=replace(
            original_link.right,
            node=h1.name,
            namespace=h1.namespace,
            interface="eth1",
        ),
    )
    backend.create_namespace(h1)
    backend.create_veth(same_namespace_link)

    backend.delete_namespace(h1.namespace)
    backend.create_namespace(h1)
    backend.create_veth(same_namespace_link)

    assert tuple(backend.namespaces[h1.namespace].interfaces) == ("lo", "eth0", "eth1")


def test_delete_namespace_records_injected_failure_before_mutating_veths(
    bridge_plan: TopologyPlan,
) -> None:
    backend = FakeNetworkBackend()
    _create_topology(backend, bridge_plan)
    snapshot = backend.inventory(bridge_plan)
    h1_namespace = bridge_plan.nodes["h1"].namespace
    backend.fail_on_call = len(backend.calls) + 1

    with pytest.raises(NslabError) as caught:
        backend.delete_namespace(h1_namespace)

    assert caught.value.code == "BACKEND_FAILURE"
    assert caught.value.details["operation"] == "delete_namespace"
    backend.fail_on_call = None
    assert backend.inventory(bridge_plan) == snapshot


def test_fail_on_call_four_is_deterministic_and_happens_before_mutation(
    bridge_plan: TopologyPlan,
) -> None:
    backend = FakeNetworkBackend(fail_on_call=4)
    nodes = tuple(bridge_plan.nodes.values())

    for node in nodes:
        backend.create_namespace(node)

    sw1 = bridge_plan.nodes["sw1"]
    with pytest.raises(NslabError) as caught:
        backend.create_bridge(sw1)

    assert caught.value.code == "BACKEND_FAILURE"
    assert caught.value.details == {
        "call": 4,
        "operation": "create_bridge",
        "resource": f"{sw1.namespace}:br0",
    }
    assert backend.calls[-1] == ("create_bridge", f"{sw1.namespace}:br0")
    assert "br0" not in backend.namespaces[sw1.namespace].interfaces


def test_route_inventory_uses_normalized_planner_values(bridge_plan: TopologyPlan) -> None:
    backend = FakeNetworkBackend()
    _create_topology(backend, bridge_plan)

    routes = backend.inventory(bridge_plan).namespaces[bridge_plan.nodes["h1"].namespace].routes
    assert routes[-1].dst == IPv4Network("0.0.0.0/0")
    assert routes[-1].via == IPv4Address("10.10.0.254")
    assert routes[-1].dev == "eth0"


def test_expected_main_table_routes_deduplicate_semantic_duplicates_in_order(
    bridge_plan: TopologyPlan,
) -> None:
    h1 = bridge_plan.nodes["h1"]
    default_route = h1.routes[0]
    duplicate_plan = replace(
        h1,
        interfaces={
            "eth0": (
                IPv4Interface("10.10.0.1/24"),
                IPv4Interface("10.10.0.2/24"),
            )
        },
        routes=(
            RoutePlan(
                dst=IPv4Network("10.10.0.0/24"),
                via=None,
                dev="eth0",
            ),
            default_route,
            default_route,
        ),
    )

    assert expected_main_table_routes(duplicate_plan) == (
        RoutePlan(
            dst=IPv4Network("127.0.0.0/8"),
            via=None,
            dev="lo",
        ),
        RoutePlan(
            dst=IPv4Network("10.10.0.0/24"),
            via=None,
            dev="eth0",
        ),
        default_route,
    )
