# Manifest: links

Each item in `topology.links` describes a veth pair. Its `endpoints` are
`node:interface` names. `mtu`, `netem`, and `qdisc` are optional. A link cannot
declare both `netem` and `qdisc`.

```yaml
links:
  - endpoints: [h1:eth0, h2:eth0]
    mtu: 1500
    netem:
      delay_ms: 20
      loss_percent: 1
```

See [link fields](manifest.md#topology) and the [qdisc chapter](manifest-qdisc.md).
