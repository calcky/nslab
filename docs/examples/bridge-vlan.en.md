# Linux bridge VLAN

## Goal

Two VLAN-aware bridges carry VLANs 10 and 20 over a tagged trunk. All four hosts deliberately
share one IPv4 subnet so that layer 2 VLAN isolation is directly visible.

## Graph

```bash
nslab graph --format mermaid
```

```mermaid
flowchart LR
    n0["h10a\nlinux"]
    n1["sw1\nbridge"]
    n2["h20a\nlinux"]
    n3["sw2\nbridge"]
    n4["h10b\nlinux"]
    n5["h20b\nlinux"]
    n0 -- "eth0 <-> access10" --- n1
    n2 -- "eth0 <-> access20" --- n1
    n1 -- "trunk <-> trunk" --- n3
    n3 -- "access10 <-> eth0" --- n4
    n3 -- "access20 <-> eth0" --- n5
```

The outputs below are representative. Interface indexes, MAC addresses, and counters vary per
run.

## Run

```bash
cd examples/bridge-vlan
```

```console
$ sudo nslab deploy
deployed topology: bridge-vlan

$ sudo nslab inspect
status: deployed

NAME  KIND    STATUS    NAMESPACE
----  ------  --------  -----------------------------
h10a  linux   matching  nslab-bridge-vlan-h10a-...
sw1   bridge  matching  nslab-bridge-vlan-sw1-...
h20a  linux   matching  nslab-bridge-vlan-h20a-...
sw2   bridge  matching  nslab-bridge-vlan-sw2-...
h10b  linux   matching  nslab-bridge-vlan-h10b-...
h20b  linux   matching  nslab-bridge-vlan-h20b-...
```

## Observe and verify

```console
$ sudo nslab exec --node sw1 -- bridge vlan show
port      vlan-id
access10  10 PVID Egress Untagged
access20  20 PVID Egress Untagged
trunk     10
          20

$ sudo nslab exec --node sw2 -- bridge vlan show
port      vlan-id
trunk     10
          20
access10  10 PVID Egress Untagged
access20  20 PVID Egress Untagged

$ sudo nslab exec --node h10a -- ping -c 3 10.0.0.2
64 bytes from 10.0.0.2: icmp_seq=1 ttl=64 time=<time> ms
...
3 packets transmitted, 3 received, 0% packet loss

$ sudo nslab exec --node h20a -- ping -c 3 10.0.0.4
64 bytes from 10.0.0.4: icmp_seq=1 ttl=64 time=<time> ms
...
3 packets transmitted, 3 received, 0% packet loss
```

The cross-VLAN ping fails as expected:

```console
$ sudo nslab exec --node h10a -- ping -c 2 -W 1 10.0.0.3
From 10.0.0.1 icmp_seq=1 Destination Host Unreachable
From 10.0.0.1 icmp_seq=2 Destination Host Unreachable
2 packets transmitted, 0 received, +2 errors, 100% packet loss

$ sudo nslab exec --node sw1 -- bridge fdb show br br0
<h10a-mac> dev access10 vlan 10 master br0
<h10b-mac> dev trunk vlan 10 master br0
<h20a-mac> dev access20 vlan 20 master br0
<h20b-mac> dev trunk vlan 20 master br0
...

$ sudo nslab exec --node sw2 -- bridge fdb show br br0
<h10a-mac> dev trunk vlan 10 master br0
<h10b-mac> dev access10 vlan 10 master br0
<h20a-mac> dev trunk vlan 20 master br0
<h20b-mac> dev access20 vlan 20 master br0
...
```

## Clean up

```console
$ sudo nslab destroy
destroyed topology: bridge-vlan
```

[View nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/bridge-vlan/nslab.yaml) ·
[View example README](https://github.com/calcky/nslab/blob/main/examples/bridge-vlan/README.md)
