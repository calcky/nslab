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
│     │  ├─ interfaces / devices / routes / rules / sysctls
│     │  ├─ devices → <device-name> → type: vlan | vrf | bond | vxlan
│     │  └─ routing
│     └─ kind: bridge
│        ├─ interfaces / devices / routes / sysctls
│        ├─ devices → <device-name> → type: vxlan
│        └─ bridge → ports → vlans
└─ links
   └─ endpoints / mtu / netem | qdisc
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
Namespace-local VLAN, VRF, bond, and VXLAN devices belong under `devices`, not `interfaces`.

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
| `dev` | Yes | None | Available egress interface: linked, namespace-local, or the node's bridge device |
| `table` | No | Automatic | Routing table ID in `1..4294967295`, excluding local table `255` |

A node cannot repeat a destination within one routing table or declare one of that table's
connected networks as a static route. An omitted `table` uses main table 254, except that VRF
member interfaces select their VRF table automatically. An explicit table on a VRF member route
must equal that VRF's table.

##### `sysctls`

Only these keys are accepted, and values must be integer `0` or `1`:

| Key | Description |
| --- | --- |
| `net.ipv4.ip_forward` | Disable or enable IPv4 forwarding |
| `net.ipv6.conf.all.forwarding` | Disable or enable IPv6 forwarding |

#### `kind: linux`

A Linux node represents a regular network namespace and accepts the common fields plus
namespace-local devices, policy rules, and dynamic routing:

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

##### `rules[]`

`rules` declares Linux routing policy database (RPDB) entries. Rules are evaluated by ascending
`priority`; each rule combines every selector it declares. The address family is inferred from
`from` or `to`, and otherwise defaults to IPv4.

```yaml
routes:
  - dst: 203.0.113.0/24
    via: 10.0.0.2
    dev: eth1
    table: 100
rules:
  - priority: 100
    from: 192.0.2.0/24
    to: 203.0.113.0/24
    iif: eth0
    table: 100
```

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `priority` | Yes | None | Rule preference in `1..4294967295`; unique within its address family |
| `family` | No | Inferred or IPv4 | `ipv4` or `ipv6`; set it for selector-free IPv6 rules |
| `action` | No | `lookup` | `lookup`, `goto`, `nop`, `blackhole`, `unreachable`, or `prohibit` |
| `table` | Conditional | `null` | Table ID in `1..4294967295`; required by `lookup` unless `l3mdev: true` |
| `goto` | Conditional | `null` | Greater priority to jump to; required by `action: goto` |
| `from` | No | All sources | Source IPv4/IPv6 prefix |
| `to` | No | All destinations | Destination IPv4/IPv6 prefix |
| `not` | No | `false` | Invert the result of the complete selector |
| `tos` | No | Unspecified | IPv4 TOS or IPv6 traffic class in `0..255`; zero is normalized to unspecified |
| `fwmark` | No | `null` | Packet mark in `0..4294967295` |
| `fwmask` | No | Full mask | Mark mask in `0..4294967295`; requires `fwmark` |
| `iif` | No | Any | Input interface name, including `lo` or a declared Linux device |
| `oif` | No | Any | Output interface name, including `lo` or a declared Linux device |
| `l3mdev` | No | `false` | Match packets associated with an L3 master and use its routing table |
| `uid_range` | No | `null` | Local socket UID range as `{start, end}`, each in `0..4294967295` |
| `protocol` | No | `0` | Rule-origin protocol number in `0..255` |
| `ip_protocol` | No | `null` | IP protocol in `0..255`; zero is normalized to unspecified |
| `source_port` | No | `null` | Source port range as `{start, end}`, each in `0..65535` |
| `destination_port` | No | `null` | Destination port range as `{start, end}`, each in `0..65535` |
| `tunnel_id` | No | `null` | Tunnel key in `0..18446744073709551615`; zero means unspecified |
| `suppress_prefix_length` | No | `null` | Ignore lookup results whose prefix length is at most this value |
| `suppress_interface_group` | No | `null` | Ignore results using interface group `0..4294967294` |
| `realms` | No | `null` | Route realms as `{source, destination}`, each in `0..65535` |

