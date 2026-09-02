# Manifest field reference

`nslab.yaml` uses a strict schema. Unknown fields, incorrect types, and invalid references are
rejected before any kernel network resource is changed. The complete hierarchy is:

```text
version
name
topology
├─ nodes
│  └─ <node-name>
│     ├─ kind: linux
│     │  ├─ interfaces / devices / routes / sysctls
│     │  ├─ devices → <device-name> → type: vlan | vrf
│     │  └─ routing
│     └─ kind: bridge
│        ├─ interfaces / routes / sysctls
│        └─ bridge → ports → vlans
└─ links
   └─ endpoints / mtu / netem
```

## Top-level fields

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `version` | Yes | None | Schema version; currently only integer `1` |
| `name` | Yes | None | Deployment name matching `^[a-z][a-z0-9_-]{0,31}$` |
| `topology` | Yes | None | Topology object containing `nodes` and `links` |

```yaml
version: 1
name: my-lab
topology:
  nodes: {}
  links: []
```

## `topology`

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `topology.nodes` | Yes | Mapping | Keys are node names; values are Linux or bridge nodes |
| `topology.links` | Yes | List | Each item describes a two-ended veth link |

### `topology.nodes`

Node names use the deployment-name format: start with a lowercase letter, contain no more than
32 characters, and use lowercase letters, digits, `_`, or `-`. Current node kinds are `linux`
and `bridge`.

#### Common node fields

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `kind` | Yes | None | Discriminator: `linux` or `bridge` |
| `interfaces` | No | `{}` | Mapping from interface name to interface configuration |
| `routes` | No | `[]` | Static IPv4/IPv6 routes |
| `sysctls` | No | `{}` | Network sysctls that nslab permits |
| `routing` | No | `null` | OSPF/BGP configuration, allowed only on `linux` nodes |

Interface names contain 1 to 15 letters, digits, `_`, `.`, or `-`. Except for a bridge device
name, every interface declared in `interfaces` must appear in a `links[].endpoints` entry.
Namespace-local VLAN and VRF devices belong under `devices`, not `interfaces`.

##### `interfaces.<ifname>`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `addresses` | No | `[]` | Unique IPv4/IPv6 CIDR addresses, such as `10.0.0.1/24` or `2001:db8::1/64` |

An interface may contain multiple addresses or no address. Duplicate addresses are rejected.

##### `routes[]`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `dst` | Yes | None | IPv4/IPv6 destination prefix; `default` means `0.0.0.0/0` |
| `via` | No | `null` | Next-hop address in the same address family as `dst` |
| `dev` | Yes | None | Egress interface: a linked interface or the node's bridge device |

A node cannot repeat a destination within one routing table or declare one of that table's
connected networks as a static route. VRF member interfaces select their VRF table automatically.

##### `sysctls`

Only these keys are accepted, and values must be integer `0` or `1`:

| Key | Description |
| --- | --- |
| `net.ipv4.ip_forward` | Disable or enable IPv4 forwarding |
| `net.ipv6.conf.all.forwarding` | Disable or enable IPv6 forwarding |

#### `kind: linux`

A Linux node represents a regular network namespace and accepts the common fields plus
namespace-local devices and dynamic routing:

```yaml
r1:
  kind: linux
  interfaces:
    eth0:
      addresses: [10.0.12.1/30]
  devices:
    vlan10:
      type: vlan
      link: eth0
      id: 10
      addresses: [192.168.10.1/24]
  sysctls:
    net.ipv4.ip_forward: 1
```

##### `devices`

`devices` creates interfaces inside the Linux node after all veth endpoints have been moved
into place. Device names follow the interface-name rules, cannot be `lo`, and cannot collide with
a linked endpoint or an `interfaces` key. `type` is required and selects `vlan` or `vrf`.

###### `type: vlan`

An 802.1Q VLAN subinterface may be used by `routes[].dev` and
`routing.ospf.passive_interfaces`:

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `devices.<name>.type` | Yes | None | Must be `vlan` |
| `devices.<name>.link` | Yes | None | Lower interface; must be a linked interface on the same node |
| `devices.<name>.id` | Yes | None | VLAN ID in `1..4094`, unique on the lower interface |
| `devices.<name>.addresses` | No | `[]` | Unique IPv4/IPv6 CIDR addresses assigned to the VLAN device |

Only one level is supported: a VLAN device cannot use another declared device as its lower
interface. Its MTU follows the lower interface. Connected routes, BGP directly connected
neighbor checks, and automatic OSPF/BGP network statements include device addresses.

###### `type: vrf`

A VRF is a layer-3 master that assigns its member interfaces to a dedicated Linux routing table:

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `devices.<name>.type` | Yes | None | Must be `vrf` |
| `devices.<name>.table` | Yes | None | Table ID in `1..4294967295`, excluding reserved tables `253`, `254`, and `255` |
| `devices.<name>.interfaces` | Yes | None | Non-empty list of linked interfaces or declared VLAN devices |

