# Linux bridge FDB

## Goal

Use two hosts and one Linux bridge to observe layer 2 forwarding, dynamic MAC learning, and
bridge port counters.

## Graph

```bash
nslab graph --format mermaid
```

```mermaid
flowchart LR
    n0["h1\nlinux"]
    n1["sw1\nbridge"]
    n2["h2\nlinux"]
    n0 -- "eth0 <-> swp1" --- n1
    n2 -- "eth0 <-> swp2" --- n1
```

The outputs below are representative. Interface indexes, MAC addresses, and counters vary per
run.

## Run

```bash
cd examples/bridge-fdb
```

```console
$ sudo nslab deploy
deployed topology: bridge-fdb

$ sudo nslab inspect
status: deployed

NAME  KIND    STATUS    NAMESPACE
----  ------  --------  ----------------------------
h1    linux   matching  nslab-bridge-fdb-h1-...
sw1   bridge  matching  nslab-bridge-fdb-sw1-...
h2    linux   matching  nslab-bridge-fdb-h2-...

$ sudo nslab deploy
topology already deployed: bridge-fdb
```

## Observe the FDB

Read the initial FDB, then generate ICMP traffic:

```console
$ sudo nslab exec --node sw1 -- bridge fdb show br br0
... dev swp1 self permanent
... dev swp2 self permanent

$ sudo nslab exec --node h1 -- ping -c 3 10.10.0.2
PING 10.10.0.2 (10.10.0.2) 56(84) bytes of data.
64 bytes from 10.10.0.2: icmp_seq=1 ttl=64 time=<time> ms
...
3 packets transmitted, 3 received, 0% packet loss

$ sudo nslab exec --node sw1 -- bridge fdb show br br0
<h1-mac> dev swp1 master br0
<h2-mac> dev swp2 master br0
...
```

The port counters increase with the generated traffic:

```console
$ sudo nslab exec --node sw1 -- ip -s link show swp1
... swp1 ... state UP ...
    RX:  bytes  packets  errors  dropped  missed  mcast
         ...    ...      0       0        0       ...
    TX:  bytes  packets  errors  dropped  carrier  collsns
         ...    ...      0       0        0        0

$ sudo nslab exec --node sw1 -- ip -s link show swp2
... swp2 ... state UP ...
    RX:  bytes  packets  errors  dropped  missed  mcast
         ...    ...      0       0        0       ...
    TX:  bytes  packets  errors  dropped  carrier  collsns
         ...    ...      0       0        0        0
```

## Clean up

```console
$ sudo nslab destroy
destroyed topology: bridge-fdb

$ sudo nslab destroy
topology already absent: bridge-fdb
```

[View nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/bridge-fdb/nslab.yaml) ·
[View example README](https://github.com/calcky/nslab/blob/main/examples/bridge-fdb/README.md)
