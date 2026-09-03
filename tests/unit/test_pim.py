from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from ipaddress import IPv4Address
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from nslab.backend.base import InterfaceInventory, LiveInventory, inventory_matches_plan
from nslab.backend.fake import FakeNetworkBackend
from nslab.graph import render_graph
from nslab.inspector import inspect_topology
from nslab.lifecycle import LifecycleService
from nslab.manifest import Manifest, load_manifest
from nslab.planner import compile_plan
from nslab.routing import render_frr_config, routing_daemons, routing_protocols
from nslab.state import StateStore

_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "pim" / "nslab.yaml"


def _document() -> dict[str, object]:
    document = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_pim_example_compiles_ospf_and_static_rp_configuration() -> None:
    plan = compile_plan(load_manifest(_EXAMPLE))

    assert tuple(plan.nodes) == ("source", "r1", "r2", "r3", "receiver1", "receiver2")
    r3 = plan.nodes["r3"]
    assert r3.routing is not None
    assert r3.routing.pim is not None
    assert r3.routing.pim.rp_address == IPv4Address("10.255.0.2")
    assert r3.routing.pim.interfaces == ("eth0", "eth1", "eth2")
    assert r3.routing.pim.igmp_interfaces == ("eth1", "eth2")
    assert routing_protocols(r3.routing) == ("ospf", "pim")
    assert routing_daemons(r3.routing) == ("ospfd", "pimd")


def test_render_pim_config_enables_pim_and_igmp_on_selected_interfaces() -> None:
    plan = compile_plan(load_manifest(_EXAMPLE))

    config = render_frr_config(plan.nodes["r3"], plan)

    assert "ip pim rp 10.255.0.2 224.0.0.0/4\n" in config
    assert "interface eth0\n ip ospf network point-to-point\n!\n" in config
    assert "interface eth0\n ip pim\n!\n" in config
    assert "interface eth1\n ip pim\n ip igmp\n!\n" in config
    assert "interface eth2\n ip pim\n ip igmp\n!\n" in config


def test_pim_graph_detail_shows_static_rp() -> None:
    plan = compile_plan(load_manifest(_EXAMPLE))

    compact = render_graph(plan, "tree")
    detailed = render_graph(plan, "tree", detail=True)

    assert "r3 [linux]" in compact
    assert "r3 [linux · ospf · pim" not in compact
    assert "r3 [linux · ospf · pim rp 10.255.0.2]" in detailed


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda document: document["topology"]["nodes"]["r1"]["routing"]["pim"].update(
                {"rp_address": "239.1.1.1"}
            ),
            "PIM rp_address must be a unicast IPv4 address",
        ),
        (
            lambda document: document["topology"]["nodes"]["r1"]["routing"]["pim"].update(
                {"interfaces": ["eth9"]}
            ),
            "PIM interface is not available",
        ),
        (
            lambda document: document["topology"]["nodes"]["r1"]["interfaces"]["eth1"].update(
                {"addresses": ["2001:db8:12::1/64"]}
            ),
            "PIM interface requires an IPv4 address",
        ),
        (
            lambda document: document["topology"]["nodes"]["r3"]["routing"]["pim"].update(
                {"interfaces": ["eth0"], "igmp_interfaces": ["eth1"]}
            ),
            "PIM IGMP interface must also enable PIM",
        ),
        (
            lambda document: document["topology"]["nodes"]["r3"]["routing"]["pim"].update(
                {"rp_address": "10.255.0.3"}
            ),
            "all PIM nodes must use the same rp_address",
        ),
        (
            lambda document: document["topology"]["nodes"]["r1"].update(
                {"devices": {"pimreg": {"type": "dummy"}}}
            ),
            "PIM runtime interface name is reserved",
        ),
    ],
)
def test_manifest_rejects_invalid_pim_configuration(mutate: object, message: str) -> None:
    document = deepcopy(_document())
    assert callable(mutate)
    mutate(document)

    with pytest.raises(ValidationError, match=message):
        Manifest.model_validate(document)


def test_pim_runtime_interface_is_ignored_by_inventory_and_inspection(tmp_path: Path) -> None:
    manifest = load_manifest(_EXAMPLE)
    plan = compile_plan(manifest)
    backend = FakeNetworkBackend()
    store = StateStore(tmp_path)
    service = LifecycleService(
        backend,
        store,
        lock_factory=lambda _name: nullcontext(),
    )
    service.deploy(plan, manifest)
    inventory = backend.inventory(plan)
    r1 = plan.nodes["r1"]
    namespace = inventory.namespaces[r1.namespace]
    interfaces = dict(namespace.interfaces)
    interfaces["pimreg"] = InterfaceInventory(
        name="pimreg",
        kind="unknown",
        ifindex=999,
        master=None,
        mtu=1500,
        up=True,
    )
    namespaces = dict(inventory.namespaces)
    namespaces[r1.namespace] = replace(namespace, interfaces=interfaces)
    with_runtime_interface = LiveInventory(
        namespaces=namespaces,
        root_interfaces=inventory.root_interfaces,
    )

    assert inventory_matches_plan(plan, with_runtime_interface)
    snapshot = store.load(plan.name)
    assert snapshot is not None
    report = inspect_topology(plan, snapshot, with_runtime_interface)
    assert report.status == "deployed"
    assert report.differences == ()
