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
| [Bridge VLAN](bridge-vlan.md) | Access VLANs, PVID, untagged access, and tagged trunks |
| [VLAN subinterfaces](vlan-subinterface.md) | 802.1Q VLAN devices over a veth link |
| [Bridge STP](bridge-stp.md) | Root election, port selection, and failure reconvergence |

## Link aggregation

| Example | Focus |
| --- | --- |
| [Bond active-backup](bond-active-backup.md) | Preferred-link failover and recovery |
| [Bond 802.3ad](bond-8023ad.md) | LACP negotiation and per-flow hashing |

## IP forwarding

| Example | Focus |
| --- | --- |
| [IPv4 forwarding](ipv4-forward.md) | IPv4 static routes and the Linux forwarding path |
| [IPv6 forwarding](ipv6-forward.md) | IPv6 default routes, DAD, and the Linux forwarding path |
| [Router on a stick](router-on-a-stick.md) | Inter-VLAN routing over one tagged trunk |

## Routing isolation

| Example | Focus |
| --- | --- |
| [Linux VRF](vrf.md) | Multiple routing tables, interface membership, and overlapping address spaces |
| [Policy routing](policy-routing.md) | RPDB selectors, packet marks, and per-rule routing tables |

## Link conditions

| Example | Focus |
| --- | --- |
| [netem](netem.md) | Bidirectional delay, jitter, random loss, and qdisc statistics |

## Packet processing

| Example | Focus |
| --- | --- |
| [XDP receive and transmit](xdp.md) | `XDP_PASS`, `XDP_DROP`, `XDP_TX`, `XDP_REDIRECT`, and BPF map counters |

## Dynamic routing

| Example | Focus |
| --- | --- |
| [OSPFv2](ospf.md) | Adjacencies, link-state routes, and failure convergence |
| [eBGP](bgp.md) | Peer establishment, AS_PATH propagation, and edge routing |

!!! note "Privileges and dependencies"

    `graph` does not require root. Creating namespaces, bridges, veth pairs, or qdiscs does.
    The OSPF and BGP pages also require the system `frr` and `frr-pythontools` packages. The XDP
    page requires Clang, libbpf headers, and bpftool.
