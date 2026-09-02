# OSPFv2 dynamic routing

## Goal

Three Linux routers run independent FRRouting `zebra` and `ospfd` processes in an OSPFv2
triangle. The direct `r1`-to-`r3` link provides a backup path for observing neighbors, learned
routes, and failure convergence.

## Graph

```console
$ nslab graph
Topology: ospf

r1 [linux]
  eth0: 10.0.12.1/30
  eth1: 10.0.13.1/30
  eth2: 192.0.1.1/24
├─ eth2 ↔ eth0  h1 [linux]
│               eth0: 192.0.1.2/24
├─ eth0 ↔ eth0  r2 [linux]
│               eth0: 10.0.12.2/30
│               eth1: 10.0.23.1/30
└─ eth1 ↔ eth1  r3 [linux]
                eth0: 10.0.23.2/30
                eth1: 10.0.13.2/30
                eth2: 192.0.3.1/24
                └─ eth2 ↔ eth0  h2 [linux]
                                eth0: 192.0.3.2/24
Cross-links:
  ↩ [L2] r2:eth1 ↔ r3:eth0
```

## Prepare and run

```bash
sudo apt install -y frr frr-pythontools
cd examples/ospf
sudo nslab deploy
sudo nslab inspect
```

`deploy` waits for daemon startup, not OSPF neighbor convergence.

## Inspect neighbors and routes

```bash
sudo nslab exec --node r1 -- vtysh -N nslab-ospf-r1 -c "show ip ospf neighbor"
sudo nslab exec --node r1 -- vtysh -N nslab-ospf-r1 -c "show ip route ospf"
sudo nslab exec --node r1 -- ip -4 route
sudo nslab exec --node h1 -- ping -c 3 192.0.3.2
```

After neighbors reach `Full`, `r1` should install an OSPF route to `192.0.3.0/24`.

## Observe failure convergence

```bash
sudo nslab exec --node r1 -- ip link set eth0 down
sleep 10
sudo nslab exec --node r1 -- vtysh -N nslab-ospf-r1 -c "show ip ospf neighbor"
sudo nslab exec --node r1 -- ip -4 route get 192.0.3.2
sudo nslab exec --node h1 -- ping -c 3 192.0.3.2
sudo nslab exec --node r1 -- ip link set eth0 up
```

## Clean up

```bash
sudo nslab destroy
```

[View nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/ospf/nslab.yaml) ·
[View example README](https://github.com/calcky/nslab/blob/main/examples/ospf/README.md)
