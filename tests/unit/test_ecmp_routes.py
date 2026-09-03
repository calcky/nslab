from __future__ import annotations

import json
import socket
from dataclasses import replace
from ipaddress import IPv4Address, IPv4Interface, IPv4Network, IPv6Address, IPv6Network
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call

import pytest
from pydantic import ValidationError

from nslab.backend.base import InterfaceInventory, inventory_matches_plan
from nslab.backend.fake import FakeNetworkBackend
from nslab.backend.pyroute2 import Pyroute2Backend
from nslab.errors import NslabError
from nslab.inspector import inspect_topology
from nslab.lifecycle import LifecycleService
from nslab.manifest import Manifest, RouteConfig, RouteNextHopConfig, normalized_manifest
from nslab.planner import RouteNextHopPlan, RoutePlan, compile_plan
from nslab.state import StateStore


def _ecmp_document() -> dict[str, Any]:
    return {
        "version": 1,
        "name": "ecmp",
        "topology": {
            "nodes": {
                "r1": {
                    "kind": "linux",
                    "interfaces": {
                        "eth0": {"addresses": ["10.0.12.1/30"]},
                        "eth1": {"addresses": ["10.0.13.1/30"]},
                    },
                    "routes": [
                        {
                            "dst": "192.0.2.0/24",
                            "nexthops": [
                                {"via": "10.0.12.2", "dev": "eth0", "weight": 1},
                                {"via": "10.0.13.2", "dev": "eth1", "weight": 2},
                            ],
                        }
                    ],
                },
                "r2": {
                    "kind": "linux",
                    "interfaces": {"eth0": {"addresses": ["10.0.12.2/30"]}},
                },
                "r3": {
                    "kind": "linux",
                    "interfaces": {"eth0": {"addresses": ["10.0.13.2/30"]}},
                },
            },
            "links": [
                {"endpoints": ["r1:eth0", "r2:eth0"]},
                {"endpoints": ["r1:eth1", "r3:eth0"]},
            ],
        },
    }


def test_ecmp_manifest_plan_and_normalization_preserve_nexthops() -> None:
    manifest = Manifest.model_validate(_ecmp_document())
    route = manifest.topology.nodes["r1"].routes[0]

    assert route.via is None
    assert route.dev is None
    assert route.nexthops == (
        RouteNextHopConfig(via=IPv4Address("10.0.12.2"), dev="eth0", weight=1),
        RouteNextHopConfig(via=IPv4Address("10.0.13.2"), dev="eth1", weight=2),
    )

    plan = compile_plan(manifest)
    assert plan.nodes["r1"].routes == (
        RoutePlan(
            dst=IPv4Network("192.0.2.0/24"),
            via=None,
            dev=None,
            nexthops=(
                RouteNextHopPlan(IPv4Address("10.0.12.2"), "eth0", 1),
                RouteNextHopPlan(IPv4Address("10.0.13.2"), "eth1", 2),
            ),
        ),
    )

    route_document = normalized_manifest(manifest)["topology"]["nodes"]["r1"]["routes"][0]
    assert route_document == {
        "dst": "192.0.2.0/24",
        "nexthops": [
            {"via": "10.0.12.2", "dev": "eth0", "weight": 1},
            {"via": "10.0.13.2", "dev": "eth1", "weight": 2},
        ],
    }
    json.dumps(normalized_manifest(manifest))


def test_legacy_single_path_route_normalization_does_not_add_nexthops() -> None:
    document = _ecmp_document()
    document["topology"]["nodes"]["r1"]["routes"] = [
        {"dst": "192.0.2.0/24", "via": "10.0.12.2", "dev": "eth0"}
    ]

    manifest = Manifest.model_validate(document)

    assert normalized_manifest(manifest)["topology"]["nodes"]["r1"]["routes"][0] == {
        "dst": "192.0.2.0/24",
        "via": "10.0.12.2",
        "dev": "eth0",
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda route: route.update(dev="eth0"),
            "route cannot combine via/dev with nexthops",
        ),
        (
            lambda route: route.update(via="10.0.12.2"),
            "route cannot combine via/dev with nexthops",
        ),
        (
            lambda route: route.update(nexthops=route["nexthops"][:1]),
            "multipath route requires at least two nexthops",
        ),
        (
            lambda route: route.update(nexthops=[route["nexthops"][0]] * 2),
            "multipath route nexthops must be unique",
        ),
        (
            lambda route: route["nexthops"][0].update(via="2001:db8::1"),
            "route destination and nexthop gateways must use the same address family",
        ),
        (
            lambda route: route["nexthops"][0].update(dev="missing0"),
            "route device is not linked",
        ),
    ],
)
def test_ecmp_manifest_rejects_invalid_route_shapes(
    mutation: Any,
    message: str,
) -> None:
    document = _ecmp_document()
    route = document["topology"]["nodes"]["r1"]["routes"][0]
    mutation(route)

    with pytest.raises(ValidationError, match=message):
        Manifest.model_validate(document)


