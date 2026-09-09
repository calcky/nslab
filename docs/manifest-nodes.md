# Manifest: nodes

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
| `routing` | No | `null` | [OSPF/BGP/PIM configuration](manifest-routing.md#routing), allowed only on `linux` nodes |

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
