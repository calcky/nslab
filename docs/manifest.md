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
│     │  ├─ interfaces / devices / routes → nexthops / neighbors / rules / sysctls
│     │  ├─ devices → <device-name> → type: vlan | vrf | bond | gre | ipip | vxlan | dummy | geneve | macvlan | ipvlan
│     │  └─ routing
│     └─ kind: bridge
│        ├─ interfaces / devices / routes / neighbors / sysctls
│        ├─ devices → <device-name> → type: vxlan | geneve
│        └─ bridge → ports → vlans
└─ links
   └─ <link>
      ├─ endpoints / mtu
      ├─ netem
      │  └─ delay_ms / jitter_ms / loss_percent / rate
      └─ qdisc
         └─ kind: tbf | fq_codel
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
| `neighbors` | No | `[]` | Static IPv4 ARP, IPv6 NDP, and proxy neighbor entries |
| `sysctls` | No | `{}` | Network sysctls that nslab permits |
| `routing` | No | `null` | OSPF/BGP/PIM configuration, allowed only on `linux` nodes |

Interface names contain 1 to 15 letters, digits, `_`, `.`, or `-`. Except for a bridge device
name, every interface declared in `interfaces` must appear in a `links[].endpoints` entry.
Namespace-local VLAN, VRF, bond, GRE, IPIP, VXLAN, Geneve, dummy, macvlan, and ipvlan devices
belong under `devices`, not `interfaces`.

##### `interfaces.<ifname>`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `addresses` | No | `[]` | Unique IPv4/IPv6 CIDR addresses, such as `10.0.0.1/24` or `2001:db8::1/64` |
| `mac` | No | Automatic | Fixed unicast MAC in colon-delimited form, such as `02:00:00:00:00:01` |

An interface may contain multiple addresses or no address. Duplicate addresses are rejected. MAC
addresses are normalized to lowercase; multicast, broadcast, and all-zero addresses are rejected.

##### `routes[]`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `dst` | Yes | None | IPv4/IPv6 destination prefix; `default` means `0.0.0.0/0` |
| `via` | No | `null` | Single next-hop address in the same address family as `dst`; incompatible with `nexthops` |
| `dev` | Conditional | `null` | Single-path egress interface; required without `nexthops` |
| `nexthops` | Conditional | `[]` | Two or more ECMP next hops; incompatible with top-level `via` and `dev` |
| `table` | No | Automatic | Routing table ID in `1..4294967295`, excluding local table `255` |

A node cannot repeat a destination within one routing table or declare one of that table's
connected networks as a static route. An omitted `table` uses main table 254, except that VRF
member interfaces select their VRF table automatically. An explicit table on a VRF member route
must equal that VRF's table.

A multipath route replaces top-level `via` and `dev` with `nexthops`:

```yaml
routes:
  - dst: 192.0.2.0/24
    nexthops:
      - via: 10.0.12.2
        dev: eth1
        weight: 1
      - via: 10.0.13.2
        dev: eth2
        weight: 1
```

Each `nexthops[]` item contains:

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `via` | No | `null` | Gateway in the destination address family; omit for a directly attached next hop |
| `dev` | Yes | None | Available egress interface |
| `weight` | No | `1` | Relative next-hop weight in `1..256` |

At least two unique `via + dev` combinations are required. When `table` is omitted, all next-hop
interfaces must resolve to the same routing table; this permits ECMP inside one VRF but rejects a
route that accidentally spans routing domains. Equal weights provide ECMP. Unequal values provide
weighted multipath distribution, which is statistical across flows rather than packet-by-packet.

##### `neighbors[]`

`neighbors` declares entries in the IPv4 ARP or IPv6 NDP table. A regular entry maps an IP address
to a fixed link-layer address; a proxy entry makes Linux answer address resolution for an address
that is reached through another interface.

```yaml
neighbors:
  - dst: 192.0.2.2
    dev: eth0
    lladdr: 02:00:00:00:00:02
    state: permanent
  - dst: 2001:db8:1::200
    dev: eth0
    proxy: true
```

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `dst` | Yes | None | Unicast IPv4 or IPv6 neighbor address |
| `dev` | Yes | None | Linked interface, bridge interface, or declared device containing the entry |
| `lladdr` | Regular only | None | Unicast neighbor MAC in colon-delimited form |
| `state` | No | `permanent` | Regular-entry NUD state: `permanent`, `reachable`, `stale`, or `noarp` |
| `proxy` | No | `false` | Install a proxy entry instead of a regular IP-to-MAC mapping |

