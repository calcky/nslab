
# Manifest overview

`nslab.yaml` uses a strict schema. Unknown fields, incorrect types, and invalid references are
rejected before any kernel network resource is changed. The complete hierarchy is:

```text
version
name
topology
├─ nodes
│  └─ <node-name>
│     ├─ kind: linux
│     │  ├─ interfaces / devices / routes → nexthops / neighbors / rules / sysctls
│     │  ├─ devices → <device-name> → type: vlan | vrf | bond | gre | ipip | vxlan | dummy | geneve | macvlan | ipvlan
│     │  └─ routing
│     └─ kind: bridge
│        ├─ interfaces / devices / routes / neighbors / sysctls
│        ├─ devices → <device-name> → type: vxlan | geneve
│        └─ bridge → ports → vlans
└─ links
   └─ <link>
      ├─ endpoints / mtu
      ├─ netem
      │  └─ delay_ms / jitter_ms / loss_percent / rate
      └─ qdisc
         ├─ kind: tbf | fq_codel | htb | cake
         └─ leaf → kind: fq_codel (htb only)
```

## Top-level fields

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `version` | Yes | None | Schema version; currently only integer `1` |
| `name` | Yes | None | Deployment name matching `^[a-z][a-z0-9_-]{0,31}$` |
| `topology` | Yes | None | Topology object containing `nodes` and `links` |

```yaml
version: 1
name: my-lab
topology:
  nodes: {}
  links: []
```

## `topology`

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `topology.nodes` | Yes | Mapping | Keys are node names; values are Linux or bridge nodes |
| `topology.links` | Yes | List | Each item describes a two-ended veth link |
