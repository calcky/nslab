# Linux netem link conditions

## Goal

Install an egress netem qdisc at both ends of a veth to observe link delay, jitter, random loss,
and qdisc statistics independently. This is a link-condition lab and does not use IP forwarding.

## Graph

```console
$ nslab graph
Topology: netem

h1 [linux]
  eth0: 10.30.0.1/24
└─ eth0 ↔ eth0  h2 [linux]
                eth0: 10.30.0.2/24
```

The link configures `100ms` delay, `10ms` jitter, and `5%` loss independently in both directions.

## Run

```bash
cd examples/netem
sudo nslab deploy
sudo nslab inspect
```

## Observe qdiscs

```bash
sudo nslab exec --node h1 -- tc -s qdisc show dev eth0
sudo nslab exec --node h2 -- tc -s qdisc show dev eth0
```

Both ends should show the netem settings. An echo request experiences egress delay at `h1` and
the reply experiences it again at `h2`, so RTT is centered around `200ms`.

## Generate traffic

```bash
sudo nslab exec --node h1 -- ping -c 20 -i 0.2 10.30.0.2
sudo nslab exec --node h2 -- ping -c 20 -i 0.2 10.30.0.1
```

A small sample may happen to lose no packets. Change `delay_ms`, `jitter_ms`, or `loss_percent`
and run `sudo nslab redeploy` to compare results.

## Clean up

```bash
sudo nslab destroy
```

[View nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/netem/nslab.yaml) ·
[View example README](https://github.com/calcky/nslab/blob/main/examples/netem/README.md)