Each `dst + dev` pair must be unique. A regular entry requires `lladdr`; a proxy entry forbids both
`lladdr` and `state`. Declaring an IPv4 proxy automatically enables
`net.ipv4.conf.<dev>.proxy_arp`; an IPv6 proxy similarly enables
`net.ipv6.conf.<dev>.proxy_ndp`.

`permanent` does not age. `reachable` is confirmed reachable, while `stale` retains the MAC and
asks NUD to confirm it when used. `noarp` suppresses neighbor probing. Normal traffic may move a
declared `reachable` or `stale` entry among `reachable`, `stale`, `delay`, and `probe`; inventory
and drift checks treat those healthy transitions as matching. Dynamically learned entries not
declared in the manifest are ignored.

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
a linked endpoint or an `interfaces` key. `type` is required and selects `vlan`, `vrf`, `bond`,
`gre`, `ipip`, `vxlan`, `dummy`, `geneve`, `macvlan`, or `ipvlan`.

The common `addresses` field is available on every device except VRF. The common `mac` field is
available on Ethernet-like `vlan`, `bond`, `vxlan`, `dummy`, `geneve`, and `macvlan` devices. GRE,
IPIP, and ipvlan devices reject `mac` because their kernel link types do not expose an independent
configurable Ethernet address.

###### `type: vlan`

An 802.1Q VLAN subinterface may be used by `routes[].dev` and
`routing.ospf.passive_interfaces`:

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `devices.<name>.type` | Yes | None | Must be `vlan` |
| `devices.<name>.link` | Yes | None | Lower interface; must be a linked interface on the same node |
| `devices.<name>.id` | Yes | None | VLAN ID in `1..4094`, unique on the lower interface |
| `devices.<name>.addresses` | No | `[]` | Unique IPv4/IPv6 CIDR addresses assigned to the VLAN device |
| `devices.<name>.mac` | No | Automatic | Fixed unicast MAC address |

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
| `devices.<name>.mac` | No | Automatic | Fixed unicast MAC address |
| `devices.<name>.dst_port` | No | `4789` | UDP destination port in `1..65535` |
| `devices.<name>.learning` | No | `true` | Enable source-MAC learning |
| `devices.<name>.mtu` | No | Automatic | MTU bounded by underlay MTU minus encapsulation overhead |

The underlay `link` must be linked and contain the exact `local` address. The automatic MTU
subtracts 50 bytes for IPv4 or 70 bytes for IPv6. A standalone VXLAN has no bridge master, so it
can be used as `routes[].dev`, as shown in the combined VXLAN example at
`examples/vxlan/nslab.yaml`.

###### `type: dummy`

A dummy device is a namespace-local virtual interface with no physical peer. It is useful as a
stable address or route target:

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `devices.<name>.type` | Yes | None | Must be `dummy` |
| `devices.<name>.addresses` | No | `[]` | IPv4/IPv6 addresses assigned to the dummy device |
| `devices.<name>.mac` | No | Automatic | Fixed unicast MAC address |
| `devices.<name>.mtu` | No | `1500` | MTU in `576..9216` |

###### `type: geneve`

A Linux-node Geneve device is a static unicast tunnel that may carry addresses and routes. Its
source address is selected by the route to `remote`; unlike VXLAN, the manifest does not declare a
`local` field:

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `devices.<name>.type` | Yes | None | Must be `geneve` |
| `devices.<name>.vni` | Yes | None | Geneve Network Identifier in `1..16777215`, unique on the node |
| `devices.<name>.link` | Yes | None | Linked underlay interface on the same node |
| `devices.<name>.remote` | Yes | None | Static unicast IPv4/IPv6 remote VTEP address |
| `devices.<name>.dst_port` | No | `6081` | UDP destination port in `1..65535` |
| `devices.<name>.addresses` | No | `[]` | IPv4/IPv6 addresses assigned to the Geneve device |
| `devices.<name>.mac` | No | Automatic | Fixed unicast MAC address |
| `devices.<name>.mtu` | No | Automatic | MTU in `576..9216`, bounded by underlay MTU minus encapsulation overhead |

The underlay `link` must be a linked interface. IPv4 Geneve subtracts 50 bytes from the underlay
MTU; IPv6 Geneve subtracts 70 bytes. The `remote` address must be unicast. A Geneve device on a
Linux node has no bridge master and can be selected by `routes[].dev`.

###### `type: gre`

