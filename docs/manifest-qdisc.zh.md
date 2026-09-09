# Manifest：qdisc

## 模型

在每个 `topology.links[]` 项下配置一个 `qdisc`。nslab 会将相同配置安装到 veth 两端的
egress。`qdisc` 与 `netem` 互斥。

```yaml
links:
  - endpoints: [h1:eth0, h2:eth0]
    qdisc:
      kind: htb
      rate: 20mbit
      leaf:
        kind: fq_codel
        target_ms: 5
        interval_ms: 100
```

| 分组 | 配置 |
| --- | --- |
| 单队列 | `HTB + pfifo`、`HTB + red`、`HTB + codel`、`HTB + pie` |
| 自动流隔离 | `HTB + sfq`、`HTB + fq`、`HTB + fq_codel`、`HTB + fq_pie` |
| 完整方案 | 自带整形和 AQM 的 `CAKE` |

## HTB 子队列

| kind | 主要字段 | 用途 |
| --- | --- | --- |
| `pfifo` | `limit` | 按报文数限制的 FIFO |
| `red` | `limit`、`min`、`max`、`avpkt`、`burst`、`probability`、`ecn` | 平均队列 AQM，阈值为字节 |
| `codel` | `limit`、`target_ms`、`interval_ms`、`ecn` | 单队列延迟 AQM |
| `pie` | `limit`、`target_ms`、`tupdate_ms`、`alpha`、`beta`、`ecn`、`bytemode` | 单队列概率 AQM |
| `sfq` | `limit`、`quantum`、`perturb`、`flows` | 基于 hash 的随机流隔离 |
| `fq` | `limit`、`quantum` | 流调度和 socket pacing |
| `fq_codel` | `limit`、`target_ms`、`interval_ms`、`ecn` | 流隔离加 CoDel |
| `fq_pie` | `limit`、`target_ms`、`tupdate_ms`、`alpha`、`beta`、`ecn`、`bytemode` | 流隔离加 PIE |

示例：

```yaml
qdisc:
  kind: htb
  rate: 20mbit
  leaf:
    kind: fq_codel
    target_ms: 5
    interval_ms: 100
    limit: 10240
    ecn: true
```

`pie` 和 `fq_pie` 需要内核支持 `sch_pie` 且系统 `tc` 版本兼容。缺少支持时返回
`QDISC_UNSUPPORTED`。所有 qdisc 参数在目标 namespace 内通过 `tc` 配置，拓扑操作仍由
pyroute2 完成。

## 根 qdisc

上表中的所有 qdisc 都可以直接作为根 qdisc。FIFO、公平队列和 AQM 类型不提供总带宽整形，
`tbf` 和 `cake` 提供整形。`tbf` 使用 `rate`、`burst`、
`latency_ms`；`cake` 使用 `bandwidth`、`flow_mode`、`diffserv_mode`、`rtt_ms`、`nat`。

```yaml
qdisc:
  kind: cake
  bandwidth: 20mbit
  flow_mode: flows
  diffserv_mode: besteffort
  rtt_ms: 100
  nat: false
```

完整的部署、流量、计数和清理流程见[qdisc 示例](examples/qdisc.zh.md)。
