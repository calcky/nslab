# Manifest: routing

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