A GRE device is a static point-to-point tunnel with IPv4 outer endpoints. It may carry IPv4 or
IPv6 addresses and routes:

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `devices.<name>.type` | Yes | None | Must be `gre` |
| `devices.<name>.link` | Yes | None | Linked IPv4 underlay interface on the same node |
| `devices.<name>.local` | Yes | None | Unicast IPv4 source address configured on `link` |
| `devices.<name>.remote` | Yes | None | Static unicast IPv4 remote endpoint, different from `local` |
| `devices.<name>.key` | No | `null` | Symmetric ingress/egress key in `1..4294967295` |
| `devices.<name>.ttl` | No | `64` | Outer IPv4 TTL in `1..255` |
| `devices.<name>.addresses` | No | `[]` | IPv4/IPv6 addresses assigned to the GRE device |
| `devices.<name>.mtu` | No | Automatic | MTU in `576..9216`, bounded by encapsulation overhead |

The underlay must contain the exact `local` address. Automatic MTU subtracts 24 bytes for the
outer IPv4 and GRE headers, plus 4 bytes when `key` is present. Both ends must use the same key.
The kernel fallback names `gre0`, `gretap0`, and `erspan0` are reserved. See
`examples/ip-tunnels/nslab.yaml`.

###### `type: ipip`

An IPIP device carries IPv4 packets inside IPv4 and uses static point-to-point endpoints:

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `devices.<name>.type` | Yes | None | Must be `ipip` |
| `devices.<name>.link` | Yes | None | Linked IPv4 underlay interface on the same node |
| `devices.<name>.local` | Yes | None | Unicast IPv4 source address configured on `link` |
| `devices.<name>.remote` | Yes | None | Static unicast IPv4 remote endpoint, different from `local` |
| `devices.<name>.ttl` | No | `64` | Outer IPv4 TTL in `1..255` |
| `devices.<name>.addresses` | No | `[]` | IPv4 addresses assigned to the IPIP device |
| `devices.<name>.mtu` | No | Automatic | MTU in `576..9216`, bounded by encapsulation overhead |

Automatic MTU subtracts the 20-byte outer IPv4 header. The underlay must contain the exact
`local` address, and IPIP device addresses must be IPv4. The kernel fallback name `tunl0` is
reserved. See `examples/ip-tunnels/nslab.yaml`.

###### `type: macvlan`

A macvlan device gives a linked parent interface an additional virtual interface and MAC address:

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `devices.<name>.type` | Yes | None | Must be `macvlan` |
| `devices.<name>.link` | Yes | None | Linked parent interface on the same node |
| `devices.<name>.mode` | No | `bridge` | `private`, `vepa`, `bridge`, `passthru`, or `source` |
| `devices.<name>.addresses` | No | `[]` | IPv4/IPv6 addresses assigned to the macvlan device |
| `devices.<name>.mac` | No | Automatic | Fixed unicast MAC address |
| `devices.<name>.mtu` | No | Parent MTU | MTU in `576..9216` |

The parent must be a linked interface, not another declared device. `bridge` mode permits sibling
macvlan interfaces on the same parent to communicate; the other modes expose their corresponding
kernel isolation behavior.

###### `type: ipvlan`

An ipvlan device shares its parent's lower-layer identity while providing a separate interface:

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `devices.<name>.type` | Yes | None | Must be `ipvlan` |
| `devices.<name>.link` | Yes | None | Linked parent interface on the same node |
| `devices.<name>.mode` | No | `l2` | `l2`, `l3`, or `l3s` |
| `devices.<name>.addresses` | No | `[]` | IPv4/IPv6 addresses assigned to the ipvlan device |
| `devices.<name>.mtu` | No | Parent MTU | MTU in `576..9216` |

`l2` forwards at the Ethernet layer, while `l3` and `l3s` use IPvlan layer-3 forwarding variants.
The parent must be a linked interface and cannot be another declared device.

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
| `devices.<name>.mac` | No | Automatic | Fixed unicast MAC address for the bond |
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
appear once in each routing domain. Dynamic OSPF/BGP/PIM configuration cannot currently be combined
with VRF devices; run routing daemons explicitly through `nslab exec` for advanced VRF labs.

##### `routing`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `routing.ospf` | Conditional | `null` | OSPFv2 configuration |
| `routing.bgp` | Conditional | `null` | IPv4 eBGP configuration |
| `routing.pim` | Conditional | `null` | IPv4 PIM-SM and IGMP configuration |

At least one protocol is required when `routing` is present; protocols may be enabled together. The
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

###### `routing.pim`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `rp_address` | Yes | None | Static unicast IPv4 rendezvous point address |
| `interfaces` | Yes | None | Non-empty list of interfaces on which to enable PIM-SM |
| `igmp_interfaces` | No | `[]` | PIM interfaces on which to also enable IGMP |

