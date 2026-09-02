# Linux IPv4 forwarding

## Goal

`r1` connects two IPv4 subnets with `net.ipv4.ip_forward=1`. Each host uses an exact static
route to the remote subnet, exposing Linux route selection and the forwarding path.

## Graph

```console
$ nslab graph
Topology: ipv4-forward

r1 [linux]
  eth0: 192.0.2.1/24
  eth1: 198.51.100.1/24
├─ eth0 ↔ eth0  h1 [linux]
│               eth0: 192.0.2.2/24
└─ eth1 ↔ eth0  h2 [linux]
                eth0: 198.51.100.2/24
```

## Run

```bash
cd examples/ipv4-forward
sudo nslab deploy
sudo nslab inspect
```

## Observe routes and forwarding

```bash
sudo nslab exec --node r1 -- cat /proc/sys/net/ipv4/ip_forward
sudo nslab exec --node r1 -- ip -4 address show
sudo nslab exec --node h1 -- ip -4 route show
sudo nslab exec --node h1 -- ip -4 route get 198.51.100.2
sudo nslab exec --node h2 -- ip -4 route show
```

The forwarding switch should be `1`; the next hop from `h1` to `198.51.100.0/24` should be
`192.0.2.1`.

## Verify connectivity

```bash
sudo nslab exec --node h1 -- ping -c 3 198.51.100.2
sudo nslab exec --node h2 -- ping -c 3 192.0.2.2
sudo nslab exec --node r1 -- ip -s link show eth0
sudo nslab exec --node r1 -- ip -s link show eth1
```

TTL decreases after an ICMP packet crosses `r1`, and both router interface counters increase.

## Clean up

```bash
sudo nslab destroy
```

[View nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/ipv4-forward/nslab.yaml) ·
[View example README](https://github.com/calcky/nslab/blob/main/examples/ipv4-forward/README.md)