Table IDs are unique within a node, and an interface may belong to only one VRF. Connected and
declared static routes for a member automatically use the VRF table, so the same destination may
appear once in each routing domain. Dynamic OSPF/BGP configuration cannot currently be combined
with VRF devices; run routing daemons explicitly through `nslab exec` for advanced VRF labs.

##### `routing`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `routing.ospf` | Conditional | `null` | OSPFv2 configuration |
| `routing.bgp` | Conditional | `null` | IPv4 eBGP configuration |

At least one protocol is required when `routing` is present; both may be enabled together. The
node must set `net.ipv4.ip_forward: 1`. nslab starts independent FRRouting daemons and a distinct
pathspace for every configured node.

###### `routing.ospf`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `router_id` | Yes | None | Unique IPv4 router ID |
| `area` | No | `0.0.0.0` | OSPF area used by every network statement |
| `networks` | No | `[]` | Advertised IPv4 prefixes; empty means all connected IPv4 networks |
| `passive_interfaces` | No | `[]` | Interfaces that advertise their network without forming neighbors |

Every passive interface must belong to the node. OSPF router IDs must be unique within a
manifest, and network prefixes cannot repeat.

###### `routing.bgp`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `local_as` | Yes | None | Local ASN in `1..4294967295` |
| `router_id` | Yes | None | Unique IPv4 router ID |
| `neighbors` | Yes | None | List of directly connected IPv4 BGP neighbors |
| `networks` | No | `[]` | Advertised IPv4 prefixes; empty means all connected IPv4 networks |

Each `neighbors[]` item contains:

| Field | Required | Description |
| --- | --- | --- |
| `address` | Yes | Neighbor IPv4 address in one of this node's connected IPv4 networks |
| `remote_as` | Yes | Remote ASN in `1..4294967295` |

Neighbor addresses and network prefixes cannot repeat. BGP router IDs must be unique within a
manifest.

#### `kind: bridge`

A bridge node creates a Linux bridge in its own namespace. It accepts the common `interfaces`,
`routes`, and `sysctls` fields but cannot declare `routing`.

| Node field | Required | Default | Description |
| --- | --- | --- | --- |
| `bridge` | Yes | None | Linux bridge device and port configuration object |

```yaml
sw1:
  kind: bridge
  bridge:
    name: br0
    stp: true
    vlan_filtering: false
```

##### `bridge`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `name` | Yes | None | Bridge device name inside the namespace; cannot be `lo` |
| `stp` | Yes | None | Enable Linux bridge STP |
| `vlan_filtering` | Yes | None | Enable VLAN-aware filtering |
| `priority` | No | `null` | Bridge priority in `0..65535` |
| `ports` | No | `{}` | Mapping from linked port name to STP/VLAN settings |

`bridge.name` cannot collide with a linked endpoint. To assign an IP address to the bridge
itself, use the same `bridge.name` under the node's `interfaces` mapping.

###### `bridge.ports.<ifname>`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `path_cost` | No | `null` | STP path cost in `1..65535`; requires `stp: true` |
| `priority` | No | `null` | Linux STP port priority in `0..63`; requires `stp: true` |
| `vlans` | No | `[]` | Port VLAN entries; requires `vlan_filtering: true` |

A port configuration must contain at least one STP or VLAN setting, and the port must be linked.

Each `vlans[]` item contains:

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `vid` | Yes | None | VLAN ID in `1..4094`, unique on the port |
| `pvid` | No | `false` | PVID for ingress untagged frames; at most one per port |
| `untagged` | No | `false` | Remove the 802.1Q tag on egress |

### `topology.links`

Each link joins two `node:interface` endpoints. An endpoint may appear only once in the entire
topology. Node and interface references must be valid and cannot use `lo`.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `kind` | No | `veth` | Link kind; currently only `veth` |
| `endpoints` | Yes | None | Exactly two `node:interface` strings |
| `mtu` | No | `1500` | MTU on both ends in `576..9216` |
| `netem` | No | `null` | Link conditions applied to egress at both ends |

```yaml
links:
  - endpoints: [h1:eth0, h2:eth0]
    mtu: 1500
```

#### `links[].netem`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `delay_ms` | No | `0` | Per-end egress delay in `0..60000` ms |
| `jitter_ms` | No | `0` | Delay jitter in `0..60000` ms; requires `delay_ms > 0` |
| `loss_percent` | No | `0` | Random packet loss as an integer percentage in `0..100` |

The three values cannot all be zero. netem is installed on both veth ends, so a bidirectional
ping experiences one egress condition on the request and another on the reply.

## Manifest boundary

A manifest does not accept `traffic`, `observe`, packet capture, or arbitrary command fields.
Run traffic and observation actions explicitly with [`nslab exec`](cli.md#exec). See
[Examples](examples/index.md) for complete runnable manifests.
