from __future__ import annotations

import copy
import json
from collections.abc import Callable
from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv4Network,
    IPv6Address,
    IPv6Interface,
    IPv6Network,
)
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from nslab.errors import NslabError
from nslab.manifest import (
    BridgeNode,
    InterfaceConfig,
    LinkConfig,
    LinuxNode,
    Manifest,
    RouteConfig,
    Topology,
    load_manifest,
    manifest_fingerprint,
    normalized_manifest,
)

ManifestData = dict[str, Any]
Mutation = Callable[[ManifestData], None]


@pytest.fixture
def manifest_data() -> ManifestData:
    return {
        "version": 1,
        "name": "bridge-fdb",
        "topology": {
            "nodes": {
                "h1": {
                    "kind": "linux",
                    "interfaces": {"eth0": {"addresses": ["10.10.0.1/24"]}},
                },
                "sw1": {
                    "kind": "bridge",
                    "bridge": {
                        "name": "br0",
                        "stp": False,
                        "vlan_filtering": False,
                    },
                },
                "h2": {
                    "kind": "linux",
                    "interfaces": {"eth0": {"addresses": ["10.10.0.2/24"]}},
                },
            },
            "links": [
                {"endpoints": ["h1:eth0", "sw1:swp1"], "mtu": 1500},
                {"endpoints": ["h2:eth0", "sw1:swp2"], "mtu": 1500},
            ],
        },
    }