`from` and `to` must use the selected family. Port ranges require a nonzero `ip_protocol`, range starts
cannot exceed their ends, and at least one realm must be nonzero. Suppress options are valid only
for `lookup`. A `goto` target must be numerically greater than its rule but may remain unresolved;
Linux will resolve it if a suitable rule is added later. Lookup rules may select the standard
tables 253 (`default`), 254 (`main`), and 255 (`local`) by number.

When a node declares a VRF, Linux installs its own priority-1000 `l3mdev` rule, so that priority is
reserved on the node. The deprecated route-NAT rule action is not supported because current Linux
does not preserve its address through the netlink inventory used for deterministic drift checks.

##### `devices`

`devices` creates interfaces inside the Linux node after all veth endpoints have been moved
into place. Device names follow the interface-name rules, cannot be `lo`, and cannot collide with
a linked endpoint or an `interfaces` key. `type` is required and selects `vlan`, `vrf`, `bond`, or
`vxlan`.

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

###### `type: vxlan`

A standalone VXLAN device is a Linux Layer 3 interface. It uses a static unicast remote VTEP and
may own addresses and routes directly:

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `devices.<name>.type` | Yes | None | Must be `vxlan` |
| `devices.<name>.vni` | Yes | None | VXLAN Network Identifier in `1..16777215`, unique on the node |
| `devices.<name>.link` | Yes | None | Linked underlay interface on the same node |
| `devices.<name>.local` | Yes | None | Unicast IPv4/IPv6 source address configured on `link` |
| `devices.<name>.remote` | Yes | None | Static unicast VTEP address in the same family as `local` |
| `devices.<name>.addresses` | No | `[]` | IPv4/IPv6 addresses assigned to the VXLAN interface |
| `devices.<name>.dst_port` | No | `4789` | UDP destination port in `1..65535` |
| `devices.<name>.learning` | No | `true` | Enable source-MAC learning |
| `devices.<name>.mtu` | No | Automatic | MTU bounded by underlay MTU minus encapsulation overhead |

The underlay `link` must be linked and contain the exact `local` address. The automatic MTU
subtracts 50 bytes for IPv4 or 70 bytes for IPv6. A standalone VXLAN has no bridge master, so it
can be used as `routes[].dev`, as shown in the combined VXLAN example at
`examples/vxlan/nslab.yaml`.

###### `type: bond`

A bond combines two or more linked interfaces into one logical interface. IP addresses and routes
belong to the bond; member interfaces must not declare addresses. All member links must use the
same MTU, and one linked interface cannot belong to multiple bonds.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `devices.<name>.type` | Yes | None | Must be `bond` |
| `devices.<name>.mode` | Yes | None | `active-backup` or `802.3ad` |
| `devices.<name>.interfaces` | Yes | None | At least two unique linked member interfaces |
| `devices.<name>.addresses` | No | `[]` | Unique IPv4/IPv6 CIDR addresses assigned to the bond |
| `devices.<name>.miimon_ms` | No | `100` | MII carrier polling interval in `0..60000` ms; zero disables polling |
| `devices.<name>.primary` | No | `null` | Preferred member; valid only for `active-backup` and must name a member |
| `devices.<name>.lacp_rate` | No | `slow` | `slow` or `fast`; valid only for `802.3ad` |
| `devices.<name>.xmit_hash_policy` | No | `layer2` | `layer2`, `layer2+3`, or `layer3+4`; valid only for `802.3ad` |
| `devices.<name>.min_links` | No | `0` | Minimum active links in `0..65535`; valid only for `802.3ad` and cannot exceed the member count |

The `802.3ad` peer must also run LACP. A single flow normally hashes to one member; multiple flows
are needed to observe distribution across links. Routes and dynamic routing may use a bond, but a
bond cannot currently be a VLAN parent or VRF member.

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
| `devices` | No | `{}` | Static VXLAN devices attached to this bridge |
| `bridge` | Yes | None | Linux bridge device and port configuration object |

```yaml
sw1:
  kind: bridge
  interfaces:
    underlay0:
      addresses: [192.0.2.1/30]
  devices:
    vxlan100:
      type: vxlan
      vni: 100
      link: underlay0
      local: 192.0.2.1
      remote: 192.0.2.2
  bridge:
    name: br0
    stp: true
    vlan_filtering: false
```

