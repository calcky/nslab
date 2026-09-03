from __future__ import annotations

import copy
import socket
from dataclasses import replace
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call

import pytest
from pydantic import ValidationError

from nslab.backend.base import inventory_matches_plan, neighbors_match
from nslab.backend.fake import FakeNetworkBackend
from nslab.backend.pyroute2 import Pyroute2Backend
from nslab.inspector import inspect_topology
from nslab.lifecycle import LifecycleService
from nslab.manifest import Manifest, normalized_manifest
from nslab.planner import NeighborPlan, compile_plan
from nslab.state import StateStore


def _neighbors_document() -> dict[str, Any]:
    return {
        "version": 1,
        "name": "neighbors",
        "topology": {
            "nodes": {
                "h1": {
                    "kind": "linux",
                    "interfaces": {
                        "eth0": {
                            "addresses": ["192.0.2.1/24", "2001:db8:1::1/64"],
                            "mac": "02:00:00:00:01:0A",
                        }
                    },
                    "neighbors": [
                        {
                            "dst": "192.0.2.2",
                            "dev": "eth0",
                            "lladdr": "02:00:00:00:02:0A",
                        },
                        {
                            "dst": "2001:db8:1::2",
                            "dev": "eth0",
                            "lladdr": "02:00:00:00:02:0A",
                            "state": "reachable",
                        },
                        {"dst": "192.0.2.200", "dev": "eth0", "proxy": True},
                        {"dst": "2001:db8:1::200", "dev": "eth0", "proxy": True},
                    ],
                },
                "h2": {
                    "kind": "linux",
                    "interfaces": {
                        "eth0": {
                            "addresses": ["192.0.2.2/24", "2001:db8:1::2/64"],
                            "mac": "02:00:00:00:02:0a",
                        }
                    },
                },
            },
            "links": [{"endpoints": ["h1:eth0", "h2:eth0"]}],
        },
    }


def _neighbor_message(
    family: int,
    dst: str,
    *,
    state: int,
    lladdr: str | None = None,
    proxy: bool = False,
) -> dict[str, object]:
    attributes: list[tuple[str, object]] = [("NDA_DST", dst)]
    if lladdr is not None:
        attributes.append(("NDA_LLADDR", lladdr))
    return {
        "family": family,
        "ifindex": 10,
        "flags": 0x08 if proxy else 0,
        "state": state,
        "ndm_type": 1,
        "attrs": attributes,
    }


def test_manifest_plan_and_normalization_preserve_macs_and_neighbors() -> None:
    manifest = Manifest.model_validate(_neighbors_document())
    h1 = manifest.topology.nodes["h1"]

    assert h1.interfaces["eth0"].mac == "02:00:00:00:01:0a"
    assert h1.neighbors[0].lladdr == "02:00:00:00:02:0a"
    assert h1.neighbors[0].state is None

    plan = compile_plan(manifest)
    planned = plan.nodes["h1"]
    assert planned.mac_addresses == {"eth0": "02:00:00:00:01:0a"}
    assert planned.neighbors == (
        NeighborPlan(
            dst=IPv4Address("192.0.2.2"),
            dev="eth0",
            lladdr="02:00:00:00:02:0a",
            state="permanent",
        ),
        NeighborPlan(
            dst=IPv6Address("2001:db8:1::2"),
            dev="eth0",
            lladdr="02:00:00:00:02:0a",
            state="reachable",
        ),
        NeighborPlan(
            dst=IPv4Address("192.0.2.200"),
            dev="eth0",
            lladdr=None,
            state=None,
            proxy=True,
        ),
        NeighborPlan(
            dst=IPv6Address("2001:db8:1::200"),
            dev="eth0",
            lladdr=None,
            state=None,
            proxy=True,
        ),
    )
    assert planned.sysctls == {
        "net.ipv4.conf.eth0.proxy_arp": 1,
        "net.ipv6.conf.eth0.proxy_ndp": 1,
    }

    node_document = normalized_manifest(manifest)["topology"]["nodes"]["h1"]
    assert node_document["interfaces"]["eth0"]["mac"] == "02:00:00:00:01:0a"
    assert node_document["neighbors"] == [
        {"dst": "192.0.2.2", "dev": "eth0", "lladdr": "02:00:00:00:02:0a"},
        {
            "dst": "2001:db8:1::2",
            "dev": "eth0",
            "lladdr": "02:00:00:00:02:0a",
            "state": "reachable",
        },
        {"dst": "192.0.2.200", "dev": "eth0", "proxy": True},
        {"dst": "2001:db8:1::200", "dev": "eth0", "proxy": True},
    ]