def _write_manifest(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "nslab.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _assert_invalid(tmp_path: Path, data: object) -> NslabError:
    path = _write_manifest(tmp_path, data)
    with pytest.raises(NslabError) as caught:
        load_manifest(path)

    error = caught.value
    assert error.code == "MANIFEST_INVALID"
    assert error.details["path"] == str(path.resolve())
    assert error.details["issues"]
    return error


def _model_validation_issue(data: ManifestData) -> dict[str, Any]:
    with pytest.raises(ValidationError) as caught:
        Manifest.model_validate(data)
    issues = caught.value.errors(include_url=False, include_input=False)
    assert len(issues) == 1
    return issues[0]


def test_loads_bridge_fdb_manifest_and_types_values(
    tmp_path: Path, manifest_data: ManifestData
) -> None:
    path = _write_manifest(tmp_path, manifest_data)

    manifest = load_manifest(path)

    assert manifest.version == 1
    assert manifest.name == "bridge-fdb"
    assert isinstance(manifest, Manifest)
    assert isinstance(manifest.topology, Topology)
    assert isinstance(manifest.topology.nodes["h1"], LinuxNode)
    assert manifest.topology.nodes["h1"].kind == "linux"
    assert isinstance(manifest.topology.nodes["sw1"], BridgeNode)
    assert manifest.topology.nodes["sw1"].kind == "bridge"
    assert manifest.topology.links[0].endpoints == ("h1:eth0", "sw1:swp1")
    assert manifest.topology.links[0].mtu == 1500
    assert isinstance(manifest.topology.links[0], LinkConfig)

    h1 = manifest.topology.nodes["h1"]
    assert isinstance(h1, LinuxNode)
    assert isinstance(h1.interfaces["eth0"], InterfaceConfig)
    assert h1.interfaces["eth0"].addresses == (IPv4Interface("10.10.0.1/24"),)

    sw1 = manifest.topology.nodes["sw1"]
    assert isinstance(sw1, BridgeNode)
    assert sw1.bridge.name == "br0"
    assert sw1.bridge.stp is False
    assert sw1.bridge.vlan_filtering is False
    assert sw1.bridge.priority is None
    assert sw1.bridge.ports == {}

    assert manifest_fingerprint(manifest) == manifest_fingerprint(load_manifest(path))


def test_loads_explicit_bridge_and_port_stp_settings(
    tmp_path: Path, manifest_data: ManifestData
) -> None:
    bridge = manifest_data["topology"]["nodes"]["sw1"]["bridge"]
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

    manifest = load_manifest(_write_manifest(tmp_path, manifest_data))
    sw1 = manifest.topology.nodes["sw1"]
    assert isinstance(sw1, BridgeNode)
    assert sw1.bridge.priority == 4096
    assert sw1.bridge.ports["swp1"].path_cost == 10
    assert sw1.bridge.ports["swp1"].priority == 16
    assert sw1.bridge.ports["swp2"].path_cost == 100
    assert sw1.bridge.ports["swp2"].priority is None

    normalized = normalized_manifest(manifest)
    normalized_bridge = normalized["topology"]["nodes"]["sw1"]["bridge"]
    assert normalized_bridge["priority"] == 4096
    assert normalized_bridge["ports"] == {
        "swp1": {"path_cost": 10, "priority": 16},
        "swp2": {"path_cost": 100},
    }


def test_loads_bridge_port_vlan_settings(tmp_path: Path, manifest_data: ManifestData) -> None:
    bridge = manifest_data["topology"]["nodes"]["sw1"]["bridge"]
    bridge.update(
        {
            "vlan_filtering": True,
            "ports": {
                "swp1": {
                    "vlans": [
                        {"vid": 10, "pvid": True, "untagged": True},
                        {"vid": 20},
                    ]
                }
            },
        }
    )

    manifest = load_manifest(_write_manifest(tmp_path, manifest_data))
    sw1 = manifest.topology.nodes["sw1"]
    assert isinstance(sw1, BridgeNode)
    assert tuple(vlan.vid for vlan in sw1.bridge.ports["swp1"].vlans) == (10, 20)
    assert sw1.bridge.ports["swp1"].vlans[0].pvid is True
    assert sw1.bridge.ports["swp1"].vlans[0].untagged is True
    assert sw1.bridge.ports["swp1"].vlans[1].pvid is False

    normalized = normalized_manifest(manifest)
    normalized_port = normalized["topology"]["nodes"]["sw1"]["bridge"]["ports"]["swp1"]
    assert normalized_port == {
        "vlans": [
            {"vid": 10, "pvid": True, "untagged": True},
            {"vid": 20, "pvid": False, "untagged": False},
        ]
    }


def test_loads_bridge_port_forwarding_controls(tmp_path: Path, manifest_data: ManifestData) -> None:
    bridge = manifest_data["topology"]["nodes"]["sw1"]["bridge"]
    bridge["ports"] = {
        "swp1": {
            "hairpin": True,
            "isolated": True,
            "learning": False,
            "flood": False,
            "multicast_flood": False,
        }
    }

    manifest = load_manifest(_write_manifest(tmp_path, manifest_data))
    sw1 = manifest.topology.nodes["sw1"]
    assert isinstance(sw1, BridgeNode)
    port = sw1.bridge.ports["swp1"]
    assert port.hairpin is True
    assert port.isolated is True
    assert port.learning is False
    assert port.flood is False
    assert port.multicast_flood is False

    normalized = normalized_manifest(manifest)
    assert normalized["topology"]["nodes"]["sw1"]["bridge"]["ports"]["swp1"] == {
        "hairpin": True,
        "isolated": True,
        "learning": False,
        "flood": False,
        "multicast_flood": False,
    }


@pytest.mark.parametrize(
    "field",
    ["hairpin", "isolated", "learning", "flood", "multicast_flood"],
)
@pytest.mark.parametrize("value", [0, 1, "true", "false"])
def test_rejects_non_boolean_bridge_port_forwarding_controls(
    tmp_path: Path,
    manifest_data: ManifestData,
    field: str,
    value: object,
) -> None:
    bridge = manifest_data["topology"]["nodes"]["sw1"]["bridge"]
    bridge["ports"] = {"swp1": {field: value}}

    _assert_invalid(tmp_path, manifest_data)


@pytest.mark.parametrize(
    "vlans",
    [
        [{"vid": 0}],
        [{"vid": 4095}],
        [{"vid": 10}, {"vid": 10}],
        [{"vid": 10, "pvid": True}, {"vid": 20, "pvid": True}],
    ],
)
def test_rejects_invalid_bridge_port_vlans(
    tmp_path: Path, manifest_data: ManifestData, vlans: object
) -> None:
    bridge = manifest_data["topology"]["nodes"]["sw1"]["bridge"]
    bridge["vlan_filtering"] = True
    bridge["ports"] = {"swp1": {"vlans": vlans}}

    _assert_invalid(tmp_path, manifest_data)


def test_rejects_bridge_vlans_when_filtering_is_disabled(
    tmp_path: Path, manifest_data: ManifestData
) -> None:
    bridge = manifest_data["topology"]["nodes"]["sw1"]["bridge"]
    bridge["ports"] = {"swp1": {"vlans": [{"vid": 10}]}}

    error = _assert_invalid(tmp_path, manifest_data)
    assert "require vlan_filtering: true" in json.dumps(error.details["issues"])


@pytest.mark.parametrize("priority", [-1, 65536, True, 1.5])
def test_rejects_invalid_bridge_priority(
    tmp_path: Path, manifest_data: ManifestData, priority: object
) -> None:
    manifest_data["topology"]["nodes"]["sw1"]["bridge"]["priority"] = priority

    _assert_invalid(tmp_path, manifest_data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path_cost", 0),
        ("path_cost", 65536),
        ("path_cost", True),
        ("priority", -1),
        ("priority", 64),
        ("priority", True),
    ],
)
def test_rejects_invalid_bridge_port_stp_setting(
    tmp_path: Path,
    manifest_data: ManifestData,
    field: str,
    value: object,
) -> None:
    bridge = manifest_data["topology"]["nodes"]["sw1"]["bridge"]
    bridge["stp"] = True
    bridge["ports"] = {"swp1": {field: value}}

    _assert_invalid(tmp_path, manifest_data)


