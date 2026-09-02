# Linux bridge VLAN

## Goal

Two VLAN-aware bridges carry VLANs 10 and 20 over a tagged trunk. All four hosts deliberately
share one IPv4 subnet so that layer 2 VLAN isolation is directly visible.

## Graph

```console
$ nslab graph
Topology: bridge-vlan

sw1 [bridge · br0]
├─ access10 ↔ eth0  h10a [linux]
│                   eth0: 10.0.0.1/24
├─ access20 ↔ eth0  h20a [linux]
│                   eth0: 10.0.0.3/24
└─ trunk ↔ trunk  sw2 [bridge · br0]
                  ├─ access10 ↔ eth0  h10b [linux]
                  │                   eth0: 10.0.0.2/24
                  └─ access20 ↔ eth0  h20b [linux]
                                      eth0: 10.0.0.4/24
```

`nslab graph --detail` also shows each port's PVID, untagged state, and trunk VLANs.

## Run

```bash
cd examples/bridge-vlan
sudo nslab deploy
sudo nslab inspect
```

## Observe and verify

```bash
sudo nslab exec --node sw1 -- bridge vlan show
sudo nslab exec --node sw2 -- bridge vlan show
sudo nslab exec --node h10a -- ping -c 3 10.0.0.2
sudo nslab exec --node h20a -- ping -c 3 10.0.0.4
```

Traffic in the same VLAN should cross the trunk. The cross-VLAN ping below should time out
because ARP broadcasts do not cross VLAN boundaries:

```bash
sudo nslab exec --node h10a -- ping -c 2 -W 1 10.0.0.3
sudo nslab exec --node sw1 -- bridge fdb show br br0
```

## Clean up

```bash
sudo nslab destroy
```

[View nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/bridge-vlan/nslab.yaml) ·
[View example README](https://github.com/calcky/nslab/blob/main/examples/bridge-vlan/README.md)
