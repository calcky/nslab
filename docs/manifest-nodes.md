# Manifest: nodes

Nodes are declared under `topology.nodes`. The supported kinds are `linux` and `bridge`.
Common fields are `interfaces`, `routes`, `neighbors`, `sysctls`, and (for Linux nodes)
`routing`.

```yaml
topology:
  nodes:
    h1:
      kind: linux
      interfaces:
        eth0:
          addresses: [10.0.0.1/24]
    sw1:
      kind: bridge
      bridge:
        name: br0
  links: []
```

Namespace-local devices such as `vlan`, `vrf`, `bond`, `gre`, `ipip`, `vxlan`,
`geneve`, `dummy`, `macvlan`, and `ipvlan` belong under a Linux node's `devices`.
See the [complete reference](manifest.md#topologynodes) for each device's fields.
