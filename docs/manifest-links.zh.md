# Manifest：链路

### `topology.links`

每条链路连接两个 `node:interface` endpoint。一个 endpoint 在整个拓扑中只能使用一次；
节点和接口引用都必须有效，且不能使用 `lo`。

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `kind` | 否 | `veth` | 链路类型，目前只能是 `veth` |
| `endpoints` | 是 | 无 | 恰好两个 `node:interface` 字符串 |
| `mtu` | 否 | `1500` | 两端 MTU，范围 `576..9216`；端点承载 IPv6 时不得小于 `1280` |
| `netem` | 否 | `null` | 同时应用到两端 egress 的 netem 条件；不能与 `qdisc` 同时使用 |
| `qdisc` | 否 | `null` | 两端各自的 egress 队列配置；root 和 HTB leaf 配置见 [Qdisc](manifest-qdisc.md) |

```yaml
links:
  - endpoints: [h1:eth0, h2:eth0]
    mtu: 1500
```

#### `links[].netem`

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `delay_ms` | 否 | `0` | 单端 egress 延迟，范围 `0..60000` ms |
| `jitter_ms` | 否 | `0` | 延迟抖动，范围 `0..60000` ms；要求 `delay_ms > 0` |
| `loss_percent` | 否 | `0` | 随机丢包率，范围 `0..100` 的整数百分比 |
| `rate` | 否 | `null` | 可选 egress 速率，例如 `10mbit`、`500kbit` 或 `1gbit` |

四个值不能同时为零。netem 同时安装在 veth 两端，因此双向 ping 会在 request 和 reply
方向各经历一次 egress 条件。

```yaml
netem:
  rate: 10mbit
  delay_ms: 20
  jitter_ms: 5
  loss_percent: 1
```