def test_rejects_empty_bridge_port_settings(tmp_path: Path, manifest_data: ManifestData) -> None:
    bridge = manifest_data["topology"]["nodes"]["sw1"]["bridge"]
    bridge["stp"] = True
    bridge["ports"] = {"swp1": {}}

    _assert_invalid(tmp_path, manifest_data)


def test_rejects_bridge_port_settings_when_stp_is_disabled(
    tmp_path: Path, manifest_data: ManifestData
) -> None:
    bridge = manifest_data["topology"]["nodes"]["sw1"]["bridge"]
    bridge["ports"] = {"swp1": {"path_cost": 10}}

    error = _assert_invalid(tmp_path, manifest_data)
    assert "require stp: true" in json.dumps(error.details["issues"])


def test_rejects_stp_settings_for_unlinked_bridge_port(
    tmp_path: Path, manifest_data: ManifestData
) -> None:
    bridge = manifest_data["topology"]["nodes"]["sw1"]["bridge"]
    bridge["stp"] = True
    bridge["ports"] = {"swp9": {"path_cost": 10}}

    error = _assert_invalid(tmp_path, manifest_data)
    assert "swp9" in json.dumps(error.details["issues"])


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update({"unexpected": True}),
        lambda data: data["topology"]["nodes"]["h1"]["interfaces"]["eth0"].update(
            {"unexpected": True}
        ),
    ],
    ids=["top-level", "nested"],
)
def test_rejects_unknown_fields(
    tmp_path: Path, manifest_data: ManifestData, mutation: Mutation
) -> None:
    mutation(manifest_data)
    _assert_invalid(tmp_path, manifest_data)


def test_rejects_unsupported_version(tmp_path: Path, manifest_data: ManifestData) -> None:
    manifest_data["version"] = 2
    _assert_invalid(tmp_path, manifest_data)


@pytest.mark.parametrize("version", [True, 1.0])
def test_rejects_non_integer_version_one(
    tmp_path: Path, manifest_data: ManifestData, version: object
) -> None:
    manifest_data["version"] = version
    _assert_invalid(tmp_path, manifest_data)


@pytest.mark.parametrize(
    ("target", "invalid_name"),
    [
        ("deployment", "1bridge"),
        ("deployment", "bridge.fdb"),
        ("deployment", "a" * 33),
        ("node", "H1"),
        ("node", "host.1"),
        ("node", "a" * 33),
        ("interface", "bad:interface"),
        ("interface", "interface-name16"),
        ("bridge", "bridge-name-way-too-long"),
    ],
)
def test_rejects_invalid_names(
    tmp_path: Path,
    manifest_data: ManifestData,
    target: str,
    invalid_name: str,
) -> None:
    if target == "deployment":
        manifest_data["name"] = invalid_name
    elif target == "node":
        manifest_data["topology"]["nodes"][invalid_name] = manifest_data["topology"]["nodes"].pop(
            "h1"
        )
        manifest_data["topology"]["links"][0]["endpoints"][0] = f"{invalid_name}:eth0"
    elif target == "interface":
        h1 = manifest_data["topology"]["nodes"]["h1"]
        h1["interfaces"][invalid_name] = h1["interfaces"].pop("eth0")
        manifest_data["topology"]["links"][0]["endpoints"][0] = f"h1:{invalid_name}"
    else:
        manifest_data["topology"]["nodes"]["sw1"]["bridge"]["name"] = invalid_name

    _assert_invalid(tmp_path, manifest_data)


