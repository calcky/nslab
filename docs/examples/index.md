# Examples

Every example has its own page with actual `nslab graph` output and commands for deployment,
observation, and cleanup. Its directory also contains a repeatable `nslab.yaml` and README.

Common workflow:

```bash
cd examples/<lab>
nslab graph
sudo nslab deploy
sudo nslab inspect
# Run the observation commands from the example page
sudo nslab destroy
```

## Layer 2 switching

| Example | Focus |
| --- | --- |
| [Bridge FDB](bridge-fdb.md) | Linux bridge forwarding, MAC learning, and counters |
| [Bridge port controls](bridge-port-controls.md) | Port isolation, hairpin, learning, and flood controls |
| [Bridge VLAN](bridge-vlan.md) | Access VLANs, PVID, untagged access, and tagged trunks |
| [VLAN](vlan.md) | 802.1Q subinterfaces and router-on-a-stick forwarding |
| [Bridge STP](bridge-stp.md) | Root election, port selection, and failure reconvergence |
| [VXLAN](vxlan.md) | Layer 2 bridge overlay and standalone Layer 3 routing |
| [Geneve](geneve.md) | Static unicast Layer 2 Geneve overlay |

## Namespace interfaces

| Example | Focus |
| --- | --- |
| [Virtual devices](virtual-devices.md) | `dummy`, `macvlan`, and `ipvlan` interfaces |

## Tunnel devices

| Example | Focus |
| --- | --- |
| [GRE and IPIP](ip-tunnels.md) | Keyed GRE and IPv4-in-IPv4 point-to-point tunnels |

## Link aggregation

| Example | Focus |
| --- | --- |
| [Bond overview](bond.md) | Compare bonding modes and choose a runnable lab |
| [Bond active-backup](bond-active-backup.md) | Preferred-link failover and recovery |
| [Bond 802.3ad](bond-8023ad.md) | LACP negotiation and per-flow hashing |

## IP forwarding

| Example | Focus |
| --- | --- |
| [IPv4 forwarding](ipv4-forward.md) | IPv4 static routes and the Linux forwarding path |
| [IPv6 forwarding](ipv6-forward.md) | IPv6 default routes, DAD, and the Linux forwarding path |
| [MTU and PMTU](pmtu.md) | IPv4 fragmentation, PMTU learning, and IPv6 Packet Too Big |
| [Neighbor tables](neighbors.md) | Fixed MAC addresses, static ARP/NDP, and proxying |
| [ECMP](ecmp.md) | Static equal-cost next hops, weights, and per-flow hashing |

## Routing isolation

| Example | Focus |
| --- | --- |
| [Linux VRF](vrf.md) | Multiple routing tables, interface membership, and overlapping address spaces |
| [Policy routing](policy-routing.md) | RPDB selectors, packet marks, and per-rule routing tables |

## Link conditions

| Example | Focus |
| --- | --- |
| [netem](netem.md) | Bidirectional delay, jitter, random loss, and qdisc statistics |
| [qdisc](qdisc.md) | netem, TBF, fq_codel, and HTB with an fq_codel leaf |
| [CAKE](cake.md) | Integrated shaping, per-flow fairness, and AQM |

## Packet processing

| Example | Focus |
| --- | --- |
| [XDP receive and transmit](xdp.md) | `XDP_PASS`, `XDP_DROP`, `XDP_TX`, `XDP_REDIRECT`, and BPF map counters |

## Dynamic routing

| Example | Focus |
| --- | --- |
| [OSPFv2](ospf.md) | Adjacencies, link-state routes, and failure convergence |
| [eBGP](bgp.md) | Peer establishment, AS_PATH propagation, and edge routing |
| [PIM-SM and IGMP](pim.md) | Static RP, multicast RPF, IGMP membership, and packet replication |

!!! note "Privileges and dependencies"

    `graph` does not require root. Creating namespaces, bridges, veth pairs, or qdiscs does.
    The OSPF, BGP, and PIM pages also require the system `frr` and `frr-pythontools` packages. The XDP
    page requires Clang, libbpf headers, and bpftool. The CAKE page requires kernel support for
    `sch_cake`.
