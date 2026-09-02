# Linux bridge STP

## Goal

Four Linux bridges form a redundant layer 2 topology for observing root election, port priority
on equal-cost links, path cost, and STP reconvergence after a link failure.

## Graph

```console
$ nslab graph
Topology: bridge-stp

sw1 [bridge · br0]
├─ host1 ↔ eth0  h1 [linux]
│                eth0: 10.20.0.1/24
├─ swp1 ↔ swp1  sw2 [bridge · br0]
│               └─ swp3 ↔ swp1  sw4 [bridge · br0]
│                               └─ host1 ↔ eth0  h2 [linux]
│                                                eth0: 10.20.0.2/24
└─ swp3 ↔ swp1  sw3 [bridge · br0]
Cross-links:
  ↩ [L2] sw1:swp2 ↔ sw2:swp2
  ↩ [L5] sw3:swp2 ↔ sw4:swp2
```

`nslab graph --detail` also prints bridge priority, port priority, and path cost.

## Run and wait for convergence

```bash
cd examples/bridge-stp
sudo nslab deploy
sudo nslab inspect
sleep 35
```

Classic Linux bridge STP can take about 30 seconds to enter forwarding state initially.

## Observe port roles

```bash
sudo nslab exec --node sw1 -- ip -d link show br0
sudo nslab exec --node sw2 -- bridge -d link show
sudo nslab exec --node sw4 -- bridge -d link show
sudo nslab exec --node h1 -- ping -c 3 10.20.0.2
```

`sw1` should become the root bridge. Normally `sw4:swp1` forwards and the higher-cost
`sw4:swp2` blocks.

## Verify failover

```bash
sudo nslab exec --node sw4 -- ip link set swp1 down
sleep 35
sudo nslab exec --node sw4 -- bridge -d link show
sudo nslab exec --node h2 -- ping -c 1 10.20.0.1
sudo nslab exec --node h1 -- ping -c 3 10.20.0.2
sudo nslab exec --node sw4 -- ip link set swp1 up
```

An `inspect` status of `degraded` is expected while the port is down. Sending from `h2` helps
the new forwarding path relearn MAC addresses.

## Clean up

```bash
sudo nslab destroy
```

[View nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/bridge-stp/nslab.yaml) ·
[View example README](https://github.com/calcky/nslab/blob/main/examples/bridge-stp/README.md)
