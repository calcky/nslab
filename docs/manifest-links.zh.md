# Manifest：链路

`topology.links` 的每一项描述一对 veth，`endpoints` 使用
`node:interface` 格式。可选字段为 `mtu`、`netem` 和 `qdisc`；同一链路不能同时
声明 `netem` 与 `qdisc`。

```yaml
links:
  - endpoints: [h1:eth0, h2:eth0]
    mtu: 1500
    netem:
      delay_ms: 20
      loss_percent: 1
```

详见[链路字段](manifest.zh.md#topology)和[qdisc 章节](manifest-qdisc.zh.md)。