Every listed interface must exist on the node and have an IPv4 address. Interface names are unique,
and every IGMP interface must also appear in `interfaces`. All PIM nodes in one topology must use
the same RP address; nslab maps it to the ASM range `224.0.0.0/4`. The RP address itself must be
reachable through the unicast routing table, normally through OSPF or BGP. On the RP node, enable
PIM on the loopback or dummy interface that owns the RP address.

FRRouting and the kernel create an internal `pimreg` interface while `pimd` is active. nslab treats
it as a runtime-managed interface during drift checks, and reserves that name on PIM nodes.

#### `kind: bridge`

A bridge node creates a Linux bridge in its own namespace. It accepts the common `interfaces`,
`routes`, and `sysctls` fields but cannot declare `routing`.

| Node field | Required | Default | Description |
| --- | --- | --- | --- |
| `devices` | No | `{}` | Static VXLAN or Geneve devices attached to this bridge |
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
| `mac` | No | Automatic | Fixed unicast MAC address |
| `dst_port` | No | `4789` | UDP destination port in `1..65535` |
| `learning` | No | `true` | Enable source-MAC learning on the VXLAN interface |
| `mtu` | No | Automatic | VXLAN MTU in `576..9216`, bounded by underlay MTU minus encapsulation overhead |

The automatic MTU subtracts 50 bytes for an IPv4 underlay or 70 bytes for IPv6. A custom value
cannot exceed that limit. `local` and `remote` cannot be equal, unspecified, or multicast. The
kernel installs a permanent all-zero-MAC FDB entry for the static remote. `bridge.ports` may name
the VXLAN device to configure STP or VLAN behavior, but it cannot name a VXLAN underlay interface.
Bridge-node VXLAN devices cannot declare `addresses`; use a Linux node for routed VXLAN.

##### `devices.<name>` with `type: geneve`

A bridge-node Geneve device creates a static unicast Layer 2 tunnel and automatically joins
`bridge.name`. It uses the route-selected source address for the underlay, so it has no `local`
field. The underlay interface remains outside the bridge:

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `type` | Yes | None | Must be `geneve` |
| `vni` | Yes | None | Geneve Network Identifier in `1..16777215`, unique on the node |
| `link` | Yes | None | Linked underlay interface on the same bridge node |
| `remote` | Yes | None | Static unicast IPv4/IPv6 remote VTEP address |
| `mac` | No | Automatic | Fixed unicast MAC address |
| `dst_port` | No | `6081` | UDP destination port in `1..65535` |
| `mtu` | No | Automatic | Geneve MTU in `576..9216`, bounded by underlay MTU minus encapsulation overhead |

The automatic MTU subtracts 50 bytes for an IPv4 underlay or 70 bytes for IPv6. A custom value
cannot exceed that limit. Bridge-node Geneve devices cannot declare `addresses`; use a Linux node
for routed Geneve. `bridge.ports` may name the Geneve device for STP or VLAN settings, but it may
not name the underlay interface.

##### `bridge`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `name` | Yes | None | Bridge device name inside the namespace; cannot be `lo` |
| `stp` | Yes | None | Enable Linux bridge STP |
| `vlan_filtering` | Yes | None | Enable VLAN-aware filtering |
| `priority` | No | `null` | Bridge priority in `0..65535` |
| `ports` | No | `{}` | Mapping from linked access, VXLAN, or Geneve port name to STP/VLAN settings |

`bridge.name` cannot collide with a linked endpoint. To assign an IP address to the bridge
itself, use the same `bridge.name` under the node's `interfaces` mapping.

###### `bridge.ports.<ifname>`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `path_cost` | No | `null` | STP path cost in `1..65535`; requires `stp: true` |
| `priority` | No | `null` | Linux STP port priority in `0..63`; requires `stp: true` |
| `hairpin` | No | `null` | Allow frames received on this port to be sent back through the same port |
| `isolated` | No | `null` | Prevent forwarding between this port and other isolated bridge ports |
| `learning` | No | `null` | Enable or disable source-MAC learning on this port |
| `flood` | No | `null` | Enable or disable unknown-unicast flooding toward this port |
| `multicast_flood` | No | `null` | Enable or disable unregistered-multicast flooding toward this port |
| `vlans` | No | `[]` | Port VLAN entries; requires `vlan_filtering: true` |

A port configuration must contain at least one STP, forwarding, or VLAN setting, and the port must
be a linked access interface or declared VXLAN/Geneve device. A `null` forwarding control leaves
the kernel default unmanaged; an explicit `true` or `false` is configured and checked for drift.

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
| `mtu` | No | `1500` | MTU on both ends in `576..9216`; at least `1280` when an endpoint carries IPv6 |
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