@pytest.mark.parametrize(
    "mac",
    [
        "00:00:00:00:00:00",
        "01:00:5e:00:00:01",
        "ff:ff:ff:ff:ff:ff",
        "02-00-00-00-00-01",
        "02:00:00:00:00",
        True,
    ],
)
def test_manifest_rejects_invalid_interface_and_neighbor_macs(mac: object) -> None:
    interface_document = _neighbors_document()
    interface_document["topology"]["nodes"]["h1"]["interfaces"]["eth0"]["mac"] = mac
    with pytest.raises(ValidationError):
        Manifest.model_validate(interface_document)

    neighbor_document = _neighbors_document()
    neighbor_document["topology"]["nodes"]["h1"]["neighbors"][0]["lladdr"] = mac
    with pytest.raises(ValidationError):
        Manifest.model_validate(neighbor_document)


@pytest.mark.parametrize(
    ("neighbor", "message"),
    [
        ({"dst": "192.0.2.9", "dev": "eth0"}, "non-proxy neighbor requires lladdr"),
        (
            {
                "dst": "192.0.2.9",
                "dev": "eth0",
                "lladdr": "02:00:00:00:00:09",
                "proxy": True,
            },
            "proxy neighbor cannot declare lladdr or state",
        ),
        (
            {"dst": "192.0.2.9", "dev": "eth0", "state": "stale", "proxy": True},
            "proxy neighbor cannot declare lladdr or state",
        ),
        (
            {
                "dst": "192.0.2.9",
                "dev": "eth0",
                "lladdr": "02:00:00:00:00:09",
                "state": "failed",
            },
            "Input should be",
        ),
        (
            {
                "dst": "224.0.0.1",
                "dev": "eth0",
                "lladdr": "02:00:00:00:00:09",
            },
            "neighbor destination must be unicast",
        ),
        (
            {
                "dst": "192.0.2.9",
                "dev": "missing0",
                "lladdr": "02:00:00:00:00:09",
            },
            "neighbor device is not linked",
        ),
    ],
)
def test_manifest_rejects_invalid_neighbor_shapes(
    neighbor: dict[str, object],
    message: str,
) -> None:
    document = _neighbors_document()
    document["topology"]["nodes"]["h1"]["neighbors"] = [neighbor]

    with pytest.raises(ValidationError, match=message):
        Manifest.model_validate(document)


def test_manifest_rejects_duplicate_neighbor_destination_and_device() -> None:
    document = _neighbors_document()
    duplicate = copy.deepcopy(document["topology"]["nodes"]["h1"]["neighbors"][0])
    document["topology"]["nodes"]["h1"]["neighbors"].append(duplicate)

    with pytest.raises(ValidationError, match="pairs must be unique"):
        Manifest.model_validate(document)


@pytest.mark.parametrize(
    "device",
    [
        {
            "type": "gre",
            "link": "eth0",
            "local": "192.0.2.1",
            "remote": "192.0.2.2",
            "mac": "02:00:00:00:00:10",
        },
        {
            "type": "ipip",
            "link": "eth0",
            "local": "192.0.2.1",
            "remote": "192.0.2.2",
            "mac": "02:00:00:00:00:10",
        },
        {
            "type": "ipvlan",
            "link": "eth0",
            "mac": "02:00:00:00:00:10",
        },
    ],
)
def test_manifest_rejects_mac_on_devices_without_configurable_mac(
    device: dict[str, object],
) -> None:
    document = _neighbors_document()
    document["topology"]["nodes"]["h1"]["devices"] = {"dev0": device}

    with pytest.raises(ValidationError, match="device type does not support a MAC address"):
        Manifest.model_validate(document)


def test_pyroute2_configures_mac_regular_neighbors_and_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = compile_plan(Manifest.model_validate(_neighbors_document()))
    node = plan.nodes["h1"]
    handle = Mock()
    handle.link_lookup.return_value = [10]
    backend = Pyroute2Backend(netns_factory=Mock(return_value=handle))
    write_sysctls = Mock()
    monkeypatch.setattr(backend, "_write_sysctls", write_sysctls)

    backend.configure_node(node, plan)

    assert call.link("set", index=10, address="02:00:00:00:01:0a") in handle.mock_calls
    assert (
        call.neigh(
            "add",
            family=socket.AF_INET,
            dst="192.0.2.2",
            ifindex=10,
            lladdr="02:00:00:00:02:0a",
            state=0x80,
        )
        in handle.mock_calls
    )
    assert (
        call.neigh(
            "add",
            family=socket.AF_INET6,
            dst="2001:db8:1::2",
            ifindex=10,
            lladdr="02:00:00:00:02:0a",
            state=0x02,
        )
        in handle.mock_calls
    )
    assert (
        call.neigh(
            "add",
            family=socket.AF_INET,
            dst="192.0.2.200",
            ifindex=10,
            flags=0x08,
            state=0,
        )
        in handle.mock_calls
    )
    assert (
        call.neigh(
            "add",
            family=socket.AF_INET6,
            dst="2001:db8:1::200",
            ifindex=10,
            flags=0x08,
            state=0,
        )
        in handle.mock_calls
    )
    write_sysctls.assert_called_once_with(node)


