# Linux IPv6 forwarding

## Goal

`r1` connects two `/64` networks with `net.ipv6.conf.all.forwarding=1`. Both hosts use explicit
IPv6 default routes to reach the remote network.

## Graph

```console
$ nslab graph
Topology: ipv6-forward

r1 [linux]
  eth0: 2001:db8:1::1/64
  eth1: 2001:db8:2::1/64
├─ eth0 ↔ eth0  h1 [linux]
│               eth0: 2001:db8:1::2/64
└─ eth1 ↔ eth0  h2 [linux]
                eth0: 2001:db8:2::2/64
```

## Run

```bash
cd examples/ipv6-forward
sudo nslab deploy
sleep 2
sudo nslab inspect
```

The short wait lets the kernel finish IPv6 duplicate address detection (DAD).

## Observe routes and forwarding

```bash
sudo nslab exec --node r1 -- cat /proc/sys/net/ipv6/conf/all/forwarding
sudo nslab exec --node r1 -- ip -6 address show
sudo nslab exec --node h1 -- ip -6 route show
sudo nslab exec --node h1 -- ip -6 route get 2001:db8:2::2
sudo nslab exec --node h2 -- ip -6 route show
```

The forwarding switch should be `1`. Kernel-generated link-local addresses are not part of the
declared manifest state.

## Verify connectivity

```bash
sudo nslab exec --node h1 -- ping -6 -c 3 2001:db8:2::2
sudo nslab exec --node h2 -- ping -6 -c 3 2001:db8:1::2
sudo nslab exec --node r1 -- ip -s link show eth0
sudo nslab exec --node r1 -- ip -s link show eth1
```

The hop limit decreases after an ICMPv6 packet crosses `r1`.

## Clean up

```bash
sudo nslab destroy
```

[View nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/ipv6-forward/nslab.yaml) ·
[View example README](https://github.com/calcky/nslab/blob/main/examples/ipv6-forward/README.md)