@pytest.mark.parametrize("weight", [0, 257, True, 1.5, "2"])
def test_ecmp_manifest_rejects_invalid_weights(weight: object) -> None:
    document = _ecmp_document()
    document["topology"]["nodes"]["r1"]["routes"][0]["nexthops"][0]["weight"] = weight

    with pytest.raises(ValidationError):
        Manifest.model_validate(document)


def test_route_requires_direct_device_or_multipath_nexthops() -> None:
    with pytest.raises(ValidationError, match="route requires dev or nexthops"):
        RouteConfig(dst="192.0.2.0/24")


def test_ecmp_supports_ipv6_gateways() -> None:
    route = RouteConfig(
        dst="2001:db8:3::/64",
        nexthops=(
            RouteNextHopConfig(via="2001:db8:1::2", dev="eth0"),
            RouteNextHopConfig(via="2001:db8:2::2", dev="eth1"),
        ),
    )

    assert route.dst == IPv6Network("2001:db8:3::/64")
    assert route.nexthops[0].via == IPv6Address("2001:db8:1::2")


def test_ecmp_infers_one_vrf_table_and_rejects_mixed_routing_domains() -> None:
    document = _ecmp_document()
    r1 = document["topology"]["nodes"]["r1"]
    r1["devices"] = {"blue": {"type": "vrf", "table": 1001, "interfaces": ["eth0", "eth1"]}}

    plan = compile_plan(Manifest.model_validate(document))
    assert plan.nodes["r1"].routes[0].table == 1001

    r1["devices"]["blue"]["interfaces"] = ["eth0"]
    with pytest.raises(ValidationError, match="multipath route spans routing tables"):
        Manifest.model_validate(document)


def test_pyroute2_configures_weighted_ecmp_route() -> None:
    plan = compile_plan(Manifest.model_validate(_ecmp_document()))
    node = plan.nodes["r1"]
    handle = Mock()
    handle.link_lookup.side_effect = lambda *, ifname: [{"eth0": 10, "eth1": 11}[ifname]]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))

    backend.configure_node(node, plan)

    assert (
        call.route(
            "add",
            dst="192.0.2.0/24",
            multipath=[
                {"gateway": "10.0.12.2", "oif": 10, "hops": 0},
                {"gateway": "10.0.13.2", "oif": 11, "hops": 1},
            ],
        )
        in handle.mock_calls
    )


def _interface(name: str, index: int, address: str) -> InterfaceInventory:
    return InterfaceInventory(
        name=name,
        kind="veth",
        ifindex=index,
        master=None,
        mtu=1500,
        up=True,
        addresses=(IPv4Interface(address),),
    )


def _multipath_message(*, family: int = socket.AF_INET) -> dict[str, object]:
    destination = "2001:db8:3::" if family == socket.AF_INET6 else "192.0.2.0"
    gateways = (
        ("2001:db8:1::2", "2001:db8:2::2")
        if family == socket.AF_INET6
        else ("10.0.12.2", "10.0.13.2")
    )
    attributes: list[tuple[str, object]] = [
        ("RTA_DST", destination),
        (
            "RTA_MULTIPATH",
            (
                {
                    "flags": 0,
                    "hops": 0,
                    "oif": 10,
                    "attrs": [("RTA_GATEWAY", gateways[0])],
                },
                {
                    "flags": 0,
                    "hops": 1,
                    "oif": 11,
                    "attrs": [("RTA_GATEWAY", gateways[1])],
                },
            ),
        ),
    ]
    if family == socket.AF_INET6:
        attributes.extend((("RTA_PRIORITY", 1024), ("RTA_PREF", 0)))
    return {
        "table": 254,
        "type": 1,
        "dst_len": 64 if family == socket.AF_INET6 else 24,
        "src_len": 0,
        "tos": 0,
        "flags": 0,
        "proto": 4,
        "scope": 0,
        "attrs": attributes,
    }


