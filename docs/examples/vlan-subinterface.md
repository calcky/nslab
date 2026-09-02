# VLAN subinterfaces

## Goal

Create a VLAN 10 device at each end of one veth link. IPv4 addresses exist only on `vlan10`;
the lower `eth0` devices carry 802.1Q-tagged frames.

## Graph

```console
$ nslab graph --format mermaid
flowchart LR
    n0["h1\nlinux\nvlan10: vlan 10 on eth0"]
    n1["h2\nlinux\nvlan10: vlan 10 on eth0"]
    n0 -- "eth0 <-> eth0" --- n1
```

```mermaid
flowchart LR
    n0["h1\nlinux\nvlan10: vlan 10 on eth0"]
    n1["h2\nlinux\nvlan10: vlan 10 on eth0"]
    n0 -- "eth0 <-> eth0" --- n1
```

The outputs below are representative. Interface indexes, MAC addresses, and timings vary.

## Run

```console
$ cd examples/vlan-subinterface

$ sudo nslab deploy
deployed topology: vlan-subinterface

$ sudo nslab inspect
status: deployed

NAME  KIND   STATUS    NAMESPACE
----  -----  --------  ------------------------------------
h1    linux  matching  nslab-vlan-subinterface-h1-...
h2    linux  matching  nslab-vlan-subinterface-h2-...
```

## Observe and verify

```console
$ sudo nslab exec --node h1 -- ip -d link show vlan10
<index>: vlan10@eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 ... state UP ...
    link/ether <mac> brd ff:ff:ff:ff:ff:ff
    vlan protocol 802.1Q id 10 <REORDER_HDR>

$ sudo nslab exec --node h1 -- ip -4 address show vlan10
<index>: vlan10@eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> ...
    inet 192.0.2.1/24 scope global vlan10

$ sudo nslab exec --node h1 -- ip -4 route show
192.0.2.0/24 dev vlan10 proto kernel scope link src 192.0.2.1

$ sudo nslab exec --node h1 -- ping -c 3 192.0.2.2
PING 192.0.2.2 (192.0.2.2) 56(84) bytes of data.
64 bytes from 192.0.2.2: icmp_seq=1 ttl=64 time=<time> ms
...
3 packets transmitted, 3 received, 0% packet loss
```

`vlan10@eth0` identifies `eth0` as the lower device. `id 10` is the 802.1Q VLAN ID matched on
receive and inserted on transmit.

## Clean up

```console
$ sudo nslab destroy
destroyed topology: vlan-subinterface
```

[View nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/vlan-subinterface/nslab.yaml) ·
[View example README](https://github.com/calcky/nslab/blob/main/examples/vlan-subinterface/README.md)
