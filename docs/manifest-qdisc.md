# Manifest: qdisc

#### `links[].qdisc`

`qdisc` selects one root egress queue discipline. It cannot be combined with `netem` on the
same link, and is installed on both veth ends.

##### Placement and Support Status

Place each `qdisc:` fragment below a `topology.links[]` entry, alongside `endpoints`:

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

A complete manifest also needs top-level `version: 1` and `name`.
Each link selects one root qdisc. The two ends queue independently: `10mbit` means
10 Mbit/s per egress, not a shared bidirectional budget. Different per-end configurations
on the same link are not currently supported.

!!! warning "New kinds are not fully integrated"
    `tbf`, `fq_codel`, `htb`, and `cake` have dedicated deployment and inventory logic.
    The new `pfifo`, `bfifo`, `pfifo_fast`, `prio`, `sfq`, `fq`, `codel`,
    and `red` kinds currently have only a generic schema and parameter forwarding.
    The installed pyroute2 lacks `bfifo/fq/red` encoders; CoDel parameters are not translated;
    PRIO has no `bands/priomap` configuration. Inventory and detailed graph support are missing
    for all eight new kinds. YAML labeled "schema example" describes accepted fields only,
    not a working deployment or a guarantee that parameters take effect.

Optional fields of new kinds default to `null` (not sent), not to a fixed kernel default.
Validation is not yet kind-specific: do not reuse unrelated fields across kinds.
Count, time, and size fields accept positive integers, not size strings such as `32kb`,
unless explicitly stated otherwise.

For manual experiments, deploy a two-node topology without `qdisc` or `netem`, then run
one of the `tc qdisc replace` commands below. Each command changes only `h1:eth0`;
repeat on `h2` for the reverse direction. Root, iproute2, and kernel support are required.
Manual changes are not saved to the manifest or verified by `nslab inspect`.
Inspect and remove them with the commands below; finish with `nslab destroy`:

```bash
sudo nslab exec --node h1 -- tc -s -d qdisc show dev eth0
sudo nslab exec --node h1 -- tc qdisc del dev eth0 root
```

##### pfifo: Packet-Limited FIFO

First-in, first-out with tail drop when full; no flow isolation or rate shaping.

| Field | Default | Description |
| --- | --- | --- |
| `kind` | Required | `pfifo` |
| `limit` | Not sent | Maximum queued packets |

Schema example:

```yaml
qdisc:
  kind: pfifo
  limit: 1000
```

Manual experiment:

```bash
sudo nslab exec --node h1 -- tc qdisc replace dev eth0 root pfifo limit 1000
```

##### bfifo: Byte-Limited FIFO

The same FIFO policy, with capacity measured in bytes rather than packets.

| Field | Default | Description |
| --- | --- | --- |
| `kind` | Required | `bfifo` |
| `limit` | Not sent | Maximum queued bytes |

Schema example (encoder missing):

```yaml
qdisc:
  kind: bfifo
  limit: 65536
```

```bash
sudo nslab exec --node h1 -- tc qdisc replace dev eth0 root bfifo limit 65536
```

##### pfifo_fast: Three-Band Priority FIFO

Three strict-priority bands, each using FIFO. Classification uses skb priority and a priomap,
not per-flow fairness. High-priority traffic can starve lower bands.

| Field | Default | Description |
| --- | --- | --- |
| `kind` | Required | `pfifo_fast`; no dedicated configuration fields exposed |

Schema example:

```yaml
qdisc:
  kind: pfifo_fast
```

```bash
sudo nslab exec --node h1 -- tc qdisc replace dev eth0 root pfifo_fast
```

##### prio: Classful Strict Priority

PRIO supports classification through a priomap or tc filters, with child qdiscs per band.
It does not shape bandwidth. The manifest does not yet expose `bands`, `priomap`,
filters, or custom children.

| Field | Default | Description |
| --- | --- | --- |
| `kind` | Required | `prio`; band hierarchy is not configurable in the current schema |

Schema example (deployment parameters incomplete):

```yaml
qdisc:
  kind: prio
```

Manual three-band configuration; `bands` is a tc option, not a manifest field:

```bash
sudo nslab exec --node h1 -- tc qdisc replace dev eth0 root handle 1: prio bands 3
```

##### sfq: Stochastic Fairness Queueing

Hashes flows into queues and services them in turn. Hash collisions can make flows share
a queue; isolation is not exact.

| Field | Default | Description |
| --- | --- | --- |
| `kind` | Required | `sfq` |
| `limit` | Not sent | Total packet limit |
| `quantum` | Not sent | Service allowance in bytes per round; normally at least the interface MTU |
| `perturb` | Not sent | Hash perturbation period in seconds |
| `flows` | Not sent | SFQ flow-count parameter, subject to kernel constraints; not hash-table `divisor` |

Schema example:

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

##### fq: Per-Flow Scheduling and Pacing

FQ supports per-flow scheduling and socket pacing, useful for studying locally generated TCP.
It is not fq_codel and does not provide a configured aggregate shaping rate.

| Field | Default | Description |
| --- | --- | --- |
| `kind` | Required | `fq` |
| `limit` | Not sent | Total packet limit |
| `quantum` | Not sent | Service allowance in bytes per round |

Schema example (encoder missing; `maxrate`, `flow_limit`, `buckets`, and pacing switches
are not exposed):

```yaml
qdisc:
  kind: fq
  limit: 10000
  quantum: 3028
```

```bash
sudo nslab exec --node h1 -- tc qdisc replace dev eth0 root fq limit 10000 quantum 3028
```

##### codel: Single-Queue Active Queue Management