def test_rejects_duplicate_endpoint_use(tmp_path: Path, manifest_data: ManifestData) -> None:
    manifest_data["topology"]["links"][1]["endpoints"][0] = "h1:eth0"
    _assert_invalid(tmp_path, manifest_data)


def test_rejects_missing_endpoint_node(tmp_path: Path, manifest_data: ManifestData) -> None:
    manifest_data["topology"]["links"][0]["endpoints"][0] = "missing:eth0"
    _assert_invalid(tmp_path, manifest_data)


def test_rejects_malformed_endpoint(tmp_path: Path, manifest_data: ManifestData) -> None:
    manifest_data["topology"]["links"][0]["endpoints"][0] = "h1-eth0"
    _assert_invalid(tmp_path, manifest_data)


def test_rejects_loopback_as_link_endpoint(tmp_path: Path, manifest_data: ManifestData) -> None:
    h1 = manifest_data["topology"]["nodes"]["h1"]
    h1["interfaces"]["lo"] = h1["interfaces"].pop("eth0")
    manifest_data["topology"]["links"][0]["endpoints"][0] = "h1:lo"
    error = _assert_invalid(tmp_path, manifest_data)
    assert "lo" in json.dumps(error.details["issues"])


def test_rejects_loopback_as_bridge_name(tmp_path: Path, manifest_data: ManifestData) -> None:
    manifest_data["topology"]["nodes"]["sw1"]["bridge"]["name"] = "lo"
    error = _assert_invalid(tmp_path, manifest_data)
    assert "lo" in json.dumps(error.details["issues"])


def test_rejects_bridge_name_colliding_with_linked_port(
    tmp_path: Path, manifest_data: ManifestData
) -> None:
    manifest_data["topology"]["nodes"]["sw1"]["bridge"]["name"] = "swp1"
    error = _assert_invalid(tmp_path, manifest_data)
    assert "swp1" in json.dumps(error.details["issues"])


def test_rejects_unreferenced_configured_interface(
    tmp_path: Path, manifest_data: ManifestData
) -> None:
    manifest_data["topology"]["nodes"]["h1"]["interfaces"]["eth1"] = {"addresses": ["192.0.2.1/24"]}
    _assert_invalid(tmp_path, manifest_data)


def test_rejects_route_device_not_linked_on_same_node(
    tmp_path: Path, manifest_data: ManifestData
) -> None:
    manifest_data["topology"]["nodes"]["h1"]["routes"] = [
        {"dst": "192.0.2.0/24", "via": "10.10.0.2", "dev": "swp1"}
    ]
    _assert_invalid(tmp_path, manifest_data)


def test_rejects_duplicate_address_on_same_interface(
    manifest_data: ManifestData,
) -> None:
    manifest_data["topology"]["nodes"]["h1"]["interfaces"]["eth0"]["addresses"] = [
        "10.10.0.1/24",
        "10.10.0.1/24",
    ]

    issue = _model_validation_issue(manifest_data)

    assert issue["loc"] == (
        "topology",
        "nodes",
        "h1",
        "linux",
        "interfaces",
        "eth0",
        "addresses",
    )
    assert issue["msg"] == ("Value error, duplicate interface address: '10.10.0.1/24'")


def test_load_manifest_wraps_network_declaration_issue_with_json_location(
    tmp_path: Path, manifest_data: ManifestData
) -> None:
    manifest_data["topology"]["nodes"]["h1"]["interfaces"]["eth0"]["addresses"] = [
        "10.10.0.1/24",
        "10.10.0.1/24",
    ]

    error = _assert_invalid(tmp_path, manifest_data)

    issues = error.details["issues"]
    assert isinstance(issues, list)
    assert len(issues) == 1
    assert issues[0]["loc"] == [
        "topology",
        "nodes",
        "h1",
        "linux",
        "interfaces",
        "eth0",
        "addresses",
    ]
    assert issues[0]["msg"] == ("Value error, duplicate interface address: '10.10.0.1/24'")