def test_pyroute2_inventory_decodes_declared_neighbors_and_ignores_learned_entries() -> None:
    node = compile_plan(Manifest.model_validate(_neighbors_document())).nodes["h1"]
    messages = (
        _neighbor_message(
            socket.AF_INET,
            "192.0.2.2",
            state=0x80,
            lladdr="02:00:00:00:02:0A",
        ),
        _neighbor_message(
            socket.AF_INET6,
            "2001:db8:1::2",
            state=0x04,
            lladdr="02:00:00:00:02:0A",
        ),
        _neighbor_message(socket.AF_INET, "192.0.2.200", state=0, proxy=True),
        _neighbor_message(socket.AF_INET6, "2001:db8:1::200", state=0, proxy=True),
        _neighbor_message(
            socket.AF_INET,
            "192.0.2.99",
            state=0x02,
            lladdr="02:00:00:00:00:99",
        ),
    )

    observed = Pyroute2Backend._inventory_neighbors(
        messages,
        {10: "eth0"},
        node.neighbors,
        node.namespace,
    )

    assert len(observed) == 4
    assert observed[0].state == "permanent"
    assert observed[1].state == "stale"
    assert observed[1].lladdr == "02:00:00:00:02:0a"
    assert observed[2].proxy is True
    assert observed[3].proxy is True
    assert neighbors_match(node.neighbors, observed)


@pytest.mark.parametrize("observed_state", ["reachable", "stale", "delay", "probe"])
def test_neighbor_matching_accepts_healthy_nud_transitions(observed_state: str) -> None:
    desired = NeighborPlan(
        dst=IPv4Address("192.0.2.2"),
        dev="eth0",
        lladdr="02:00:00:00:02:0a",
        state="reachable",
    )
    observed = replace(desired, state=observed_state)

    assert neighbors_match((desired,), (observed,))
    assert not neighbors_match((desired,), (replace(observed, state="failed"),))


def test_fake_lifecycle_and_inspector_detect_mac_and_neighbor_drift(tmp_path: Path) -> None:
    manifest = Manifest.model_validate(_neighbors_document())
    plan = compile_plan(manifest)
    backend = FakeNetworkBackend()
    store = StateStore(tmp_path / "state")
    service = LifecycleService(backend, store)

    assert service.deploy(plan, manifest).changed is True
    assert service.deploy(plan, manifest).changed is False
    inventory = backend.inventory(plan)
    assert inventory_matches_plan(plan, inventory)

    h1 = plan.nodes["h1"]
    namespace = inventory.namespaces[h1.namespace]
    interfaces = dict(namespace.interfaces)
    interfaces["eth0"] = replace(interfaces["eth0"], mac="02:00:00:00:01:ff")
    drifted = replace(
        inventory,
        namespaces={
            **inventory.namespaces,
            h1.namespace: replace(
                namespace,
                interfaces=interfaces,
                neighbors=namespace.neighbors[:-1],
            ),
        },
    )

    assert not inventory_matches_plan(plan, drifted)
    report = inspect_topology(plan, store.load(plan.name), drifted)
    assert report.status == "degraded"
    assert any(
        difference.scope == "interface" and difference.property == "mac" and difference.node == "h1"
        for difference in report.differences
    )
    assert any(
        difference.scope == "node"
        and difference.property == "neighbors"
        and difference.node == "h1"
        for difference in report.differences
    )

    desired = report.nodes[0].desired.to_dict()
    assert desired["interfaces"][1]["mac"] == "02:00:00:00:01:0a"
    assert desired["neighbors"][0] == {
        "dst": "192.0.2.2",
        "dev": "eth0",
        "lladdr": "02:00:00:00:02:0a",
        "state": "permanent",
        "proxy": False,
    }
    assert service.destroy(plan, plan.name).status == "absent"


def test_lifecycle_absence_check_treats_neighbors_as_live_resources() -> None:
    plan = compile_plan(Manifest.model_validate(_neighbors_document()))
    backend = FakeNetworkBackend()
    inventory = backend.inventory(plan)
    h1 = plan.nodes["h1"]
    namespace = inventory.namespaces[h1.namespace]
    lingering = replace(
        inventory,
        namespaces={
            **inventory.namespaces,
            h1.namespace: replace(namespace, neighbors=(h1.neighbors[0],)),
        },
    )

    assert LifecycleService._inventory_is_absent(plan, inventory)
    assert not LifecycleService._inventory_is_absent(plan, lingering)