@pytest.mark.parametrize(
    ("family", "destination", "gateways"),
    [
        (
            socket.AF_INET,
            IPv4Network("192.0.2.0/24"),
            (IPv4Address("10.0.12.2"), IPv4Address("10.0.13.2")),
        ),
        (
            socket.AF_INET6,
            IPv6Network("2001:db8:3::/64"),
            (IPv6Address("2001:db8:1::2"), IPv6Address("2001:db8:2::2")),
        ),
    ],
)
def test_pyroute2_inventory_decodes_multipath_route(
    family: int,
    destination: object,
    gateways: tuple[object, object],
) -> None:
    interfaces = {
        "eth0": _interface("eth0", 10, "10.0.12.1/30"),
        "eth1": _interface("eth1", 11, "10.0.13.1/30"),
    }

    routes = Pyroute2Backend._inventory_routes(
        (_multipath_message(family=family),),
        {10: "eth0", 11: "eth1"},
        interfaces,
        "nslab-ecmp-r1",
        family=family,
    )

    assert routes == (
        RoutePlan(
            dst=destination,
            via=None,
            dev=None,
            nexthops=(
                RouteNextHopPlan(gateways[0], "eth0", 1),
                RouteNextHopPlan(gateways[1], "eth1", 2),
            ),
        ),
    )


def test_pyroute2_inventory_decodes_dynamic_ospf_multipath_route() -> None:
    message = _multipath_message()
    message["proto"] = 188
    attributes = message["attrs"]
    assert isinstance(attributes, list)
    attributes.append(("RTA_PRIORITY", 20))
    interfaces = {
        "eth0": _interface("eth0", 10, "10.0.12.1/30"),
        "eth1": _interface("eth1", 11, "10.0.13.1/30"),
    }

    routes = Pyroute2Backend._inventory_routes(
        (message,),
        {10: "eth0", 11: "eth1"},
        interfaces,
        "nslab-ospf-r1",
        allow_dynamic=True,
    )

    assert routes == (
        RoutePlan(
            dst=IPv4Network("192.0.2.0/24"),
            via=None,
            dev=None,
            nexthops=(
                RouteNextHopPlan(IPv4Address("10.0.12.2"), "eth0", 1),
                RouteNextHopPlan(IPv4Address("10.0.13.2"), "eth1", 2),
            ),
        ),
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda item: item.update(flags=1), "multipath_nexthop_flags"),
        (lambda item: item.update(hops=256), "multipath_nexthop_weight"),
        (
            lambda item: item["attrs"].append(("RTA_ENCAP", object())),
            "multipath_nexthop_attribute",
        ),
        (lambda item: item.update(oif=999), "unknown_ifindex"),
    ],
)
def test_pyroute2_inventory_rejects_lossy_multipath_nexthops(
    mutation: Any,
    reason: str,
) -> None:
    message = _multipath_message()
    multipath = dict(message["attrs"])["RTA_MULTIPATH"]
    mutation(multipath[0])

    with pytest.raises(NslabError) as caught:
        Pyroute2Backend._inventory_routes(
            (message,),
            {10: "eth0", 11: "eth1"},
            {},
            "nslab-ecmp-r1",
        )

    assert caught.value.details["reason"] == reason


def test_fake_lifecycle_and_inspect_detect_ecmp_weight_drift(tmp_path: Path) -> None:
    manifest = Manifest.model_validate(_ecmp_document())
    plan = compile_plan(manifest)
    backend = FakeNetworkBackend()
    store = StateStore(tmp_path / "state")
    service = LifecycleService(backend, store)
    service.deploy(plan, manifest)

    inventory = backend.inventory(plan)
    assert inventory_matches_plan(plan, inventory)
    report = inspect_topology(plan, store.load(plan.name), inventory)
    route_document = report.to_dict()["nodes"][0]["desired"]["routes"][-1]
    assert route_document == {
        "dst": "192.0.2.0/24",
        "nexthops": [
            {"via": "10.0.12.2", "dev": "eth0", "weight": 1},
            {"via": "10.0.13.2", "dev": "eth1", "weight": 2},
        ],
        "table": 254,
    }

    namespace = inventory.namespaces[plan.nodes["r1"].namespace]
    route = namespace.routes[-1]
    drifted_route = replace(
        route,
        nexthops=(route.nexthops[0], replace(route.nexthops[1], weight=1)),
    )
    drifted = replace(
        inventory,
        namespaces={
            **inventory.namespaces,
            namespace.namespace: replace(
                namespace,
                routes=(*namespace.routes[:-1], drifted_route),
            ),
        },
    )

    assert not inventory_matches_plan(plan, drifted)
    degraded = inspect_topology(plan, store.load(plan.name), drifted)
    assert degraded.status == "degraded"
    assert any(difference.property == "routes" for difference in degraded.differences)
