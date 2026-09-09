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

`pie` 和 `fq_pie` 分别需要内核支持 `sch_pie` 和 `sch_fq_pie`，且系统 `tc` 版本兼容。
缺少内核支持时返回 `QDISC_UNSUPPORTED`。简单队列在目标 namespace 内通过 `tc` 配置；
HTB 结构和已有的 TBF、fq_codel、CAKE 配置继续使用 pyroute2。

## 回读与重复部署

简单根队列和 HTB 子队列通过 `tc -j -d qdisc show dev IFACE` 回读。nslab 根据类型、
handle 和 parent 与 Netlink 记录关联，同时检查 HTB 根队列及 class 结构。命令失败、
JSON 损坏、记录缺失或不唯一都会使回读失败，不会被当成部署成功。

回读保留实际参数及内核默认值。配置比较检查显式声明的参数，未声明的简单队列字段
不作为漂移约束。时间值转换为毫秒，仅容忍最多 1 微秒的内核舍入。
显式的 `ecn: false`、`bytemode: false` 会在对应队列支持时下发关闭选项。

RED 的 `limit`、`min`、`max`、`avpkt` 单位为字节，`burst` 单位为报文。内核不会返回
原始 `avpkt` 和 `burst`，因此比较它们产生的可观测参数 `ewma`、`Scell_log`，以及阈值、
概率和标志，不会把输入值伪装成回读值。产生相同可观测状态的不同输入无法区分。
RED 空闲衰减计算使用 tc 默认的 10Mbit bandwidth，不跟随父 HTB 速率，也不提供总带宽整形。
需要声明正数 `limit`、`avpkt` 和有效的阈值/burst 组合，可参考[示例](examples/qdisc.zh.md)。

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
