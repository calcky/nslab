# Manifest：qdisc

#### `links[].qdisc`

`qdisc` 选择一个根 egress 队列规则。它不能与同一链路的 `netem` 同时使用，并会安装
在 veth 两端。

##### 配置位置与当前支持状态

下面片段中的 `qdisc:` 都放在某一条 `topology.links[]` 下，与 `endpoints` 同级：

```yaml
topology:
  nodes:
    h1:
      kind: linux
      interfaces:
        eth0:
          addresses: [10.60.0.1/24]
    h2:
      kind: linux
      interfaces:
        eth0:
          addresses: [10.60.0.2/24]
  links:
    - endpoints: [h1:eth0, h2:eth0]
      qdisc:
        kind: tbf
        rate: 10mbit
```

完整 manifest 还需要顶层 `version: 1` 和 `name`。每条链路只能选择一个根
qdisc；两端各自独立排队，`10mbit` 表示每端 egress 各限速 10 Mbit/s，而不是双向共享。
当前不支持在同一条链路上声明两个不同方向的 qdisc。

!!! warning "新增类型尚未完成后端接入"
    `tbf`、`fq_codel`、`htb`、`cake` 已有专用部署和状态读取逻辑。
    新增的 `pfifo`、`bfifo`、`pfifo_fast`、`prio`、`sfq`、`fq`、
    `codel`、`red` 目前只有通用 schema 和参数透传，不能视为完整可用功能。
    当前依赖的 pyroute2 未注册 `bfifo/fq/red` 编码器，`codel` 参数名未转换，
    `prio` 缺少 `bands/priomap` 配置，八种新类型的 inspect 和详细 graph 也未适配。
    下方标注“schema 示例”的 YAML 只说明已接受的字段，不保证能部署或参数会生效。

新类型的可选字段默认均为 `null`（不发送）；这不等于固定的内核默认值。
字段暂未按 kind 分别校验，不要把某种 qdisc 的字段用于另一种。
除单独说明外，计数、时间、大小字段只接受正整数；新类型不接受 `32kb` 这样的大小字符串。

手动实验时，先部署不含 `qdisc` 和 `netem` 的两节点拓扑，然后执行各节中的
`tc qdisc replace` 命令。命令只改 `h1:eth0`，要作用于另一方向需对 `h2` 再执行一次。
需要 root、iproute2 和对应内核模块。手动配置不会写回 manifest，也不由
`nslab inspect` 核验；用以下命令查看和撤销，最后仍使用 `nslab destroy` 清理拓扑：

```bash
sudo nslab exec --node h1 -- tc -s -d qdisc show dev eth0
sudo nslab exec --node h1 -- tc qdisc del dev eth0 root
```

##### pfifo：按报文数限制的 FIFO

先进先出，队列满时尾丢弃；不区分 flow，也不限速。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `kind` | 必填 | `pfifo` |
| `limit` | 不发送 | 队列最多容纳的报文数 |

Schema 示例：

```yaml
qdisc:
  kind: pfifo
  limit: 1000
```

手动实验：

```bash
sudo nslab exec --node h1 -- tc qdisc replace dev eth0 root pfifo limit 1000
```

##### bfifo：按字节数限制的 FIFO

与 pfifo 相同的 FIFO 策略，但队列容量按字节统计。相同字节上限下能容纳多少包取决于包长。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `kind` | 必填 | `bfifo` |
| `limit` | 不发送 | 队列字节上限，不是报文数 |

Schema 示例（当前缺少编码器）：

```yaml
qdisc:
  kind: bfifo
  limit: 65536
```

```bash
sudo nslab exec --node h1 -- tc qdisc replace dev eth0 root bfifo limit 65536
```

##### pfifo_fast：固定三档优先级 FIFO

三个 band，优先发送高优先级 band 中的包；同一 band 内 FIFO。
分类基于 skb priority 与 priomap，不是自动按 flow 公平分配，高优先级流量可能饿死低优先级流量。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `kind` | 必填 | `pfifo_fast`；当前 schema 没有专用配置项 |

Schema 示例：

```yaml
qdisc:
  kind: pfifo_fast
```

```bash
sudo nslab exec --node h1 -- tc qdisc replace dev eth0 root pfifo_fast
```

##### prio：可分类的严格优先级调度