@pytest.mark.parametrize(
    ("routes", "expected_destination"),
    [
        (
            [
                {"dst": "192.0.2.0/24", "dev": "eth0"},
                {
                    "dst": "192.0.2.0/24",
                    "via": "10.10.0.2",
                    "dev": "eth0",
                },
            ],
            "192.0.2.0/24",
        ),
        (
            [
                {"dst": "default", "dev": "eth0"},
                {"dst": "0.0.0.0/0", "via": "10.10.0.2", "dev": "eth0"},
            ],
            "0.0.0.0/0",
        ),
    ],
    ids=["same-network", "default-alias"],
)
def test_rejects_duplicate_route_destinations(
    manifest_data: ManifestData,
    routes: list[dict[str, object]],
    expected_destination: str,
) -> None:
    manifest_data["topology"]["nodes"]["h1"]["routes"] = routes

    issue = _model_validation_issue(manifest_data)

    assert issue["loc"] == (
        "topology",
        "nodes",
        "h1",
        "linux",
    )
    assert issue["msg"] == (f"Value error, duplicate route destination: {expected_destination!r}")


@pytest.mark.parametrize(
    ("route_dev", "via"),
    [("eth0", None), ("eth1", "192.0.2.254")],
    ids=["connected-device-without-gateway", "other-device-with-gateway"],
)
def test_rejects_declared_route_for_connected_network(
    manifest_data: ManifestData,
    route_dev: str,
    via: str | None,
) -> None:
    if route_dev == "eth1":
        manifest_data["topology"]["nodes"]["h1"]["interfaces"]["eth1"] = {
            "addresses": ["192.0.2.1/24"]
        }
        manifest_data["topology"]["links"].append(
            {"endpoints": ["h1:eth1", "sw1:swp3"], "mtu": 1500}
        )
    route: dict[str, object] = {"dst": "10.10.0.0/24", "dev": route_dev}
    if via is not None:
        route["via"] = via
    manifest_data["topology"]["nodes"]["h1"]["routes"] = [route]

    issue = _model_validation_issue(manifest_data)

    assert issue["loc"] == (
        "topology",
        "nodes",
        "h1",
        "linux",
    )
    assert issue["msg"] == (
        "Value error, route destination conflicts with connected network: '10.10.0.0/24'"
    )


@pytest.mark.parametrize("placement", ["same-interface", "different-interfaces"])
def test_accepts_distinct_addresses_in_same_connected_network(
    manifest_data: ManifestData,
    placement: str,
) -> None:
    if placement == "same-interface":
        manifest_data["topology"]["nodes"]["h1"]["interfaces"]["eth0"]["addresses"] = [
            "10.10.0.1/24",
            "10.10.0.3/24",
        ]
    else:
        manifest_data["topology"]["nodes"]["h1"]["interfaces"]["eth1"] = {
            "addresses": ["10.10.0.3/24"]
        }
        manifest_data["topology"]["links"].append(
            {"endpoints": ["h1:eth1", "sw1:swp3"], "mtu": 1500}
        )

    manifest = Manifest.model_validate(manifest_data)

    h1 = manifest.topology.nodes["h1"]
    assert IPv4Interface("10.10.0.1/24") in h1.interfaces["eth0"].addresses
    interface_name = "eth0" if placement == "same-interface" else "eth1"
    assert IPv4Interface("10.10.0.3/24") in h1.interfaces[interface_name].addresses


def test_accepts_internal_bridge_interface_configuration(
    tmp_path: Path, manifest_data: ManifestData
) -> None:
    manifest_data["topology"]["nodes"]["sw1"]["interfaces"] = {
        "br0": {"addresses": ["192.0.2.1/24"]}
    }

    manifest = load_manifest(_write_manifest(tmp_path, manifest_data))

    sw1 = manifest.topology.nodes["sw1"]
    assert isinstance(sw1, BridgeNode)
    assert sw1.interfaces["br0"].addresses == (IPv4Interface("192.0.2.1/24"),)


def test_accepts_route_using_internal_bridge_device(
    tmp_path: Path, manifest_data: ManifestData
) -> None:
    manifest_data["topology"]["nodes"]["sw1"]["routes"] = [{"dst": "192.0.2.0/24", "dev": "br0"}]

    manifest = load_manifest(_write_manifest(tmp_path, manifest_data))

    sw1 = manifest.topology.nodes["sw1"]
    assert isinstance(sw1, BridgeNode)
    assert sw1.routes[0].dev == "br0"