##### `devices.<name>` with `type: vxlan`

A bridge-node VXLAN device creates a static unicast Layer 2 tunnel and automatically joins
`bridge.name`.
Its lower `link` must be a linked interface with the exact `local` address configured under
`interfaces`; that underlay interface stays outside the bridge.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `type` | Yes | None | Must be `vxlan` |
| `vni` | Yes | None | VXLAN Network Identifier in `1..16777215`, unique on the node |
| `link` | Yes | None | Linked underlay interface on the same bridge node |
| `local` | Yes | None | Unicast IPv4/IPv6 source address configured on `link` |
| `remote` | Yes | None | Static unicast remote VTEP address in the same family as `local` |
| `dst_port` | No | `4789` | UDP destination port in `1..65535` |
| `learning` | No | `true` | Enable source-MAC learning on the VXLAN interface |
| `mtu` | No | Automatic | VXLAN MTU in `576..9216`, bounded by underlay MTU minus encapsulation overhead |

The automatic MTU subtracts 50 bytes for an IPv4 underlay or 70 bytes for IPv6. A custom value
cannot exceed that limit. `local` and `remote` cannot be equal, unspecified, or multicast. The
kernel installs a permanent all-zero-MAC FDB entry for the static remote. `bridge.ports` may name
the VXLAN device to configure STP or VLAN behavior, but it cannot name a VXLAN underlay interface.
Bridge-node VXLAN devices cannot declare `addresses`; use a Linux node for routed VXLAN.

##### `bridge`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `name` | Yes | None | Bridge device name inside the namespace; cannot be `lo` |
| `stp` | Yes | None | Enable Linux bridge STP |
| `vlan_filtering` | Yes | None | Enable VLAN-aware filtering |
| `priority` | No | `null` | Bridge priority in `0..65535` |
| `ports` | No | `{}` | Mapping from linked access or VXLAN port name to STP/VLAN settings |

`bridge.name` cannot collide with a linked endpoint. To assign an IP address to the bridge
itself, use the same `bridge.name` under the node's `interfaces` mapping.

###### `bridge.ports.<ifname>`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `path_cost` | No | `null` | STP path cost in `1..65535`; requires `stp: true` |
| `priority` | No | `null` | Linux STP port priority in `0..63`; requires `stp: true` |
| `vlans` | No | `[]` | Port VLAN entries; requires `vlan_filtering: true` |

A port configuration must contain at least one STP or VLAN setting, and the port must be a linked
access interface or declared VXLAN device.

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
| `netem` | No | `null` | Netem conditions applied to egress at both ends; mutually exclusive with `qdisc` |
| `qdisc` | No | `null` | Root traffic-control qdisc applied to egress at both ends; currently `tbf` or `fq_codel` |

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
| `rate` | No | `null` | Optional egress rate such as `10mbit`, `500kbit`, or `1gbit` |

All four values cannot be zero. netem is installed on both veth ends, so a bidirectional
ping experiences one egress condition on the request and another on the reply.

```yaml
netem:
  rate: 10mbit
  delay_ms: 20
  jitter_ms: 5
  loss_percent: 1
```

#### `links[].qdisc`

`qdisc` selects one root egress queue discipline. It cannot be combined with `netem` on the
same link, and is installed on both veth ends.

For a token-bucket shaper (`tbf`):

```yaml
qdisc:
  kind: tbf
  rate: 10mbit
  burst: 32kb
  latency_ms: 400
```

`burst` is a positive byte count (or a `kb`/`mb`/`gb` value). `latency_ms` is the maximum
queueing latency used to derive the TBF byte limit.

For fair queueing with controlled delay (`fq_codel`):

```yaml
qdisc:
  kind: fq_codel
  target_ms: 5
  interval_ms: 100
  limit: 10240
  ecn: true
```

`target_ms` and `interval_ms` are queue-delay parameters, `limit` is the packet limit, and
`ecn` enables ECN marking.

## Manifest boundary

A manifest does not accept `traffic`, `observe`, packet capture, or arbitrary command fields.
Run traffic and observation actions explicitly with [`nslab exec`](cli.md#exec). See
[Examples](examples/index.md) for complete runnable manifests.
