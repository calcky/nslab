# eBGP dynamic routing

## Goal

`r1`, `r2`, and `r3` belong to AS 65001, 65002, and 65003 and form an eBGP chain. The two edge
LAN prefixes propagate through BGP, exposing peer state, received prefixes, and AS_PATH.

## Graph

```console
$ nslab graph
Topology: bgp

r1 [linux]
  eth0: 10.1.12.1/30
  eth1: 198.18.1.1/24
├─ eth1 ↔ eth0  h1 [linux]
│               eth0: 198.18.1.2/24
└─ eth0 ↔ eth0  r2 [linux]
                eth0: 10.1.12.2/30
                eth1: 10.1.23.1/30
                └─ eth1 ↔ eth0  r3 [linux]
                                eth0: 10.1.23.2/30
                                eth1: 198.18.3.1/24
                                └─ eth1 ↔ eth0  h2 [linux]
                                                eth0: 198.18.3.2/24
```

## Prepare and run

```bash
sudo apt install -y frr frr-pythontools
cd examples/bgp
sudo nslab deploy
sudo nslab inspect
```

## Inspect peers and routes

Each node uses an independent FRR pathspace. For `r2`:

```bash
sudo nslab exec --node r2 -- vtysh -N nslab-bgp-r2 -c "show ip bgp summary"
sudo nslab exec --node r2 -- vtysh -N nslab-bgp-r2 -c "show ip bgp"
sudo nslab exec --node r2 -- ip -4 route
```

After both sessions establish, `r2` should receive `198.18.1.0/24` and `198.18.3.0/24` from
opposite sides. Verify end-to-end forwarding:

```bash
sudo nslab exec --node h1 -- ping -c 3 198.18.3.2
sudo nslab exec --node r1 -- vtysh -N nslab-bgp-r1 -c "show ip bgp"
```

A successful `deploy` only means the daemons started. Use `show ip bgp summary` to confirm peer
convergence.

## Clean up

```bash
sudo nslab destroy
```

[View nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/bgp/nslab.yaml) ·
[View example README](https://github.com/calcky/nslab/blob/main/examples/bgp/README.md)
