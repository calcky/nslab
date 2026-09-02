# eBGP dynamic routing

## Goal

`r1`, `r2`, and `r3` belong to AS 65001, 65002, and 65003 and form an eBGP chain. The two edge
LAN prefixes propagate through BGP, exposing peer state, received prefixes, and AS_PATH.

## Graph

```bash
nslab graph --format mermaid
```

```mermaid
flowchart LR
    n0["h1\nlinux"]
    n1["r1\nlinux"]
    n2["r2\nlinux"]
    n3["r3\nlinux"]
    n4["h2\nlinux"]
    n0 -- "eth0 <-> eth1" --- n1
    n1 -- "eth0 <-> eth0" --- n2
    n2 -- "eth1 <-> eth0" --- n3
    n3 -- "eth1 <-> eth0" --- n4
```

The outputs below are representative. FRR timers, message counters, interface indexes, and
ICMP timings vary per run.

## Prepare and run

```bash
sudo apt install -y frr frr-pythontools
cd examples/bgp
```

```console
$ sudo nslab deploy
deployed topology: bgp

$ sudo nslab inspect
status: deployed

NAME  KIND   STATUS    NAMESPACE
----  -----  --------  ---------------------
h1    linux  matching  nslab-bgp-h1-...
r1    linux  matching  nslab-bgp-r1-...
r2    linux  matching  nslab-bgp-r2-...
r3    linux  matching  nslab-bgp-r3-...
h2    linux  matching  nslab-bgp-h2-...
```

## Inspect peers and routes

```console
$ sudo nslab exec --node r2 -- vtysh -N nslab-bgp-r2 -c "show ip bgp summary"
IPv4 Unicast Summary:
BGP router identifier 2.2.2.2, local AS number 65002
Neighbor     V  AS     MsgRcvd  MsgSent  Up/Down  State/PfxRcd
10.1.12.1   4  65001  <count>  <count>  <time>   1
10.1.23.2   4  65003  <count>  <count>  <time>   1

$ sudo nslab exec --node r2 -- vtysh -N nslab-bgp-r2 -c "show ip bgp"
...
*> 198.18.1.0/24  10.1.12.1  0 65001 i
*> 198.18.3.0/24  10.1.23.2  0 65003 i

$ sudo nslab exec --node r2 -- ip -4 route
...
198.18.1.0/24 via 10.1.12.1 dev eth0 proto bgp metric 20
198.18.3.0/24 via 10.1.23.2 dev eth1 proto bgp metric 20

$ sudo nslab exec --node h1 -- ping -c 3 198.18.3.2
64 bytes from 198.18.3.2: icmp_seq=1 ttl=61 time=<time> ms
...
3 packets transmitted, 3 received, 0% packet loss

$ sudo nslab exec --node r1 -- vtysh -N nslab-bgp-r1 -c "show ip bgp"
...
*> 198.18.3.0/24  10.1.12.2  0 65002 65003 i
```

## Observe route withdrawal

```bash
sudo nslab exec --node r2 -- ip link set eth0 down
sleep 5
```

```console
$ sudo nslab exec --node r2 -- vtysh -N nslab-bgp-r2 -c "show ip bgp summary"
Neighbor     V  AS     MsgRcvd  MsgSent  Up/Down  State/PfxRcd
10.1.12.1   4  65001  <count>  <count>  <time>   Active
10.1.23.2   4  65003  <count>  <count>  <time>   1

$ sudo nslab exec --node r2 -- ip -4 route
...
198.18.3.0/24 via 10.1.23.2 dev eth1 proto bgp metric 20
```

```bash
sudo nslab exec --node r2 -- ip link set eth0 up
```

## Clean up

```console
$ sudo nslab destroy
destroyed topology: bgp
```

[View nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/bgp/nslab.yaml) ·
[View example README](https://github.com/calcky/nslab/blob/main/examples/bgp/README.md)