PRIO 是 classful qdisc，可以按 priomap 或 tc filter 分类，也可以给不同 band 挂载子 qdisc。
它本身不限制总带宽；当前 manifest 尚不支持 `bands`、`priomap`、filter 或自定义子队列。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `kind` | 必填 | `prio`；不能通过当前 schema 配置 band 层级 |

Schema 示例（尚未完成部署参数接入）：

```yaml
qdisc:
  kind: prio
```

下面是手动配置三档优先级的方式，不是 manifest 字段：

```bash
sudo nslab exec --node h1 -- tc qdisc replace dev eth0 root handle 1: prio bands 3
```

##### sfq：随机公平队列

通过 flow hash 分配队列，再轮流服务；哈希碰撞可能让多个 flow 共用队列，不是精确的逐流隔离。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `kind` | 必填 | `sfq` |
| `limit` | 不发送 | 总队列报文上限 |
| `quantum` | 不发送 | 每轮服务字节额度；通常不小于接口 MTU |
| `perturb` | 不发送 | 哈希重扰动周期，秒 |
| `flows` | 不发送 | SFQ flow 数量参数，实际约束取决于内核；不是哈希桶数 `divisor` |

Schema 示例：

```yaml
qdisc:
  kind: sfq
  limit: 127
  quantum: 1514
  perturb: 10
```

```bash
sudo nslab exec --node h1 -- tc qdisc replace dev eth0 root sfq limit 127 quantum 1514 perturb 10
```

##### fq：逐流公平调度与 pacing

FQ 支持逐流调度和 socket pacing，适合观察本机 TCP pacing。
不要把它等同于 fq_codel；FQ 本身不是指定总带宽的整形器。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `kind` | 必填 | `fq` |
| `limit` | 不发送 | 总队列报文上限 |
| `quantum` | 不发送 | 每轮服务字节额度 |

Schema 示例（当前缺少编码器；`maxrate`、`flow_limit`、`buckets` 和 pacing 开关尚未暴露）：

```yaml
qdisc:
  kind: fq
  limit: 10000
  quantum: 3028
```

```bash
sudo nslab exec --node h1 -- tc qdisc replace dev eth0 root fq limit 10000 quantum 3028
```

##### codel：单队列主动队列管理

根据持续排队延迟进行丢包或 ECN 标记，没有 fq_codel 的 flow 隔离，也不负责限速。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `kind` | 必填 | `codel` |
| `limit` | 不发送 | 队列报文上限 |
| `target_ms` | 不发送 | 目标排队延迟，毫秒 |
| `interval_ms` | 不发送 | 判断延迟是否持续超标的时间窗口，毫秒 |
| `ecn` | 不发送 | 布尔值；允许对支持 ECN 的包进行拥塞标记 |

Schema 示例（当前参数未映射到 pyroute2 的 `cdl_*` 字段）：

```yaml
qdisc:
  kind: codel
  limit: 1000
  target_ms: 5
  interval_ms: 100
  ecn: true
```

```bash
sudo nslab exec --node h1 -- tc qdisc replace dev eth0 root codel limit 1000 target 5ms interval 100ms ecn
```

##### red：基于平均队列长度的主动队列管理

RED 使用平滑后的平均队列长度，在队列填满之前概率丢弃或进行 ECN 标记。
它不是限速器。空闲 veth 上不一定产生积压，ping 成功也不能证明 RED 的拥塞行为。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `kind` | 必填 | `red` |
| `limit` | 不发送 | 硬队列容量，字节 |
| `min` | 不发送 | 开始概率丢弃/标记的平均队列阈值，字节 |
| `max` | 不发送 | 概率区间上界，字节；不是报文数 |
| `avpkt` | 不发送 | 假设的平均包长，字节，用于计算平滑参数 |
| `burst` | 不发送 | 允许突发的平均大小报文数量，不是字节数 |
| `probability` | 不发送 | 最大概率配置，0..1；示例 0.02 表示 2% |
| `ecn` | 不发送 | 布尔值；拥塞时可对 ECN-capable 包标记，仍可能发生丢包 |

选择 `0 < min < max < limit`，阈值应与包长、突发和瓶颈速率相适应。
以下为 schema 示例，当前缺少 RED 编码器；这些字段不能直接透传给现有 pyroute2 来部署 RED。
它们替代旧示例中不合理的 1000/300/900 字节配置：