def test_rejects_other_unlinked_bridge_interface(
    tmp_path: Path, manifest_data: ManifestData
) -> None:
    manifest_data["topology"]["nodes"]["sw1"]["interfaces"] = {
        "other0": {"addresses": ["192.0.2.1/24"]}
    }
    _assert_invalid(tmp_path, manifest_data)


def test_rejects_route_using_other_unlinked_bridge_device(
    tmp_path: Path, manifest_data: ManifestData
) -> None:
    manifest_data["topology"]["nodes"]["sw1"]["routes"] = [{"dst": "192.0.2.0/24", "dev": "other0"}]
    _assert_invalid(tmp_path, manifest_data)


def test_rejects_unsupported_sysctl(tmp_path: Path, manifest_data: ManifestData) -> None:
    manifest_data["topology"]["nodes"]["h1"]["sysctls"] = {"kernel.hostname": 1}
    _assert_invalid(tmp_path, manifest_data)


@pytest.mark.parametrize("value", [-1, 2, True, "1"])
def test_rejects_sysctl_value_outside_integer_zero_or_one(
    tmp_path: Path, manifest_data: ManifestData, value: object
) -> None:
    manifest_data["topology"]["nodes"]["h1"]["sysctls"] = {"net.ipv4.ip_forward": value}
    _assert_invalid(tmp_path, manifest_data)


@pytest.mark.parametrize(
    "endpoints",
    [
        ["h1:eth0"],
        ["h1:eth0", "sw1:swp1", "h2:eth0"],
    ],
)
def test_rejects_link_endpoint_count_other_than_two(
    tmp_path: Path, manifest_data: ManifestData, endpoints: list[str]
) -> None:
    manifest_data["topology"]["links"][0]["endpoints"] = endpoints
    _assert_invalid(tmp_path, manifest_data)


@pytest.mark.parametrize("mtu", [575, 9217])
def test_rejects_mtu_outside_supported_range(
    tmp_path: Path, manifest_data: ManifestData, mtu: int
) -> None:
    manifest_data["topology"]["links"][0]["mtu"] = mtu
    _assert_invalid(tmp_path, manifest_data)


def test_loads_link_netem_settings(manifest_data: ManifestData) -> None:
    manifest_data["topology"]["links"][0]["netem"] = {
        "delay_ms": 100,
        "jitter_ms": 10,
        "loss_percent": 5,
    }

    manifest = Manifest.model_validate(manifest_data)

    netem = manifest.topology.links[0].netem
    assert netem is not None
    assert (netem.delay_ms, netem.jitter_ms, netem.loss_percent) == (100, 10, 5)
    assert normalized_manifest(manifest)["topology"]["links"][0]["netem"] == {
        "delay_ms": 100,
        "jitter_ms": 10,
        "loss_percent": 5,
    }


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (
            {"rate": "1000kbit"},
            {"delay_ms": 0, "jitter_ms": 0, "loss_percent": 0, "rate": "1mbit"},
        ),
        (
            {"rate": "10mbit", "delay_ms": 20, "jitter_ms": 5, "loss_percent": 1},
            {"rate": "10mbit", "delay_ms": 20, "jitter_ms": 5, "loss_percent": 1},
        ),
    ],
)
def test_loads_and_normalizes_netem_rate(
    manifest_data: ManifestData,
    settings: dict[str, object],
    expected: dict[str, object],
) -> None:
    manifest_data["topology"]["links"][0]["netem"] = settings

    manifest = Manifest.model_validate(manifest_data)

    assert manifest.topology.links[0].netem is not None
    assert normalized_manifest(manifest)["topology"]["links"][0]["netem"] == expected


@pytest.mark.parametrize(
    "qdisc",
    [
        {"kind": "tbf", "rate": "10mbit", "burst": "32kb", "latency_ms": 400},
        {"kind": "fq_codel", "target_ms": 5, "interval_ms": 100, "limit": 10240},
    ],
)
def test_loads_link_qdisc_settings(manifest_data: ManifestData, qdisc: dict[str, object]) -> None:
    manifest_data["topology"]["links"][0]["qdisc"] = qdisc

    manifest = Manifest.model_validate(manifest_data)

    assert manifest.topology.links[0].qdisc is not None
    assert normalized_manifest(manifest)["topology"]["links"][0]["qdisc"]["kind"] == qdisc["kind"]


