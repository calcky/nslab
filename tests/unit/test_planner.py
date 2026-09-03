from __future__ import annotations

from dataclasses import FrozenInstanceError, is_dataclass
from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv4Network,
    IPv6Address,
    IPv6Interface,
    IPv6Network,
)

import pytest

from nslab.errors import NslabError
from nslab.manifest import InterfaceConfig, LinuxNode, Manifest, manifest_fingerprint
from nslab.naming import namespace_name, temporary_veth_names
from nslab.planner import (
    BridgePortPlan,
    EndpointPlan,
    FqCodelPlan,
    LinkPlan,
    NetemPlan,
    NodePlan,
    RoutePlan,
    TbfPlan,
    TopologyPlan,
    compile_plan,
)


@pytest.fixture
def bridge_manifest() -> Manifest:
    return Manifest.model_validate(
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
                            "stp": False,
                            "vlan_filtering": False,
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


def test_compile_plan_preserves_manifest_order_and_desired_state(
    bridge_manifest: Manifest,
) -> None:
    plan = compile_plan(bridge_manifest)

    assert plan.name == "bridge-fdb"
    assert plan.fingerprint == manifest_fingerprint(bridge_manifest)
    assert tuple(plan.nodes) == ("h1", "sw1", "h2")
    assert plan.nodes["sw1"].namespace == namespace_name("bridge-fdb", "sw1")
    assert plan.nodes["sw1"].bridge_name == "br0"
    assert plan.nodes["sw1"].stp is False
    assert plan.nodes["sw1"].vlan_filtering is False
    assert plan.nodes["sw1"].interfaces == {"br0": (IPv4Interface("192.0.2.1/24"),)}
    assert plan.nodes["h1"].routes == (
        RoutePlan(
            dst=IPv4Network("0.0.0.0/0"),
            via=IPv4Address("10.10.0.254"),
            dev="eth0",
        ),
    )
    assert plan.nodes["h1"].sysctls == {"net.ipv4.ip_forward": 1}

    assert tuple(link.index for link in plan.links) == (0, 1)
    assert plan.links[0].left.node == "h1"
    assert plan.links[0].left.interface == "eth0"
    assert plan.links[0].right.node == "sw1"
    assert plan.links[0].right.interface == "swp1"
    assert plan.links[0].mtu == 1500
    assert plan.links[1].mtu == 1400
    assert (plan.links[0].left.temporary_name, plan.links[0].right.temporary_name) == (
        temporary_veth_names("bridge-fdb", 0)
    )


def test_compile_plan_preserves_ipv6_types(bridge_manifest: Manifest) -> None:
    document = bridge_manifest.model_dump(mode="json")
    h1 = document["topology"]["nodes"]["h1"]
    h1["interfaces"]["eth0"]["addresses"] = ["2001:db8:1::2/64"]
    h1["routes"] = [
        {"dst": "::/0", "via": "2001:db8:1::1", "dev": "eth0"},
    ]
    h1["sysctls"] = {"net.ipv6.conf.all.forwarding": 1}

    plan = compile_plan(Manifest.model_validate(document))

    assert plan.nodes["h1"].interfaces == {
        "eth0": (IPv6Interface("2001:db8:1::2/64"),),
    }
    assert plan.nodes["h1"].routes == (
        RoutePlan(
            dst=IPv6Network("::/0"),
            via=IPv6Address("2001:db8:1::1"),
            dev="eth0",
        ),
    )
    assert plan.nodes["h1"].sysctls == {"net.ipv6.conf.all.forwarding": 1}


def test_compile_plan_preserves_link_netem(bridge_manifest: Manifest) -> None:
    document = bridge_manifest.model_dump(mode="json")
    document["topology"]["links"][0]["netem"] = {
        "delay_ms": 100,
        "jitter_ms": 10,
        "loss_percent": 5,
    }

    plan = compile_plan(Manifest.model_validate(document))

    assert plan.links[0].netem == NetemPlan(
        delay_ms=100,
        jitter_ms=10,
        loss_percent=5,
    )
    assert plan.links[1].netem is None


@pytest.mark.parametrize(
    ("qdisc", "expected"),
    [
        (
            {"kind": "tbf", "rate": "10mbit", "burst": "32kb", "latency_ms": 400},
            TbfPlan(rate="10mbit", burst_bytes=32 * 1024, latency_ms=400),
        ),
        (
            {"kind": "fq_codel", "target_ms": 5, "interval_ms": 100},
            FqCodelPlan(target_ms=5, interval_ms=100, limit=10240, ecn=True),
        ),
    ],
)
def test_compile_plan_preserves_link_qdisc(
    bridge_manifest: Manifest, qdisc: dict[str, object], expected: object
) -> None:
    document = bridge_manifest.model_dump(mode="json")
    document["topology"]["links"][0]["qdisc"] = qdisc

    plan = compile_plan(Manifest.model_validate(document))

    assert plan.links[0].qdisc == expected


def test_compile_plan_preserves_immutable_bridge_stp_settings(
    bridge_manifest: Manifest,
) -> None:
    document = bridge_manifest.model_dump(mode="json")
    bridge = document["topology"]["nodes"]["sw1"]["bridge"]
    bridge.update(
        {
            "stp": True,
            "priority": 4096,
            "ports": {
                "swp1": {"path_cost": 10, "priority": 16},
                "swp2": {"path_cost": 100},
            },
        }
    )

    plan = compile_plan(Manifest.model_validate(document))
    sw1 = plan.nodes["sw1"]

    assert sw1.bridge_priority == 4096
    assert sw1.bridge_ports == {
        "swp1": BridgePortPlan(path_cost=10, priority=16),
        "swp2": BridgePortPlan(path_cost=100, priority=None),
    }
    with pytest.raises(TypeError):
        sw1.bridge_ports["swp3"] = BridgePortPlan(10, 16)  # type: ignore[index]


def test_name_override_changes_all_derived_names_without_mutating_manifest(
    bridge_manifest: Manifest,
) -> None:
    original_name = bridge_manifest.name
    original_endpoints = bridge_manifest.topology.links[0].endpoints

    original_plan = compile_plan(bridge_manifest)
    overridden_plan = compile_plan(bridge_manifest, name_override="demo")

    assert overridden_plan.name == "demo"
    assert overridden_plan.nodes["h1"].namespace == namespace_name("demo", "h1")
    assert (
        overridden_plan.links[0].left.temporary_name,
        overridden_plan.links[0].right.temporary_name,
    ) == temporary_veth_names("demo", 0)
    assert overridden_plan.links[0].left.temporary_name != (
        original_plan.links[0].left.temporary_name
    )
    assert bridge_manifest.name == original_name
    assert bridge_manifest.topology.links[0].endpoints == original_endpoints


def test_maximum_valid_name_override_is_accepted(bridge_manifest: Manifest) -> None:
    name_override = "d" * 32

    plan = compile_plan(bridge_manifest, name_override=name_override)

    assert plan.name == name_override
    assert plan.nodes["h1"].namespace == namespace_name(name_override, "h1")


@pytest.mark.parametrize("name_override", ["Demo", "1demo", "demo.lab", "d" * 33])
def test_name_override_is_validated_with_manifest_deployment_pattern(
    bridge_manifest: Manifest, name_override: str
) -> None:
    with pytest.raises(NslabError) as caught:
        compile_plan(bridge_manifest, name_override=name_override)

    error = caught.value
    assert error.code == "DEPLOYMENT_NAME_INVALID"
    assert error.message == f"invalid deployment name: {name_override!r}"
    assert error.details == {"name": name_override}


def test_plan_dataclasses_and_nested_desired_state_are_immutable(
    bridge_manifest: Manifest,
) -> None:
    plan = compile_plan(bridge_manifest)

    for plan_type in (
        RoutePlan,
        BridgePortPlan,
        NetemPlan,
        NodePlan,
        EndpointPlan,
        LinkPlan,
        TopologyPlan,
    ):
        assert is_dataclass(plan_type)
        assert plan_type.__dataclass_params__.frozen is True

    with pytest.raises(FrozenInstanceError):
        plan.name = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        plan.links[0].mtu = 9000  # type: ignore[misc]
    with pytest.raises(TypeError):
        plan.nodes["other"] = plan.nodes["h1"]  # type: ignore[index]
    with pytest.raises(TypeError):
        plan.nodes["h1"].interfaces["eth0"] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        plan.nodes["h1"].sysctls["net.ipv4.ip_forward"] = 0  # type: ignore[index]
    with pytest.raises(TypeError):
        plan.nodes["h1"].interfaces["eth0"][0] = IPv4Interface(  # type: ignore[index]
            "198.51.100.1/24"
        )


def test_mutating_nested_manifest_dicts_after_compile_does_not_change_plan(
    bridge_manifest: Manifest,
) -> None:
    plan = compile_plan(bridge_manifest)
    h1 = bridge_manifest.topology.nodes["h1"]
    assert isinstance(h1, LinuxNode)

    h1.interfaces["eth0"] = InterfaceConfig(addresses=(IPv4Interface("198.51.100.1/24"),))
    h1.sysctls["net.ipv4.ip_forward"] = 0
    bridge_manifest.topology.nodes["extra"] = h1

    assert tuple(plan.nodes) == ("h1", "sw1", "h2")
    assert plan.nodes["h1"].interfaces == {"eth0": (IPv4Interface("10.10.0.1/24"),)}
    assert plan.nodes["h1"].sysctls == {"net.ipv4.ip_forward": 1}
