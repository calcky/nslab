# Linux bridge FDB

## Goal

Use two hosts and a Linux bridge to observe layer 2 forwarding, dynamic MAC learning, and bridge
port counters.

## Graph

```console
$ nslab graph
Topology: bridge-fdb

sw1 [bridge · br0]
├─ swp1 ↔ eth0  h1 [linux]
│               eth0: 10.10.0.1/24
└─ swp2 ↔ eth0  h2 [linux]
                eth0: 10.10.0.2/24
```

## Run

```bash
cd examples/bridge-fdb
sudo nslab deploy
sudo nslab inspect
```

## Observe the FDB

Read the initial FDB, generate ICMP traffic, and inspect learned entries and counters:

```bash
sudo nslab exec --node sw1 -- bridge fdb show br br0
sudo nslab exec --node h1 -- ping -c 3 10.10.0.2
sudo nslab exec --node sw1 -- bridge fdb show br br0
sudo nslab exec --node sw1 -- ip -s link show swp1
sudo nslab exec --node sw1 -- ip -s link show swp2
```

After receiving frames, `sw1` associates each source MAC with its ingress port. RX/TX counters
on `swp1` and `swp2` also increase.

## Clean up

```bash
sudo nslab destroy
```

[View nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/bridge-fdb/nslab.yaml) ·
[View example README](https://github.com/calcky/nslab/blob/main/examples/bridge-fdb/README.md)