Drops or marks packets based on persistent queue delay. Unlike fq_codel, it does not
isolate flows; it also does not shape bandwidth.

| Field | Default | Description |
| --- | --- | --- |
| `kind` | Required | `codel` |
| `limit` | Not sent | Packet limit |
| `target_ms` | Not sent | Target queue delay in milliseconds |
| `interval_ms` | Not sent | Window for detecting persistent excess delay, in milliseconds |
| `ecn` | Not sent | Boolean; allow congestion marking for ECN-capable packets |

Schema example (currently not translated to pyroute2's `cdl_*` fields):

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

##### red: Average-Queue-Based Active Queue Management

RED uses a smoothed average queue length to drop or mark packets before the queue fills.
It is not a shaper. An idle veth may not build a queue; successful ping does not demonstrate
RED congestion handling.

| Field | Default | Description |
| --- | --- | --- |
| `kind` | Required | `red` |
| `limit` | Not sent | Hard queue capacity in bytes |
| `min` | Not sent | Average queue threshold where probabilistic drop/mark begins, in bytes |
| `max` | Not sent | Upper threshold of the probability region, in bytes, not packets |
| `avpkt` | Not sent | Assumed average packet size in bytes, used to calculate smoothing |
| `burst` | Not sent | Burst allowance in average-sized packets, not bytes |
| `probability` | Not sent | Configured maximum probability, 0..1; 0.02 means 2% |
| `ecn` | Not sent | Boolean; allow congestion marking for ECN-capable packets; drops remain possible |

Choose `0 < min < max < limit` and tune thresholds to packet size, bursts, and bottleneck rate.
The following schema example replaces the unsuitable 1000/300/900-byte values in the earlier
example. The RED encoder is currently missing; forwarding these fields directly to the
installed pyroute2 does not implement RED deployment.

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

In the manual command, `bandwidth` calculates idle-period decay; it is not a shaper and
is not currently a RED manifest field:

```bash
sudo nslab exec --node h1 -- tc qdisc replace dev eth0 root red limit 400000 min 30000 max 90000 avpkt 1000 burst 50 probability 0.02 bandwidth 10mbit ecn
```

##### tbf: Token-Bucket Shaping

```yaml
qdisc:
  kind: tbf
  rate: 10mbit
  burst: 32kb
  latency_ms: 400
```

`burst` is a positive byte count (or a `kb`/`mb`/`gb` value). `latency_ms` is the maximum
queueing latency used to derive the TBF byte limit.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `kind` | Yes | None | `tbf` |
| `rate` | Yes | None | Shaping rate: integer plus `bit/kbit/mbit/gbit`, using decimal units |
| `burst` | No | `32768` | Bytes; also accepts `b/kb/mb/gb` using powers of 1024 |
| `latency_ms` | No | `400` | Milliseconds used to derive queue capacity, 1..60000; not an end-to-end delay guarantee |

Separate TBF `limit`, `peakrate`, and child qdisc settings are not accepted.
Like other rate fields, `rate` must represent an integer byte rate in 1..4294967295 bytes/s.

##### fq_codel: Fair Queueing with Controlled Delay

```yaml
qdisc:
  kind: fq_codel
  target_ms: 5
  interval_ms: 100
  limit: 10240
  ecn: true
```

`target_ms` and `interval_ms` are queue-delay parameters, `limit` is the packet limit, and
`ecn` enables ECN marking.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `kind` | Yes | None | `fq_codel` |
| `target_ms` | No | `5` | Target queue delay, 1..60000 ms |
| `interval_ms` | No | `100` | Persistent-delay observation window, 1..60000 ms; must be at least target |
| `limit` | No | `10240` | Total packet limit, 1..1000000 |
| `ecn` | No | `true` | Boolean; allow ECN marking |

Standalone fq_codel does not shape bandwidth. `flows`, `quantum`, and `memory_limit`
are not exposed for this kind, even where the generic schema of another kind accepts such fields.

##### htb: Aggregate Shaping with an fq_codel Leaf

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

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `kind` | Yes | None | Must be `htb` |
| `rate` | Yes | None | Aggregate rate of the single HTB class |
| `leaf` | Yes | None | fq_codel configuration attached below that class |

nslab creates a fixed hierarchy: root HTB `1:`, class `1:1`, and fq_codel leaf `10:`. Packets
enter the default class, so `rate` limits aggregate egress while fq_codel separates flows and
controls queue delay.

##### cake: Shaping and Queue Management

```yaml
qdisc:
  kind: cake
  bandwidth: 20mbit
  flow_mode: flows
  diffserv_mode: besteffort
  rtt_ms: 100
  nat: false
```

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `kind` | Yes | None | Must be `cake` |
| `bandwidth` | Yes | None | Aggregate shaper bandwidth |
| `flow_mode` | No | `flows` | `srchost`, `dsthost`, `hosts`, `flows`, `dual-srchost`, `dual-dsthost`, or `triple-isolate` |
| `diffserv_mode` | No | `besteffort` | `diffserv3`, `diffserv4`, `diffserv8`, `besteffort`, or `precedence` |
| `rtt_ms` | No | `100` | RTT assumption in `1..60000` ms used by CAKE's AQM |
| `nat` | No | `false` | Consider addresses behind NAT when isolating hosts and flows |

CAKE requires kernel support for `sch_cake`. A host without that qdisc cannot deploy a topology
that declares it; use the separate CAKE example to check support without affecting other labs.

## Manifest boundary

A manifest does not accept `traffic`, `observe`, packet capture, or arbitrary command fields.
Run traffic and observation actions explicitly with [`nslab exec`](cli.md#exec). See
[Examples](examples/index.md) for complete runnable manifests.