def test_rejects_netem_and_qdisc_on_one_link(tmp_path: Path, manifest_data: ManifestData) -> None:
    manifest_data["topology"]["links"][0].update(
        {
            "netem": {"rate": "10mbit"},
            "qdisc": {"kind": "fq_codel"},
        }
    )

    _assert_invalid(tmp_path, manifest_data)


@pytest.mark.parametrize(
    "netem",
    [
        {},
        {"delay_ms": 0},
        {"jitter_ms": 10},
        {"delay_ms": -1},
        {"delay_ms": 60_001},
        {"delay_ms": True},
        {"delay_ms": 1.5},
        {"delay_ms": 100, "jitter_ms": -1},
        {"delay_ms": 100, "jitter_ms": 60_001},
        {"loss_percent": -1},
        {"loss_percent": 101},
        {"loss_percent": True},
        {"loss_percent": 1.5},
    ],
)
def test_rejects_invalid_link_netem(
    tmp_path: Path,
    manifest_data: ManifestData,
    netem: dict[str, object],
) -> None:
    manifest_data["topology"]["links"][0]["netem"] = netem

    _assert_invalid(tmp_path, manifest_data)


def test_accepts_ipv6_addresses_routes_and_forwarding(manifest_data: ManifestData) -> None:
    h1 = manifest_data["topology"]["nodes"]["h1"]
    h1["interfaces"]["eth0"]["addresses"] = ["10.10.0.1/24", "2001:db8:1::2/64"]
    h1["routes"] = [
        {"dst": "::/0", "via": "2001:db8:1::1", "dev": "eth0"},
    ]
    h1["sysctls"] = {"net.ipv6.conf.all.forwarding": 1}

    manifest = Manifest.model_validate(manifest_data)

    node = manifest.topology.nodes["h1"]
    assert node.interfaces["eth0"].addresses == (
        IPv4Interface("10.10.0.1/24"),
        IPv6Interface("2001:db8:1::2/64"),
    )
    assert node.routes == (
        RouteConfig(
            dst=IPv6Network("::/0"),
            via=IPv6Address("2001:db8:1::1"),
            dev="eth0",
        ),
    )
    assert node.sysctls == {"net.ipv6.conf.all.forwarding": 1}


@pytest.mark.parametrize(
    ("dst", "via"),
    [
        ("::/0", "10.10.0.2"),
        ("192.0.2.0/24", "2001:db8::1"),
    ],
)
def test_rejects_route_with_mixed_address_families(
    tmp_path: Path,
    manifest_data: ManifestData,
    dst: str,
    via: str,
) -> None:
    manifest_data["topology"]["nodes"]["h1"]["routes"] = [{"dst": dst, "via": via, "dev": "eth0"}]

    error = _assert_invalid(tmp_path, manifest_data)
    assert "same address family" in json.dumps(error.details["issues"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("address", True),
        ("address", 1),
        ("route-destination", True),
        ("route-destination", 1),
        ("route-gateway", True),
        ("route-gateway", 1),
    ],
)
def test_rejects_non_string_ip_scalars(
    tmp_path: Path,
    manifest_data: ManifestData,
    field: str,
    value: object,
) -> None:
    h1 = manifest_data["topology"]["nodes"]["h1"]
    if field == "address":
        h1["interfaces"]["eth0"]["addresses"] = [value]
    else:
        route: dict[str, object] = {
            "dst": "192.0.2.0/24",
            "via": "10.10.0.2",
            "dev": "eth0",
        }
        route["dst" if field == "route-destination" else "via"] = value
        h1["routes"] = [route]

    _assert_invalid(tmp_path, manifest_data)


def test_accepts_typed_ipv4_model_inputs() -> None:
    interface = InterfaceConfig(addresses=(IPv4Interface("192.0.2.1/24"),))
    route = RouteConfig(
        dst=IPv4Network("198.51.100.0/24"),
        via=IPv4Address("192.0.2.254"),
        dev="eth0",
    )

    assert interface.addresses == (IPv4Interface("192.0.2.1/24"),)
    assert route.dst == IPv4Network("198.51.100.0/24")
    assert route.via == IPv4Address("192.0.2.254")