```yaml
qdisc:
  kind: red
  limit: 400000
  min: 30000
  max: 90000
  avpkt: 1000
  burst: 50
  probability: 0.02
  ecn: true
```

手动命令中的 `bandwidth` 用于计算 RED 空闲期衰减，不执行限速，
当前也不是 RED manifest 字段：

```bash
sudo nslab exec --node h1 -- tc qdisc replace dev eth0 root red limit 400000 min 30000 max 90000 avpkt 1000 burst 50 probability 0.02 bandwidth 10mbit ecn
```

##### tbf：令牌桶整形

```yaml
qdisc:
  kind: tbf
  rate: 10mbit
  burst: 32kb
  latency_ms: 400
```

`burst` 是正的字节数，也可以使用 `kb`、`mb` 或 `gb` 后缀。`latency_ms` 用于推导 TBF
队列字节上限。

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `kind` | 是 | 无 | `tbf` |
| `rate` | 是 | 无 | 整形速率，整数加 `bit/kbit/mbit/gbit`，按十进制换算 |
| `burst` | 否 | `32768` | 字节数；也接受 `b/kb/mb/gb`，按 1024 倍换算 |
| `latency_ms` | 否 | `400` | 推导队列容量使用的毫秒数，1..60000；不是端到端延迟保证 |

当前不接受 TBF 的独立 `limit`、`peakrate` 或子 qdisc 配置。
与其他 rate 字段一样，`rate` 必须能换算为整数 bytes/s，范围为 1..4294967295 bytes/s。

##### fq_codel：公平队列与受控延迟

```yaml
qdisc:
  kind: fq_codel
  target_ms: 5
  interval_ms: 100
  limit: 10240
  ecn: true
```

`target_ms` 和 `interval_ms` 是队列延迟参数，`limit` 是报文数上限，`ecn` 控制是否启用
ECN 标记。

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `kind` | 是 | 无 | `fq_codel` |
| `target_ms` | 否 | `5` | 目标排队延迟，1..60000 ms |
| `interval_ms` | 否 | `100` | 持续超标检测窗口，1..60000 ms，必须不小于 target |
| `limit` | 否 | `10240` | 总报文上限，1..1000000 |
| `ecn` | 否 | `true` | 是否允许 ECN 标记，布尔值 |

独立 fq_codel 不限速。`flows`、`quantum` 和 `memory_limit` 尚未在此 kind 中暴露；
不能因为新类型的通用 schema 存在同名字段就填在这里。

##### htb：总带宽整形与 fq_codel 子队列

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

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `kind` | 是 | 无 | 必须是 `htb` |
| `rate` | 是 | 无 | 单个 HTB class 的总速率 |
| `leaf` | 是 | 无 | 挂在该 class 下的 fq_codel 配置 |

nslab 创建固定层级：HTB 根 `1:`、class `1:1` 和 fq_codel leaf `10:`。报文进入默认 class，
因此 `rate` 限制 egress 总带宽，fq_codel 负责区分 flow 和控制队列延迟。

##### cake：整形与队列管理

```yaml
qdisc:
  kind: cake
  bandwidth: 20mbit
  flow_mode: flows
  diffserv_mode: besteffort
  rtt_ms: 100
  nat: false
```

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `kind` | 是 | 无 | 必须是 `cake` |
| `bandwidth` | 是 | 无 | 总整形带宽 |
| `flow_mode` | 否 | `flows` | `srchost`、`dsthost`、`hosts`、`flows`、`dual-srchost`、`dual-dsthost` 或 `triple-isolate` |
| `diffserv_mode` | 否 | `besteffort` | `diffserv3`、`diffserv4`、`diffserv8`、`besteffort` 或 `precedence` |
| `rtt_ms` | 否 | `100` | CAKE AQM 使用的 RTT 假设，范围 `1..60000` ms |
| `nat` | 否 | `false` | 隔离 host 和 flow 时是否识别 NAT 内侧地址 |

CAKE 需要内核支持 `sch_cake`。不包含该 qdisc 的主机不能部署声明了 CAKE 的拓扑；独立的
CAKE 示例可用于检查支持情况，不会影响其他实验。