def test_accepts_typed_ipv6_model_inputs() -> None:
    interface = InterfaceConfig(addresses=(IPv6Interface("2001:db8::1/64"),))
    route = RouteConfig(
        dst=IPv6Network("2001:db8:1::/64"),
        via=IPv6Address("2001:db8::fe"),
        dev="eth0",
    )

    assert interface.addresses == (IPv6Interface("2001:db8::1/64"),)
    assert route.dst == IPv6Network("2001:db8:1::/64")
    assert route.via == IPv6Address("2001:db8::fe")


def test_normalizes_default_route_and_serializes_ip_values(
    tmp_path: Path, manifest_data: ManifestData
) -> None:
    manifest_data["topology"]["nodes"]["h1"]["routes"] = [
        {"dst": "default", "via": "10.10.0.254", "dev": "eth0"}
    ]

    manifest = load_manifest(_write_manifest(tmp_path, manifest_data))
    h1 = manifest.topology.nodes["h1"]
    assert isinstance(h1, LinuxNode)
    route = h1.routes[0]
    assert isinstance(route, RouteConfig)
    assert route.dst == IPv4Network("0.0.0.0/0")
    assert route.via == IPv4Address("10.10.0.254")

    normalized = normalized_manifest(manifest)
    h1_data = normalized["topology"]["nodes"]["h1"]
    assert h1_data["interfaces"]["eth0"]["addresses"] == ["10.10.0.1/24"]
    assert h1_data["routes"][0] == {
        "dst": "0.0.0.0/0",
        "via": "10.10.0.254",
        "dev": "eth0",
    }
    json.dumps(normalized)


def test_omitted_stp_tuning_preserves_legacy_normalized_manifest(
    tmp_path: Path, manifest_data: ManifestData
) -> None:
    manifest = load_manifest(_write_manifest(tmp_path, manifest_data))

    normalized = normalized_manifest(manifest)
    bridge = normalized["topology"]["nodes"]["sw1"]["bridge"]

    assert "priority" not in bridge
    assert "ports" not in bridge


def test_fingerprint_is_sorted_compact_sha256_of_normalized_manifest(
    tmp_path: Path, manifest_data: ManifestData
) -> None:
    manifest = load_manifest(_write_manifest(tmp_path, manifest_data))
    reordered = copy.deepcopy(manifest_data)
    reordered["topology"]["nodes"] = {
        name: reordered["topology"]["nodes"][name] for name in ("h2", "sw1", "h1")
    }
    reordered_manifest = Manifest.model_validate(reordered)

    fingerprint = manifest_fingerprint(manifest)

    assert len(fingerprint) == 64
    assert int(fingerprint, 16) >= 0
    assert fingerprint == "9b002753bcf4904a6a92583d4e4608a1b2c13faa3372ba29841c3715fe426c19"
    assert fingerprint == manifest_fingerprint(reordered_manifest)


def test_models_are_frozen(tmp_path: Path, manifest_data: ManifestData) -> None:
    manifest = load_manifest(_write_manifest(tmp_path, manifest_data))

    with pytest.raises(ValidationError):
        manifest.name = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        manifest.topology.links[0].mtu = 1400  # type: ignore[misc]


def test_yaml_parser_errors_are_translated(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("topology: [\n", encoding="utf-8")

    with pytest.raises(NslabError) as caught:
        load_manifest(path)

    error = caught.value
    assert error.code == "MANIFEST_INVALID"
    assert error.details["path"] == str(path.resolve())
    assert error.details["issues"]


def test_invalid_utf8_is_translated(tmp_path: Path) -> None:
    path = tmp_path / "invalid-utf8.yaml"
    path.write_bytes(b"version: \xff\n")

    with pytest.raises(NslabError) as caught:
        load_manifest(path)

    error = caught.value
    assert error.code == "MANIFEST_INVALID"
    assert error.details["path"] == str(path.resolve())
    assert error.details["issues"]


def test_yaml_reader_error_without_problem_mark_is_translated(tmp_path: Path) -> None:
    path = tmp_path / "nul.yaml"
    path.write_bytes(b"version: 1\x00\n")

    with pytest.raises(NslabError) as caught:
        load_manifest(path)

    error = caught.value
    assert error.code == "MANIFEST_INVALID"
    assert error.details["path"] == str(path.resolve())
    assert error.details["issues"]


@pytest.mark.parametrize("document", [None, [], "not-a-mapping"])
def test_non_mapping_yaml_documents_are_translated(tmp_path: Path, document: object) -> None:
    _assert_invalid(tmp_path, document)
