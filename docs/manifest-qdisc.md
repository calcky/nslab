# Manifest: qdisc

## Model

Put one `qdisc` under each `topology.links[]` item. nslab creates the same configuration on
both veth egress ends. `qdisc` and `netem` are mutually exclusive.

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

| Group | Configuration |
| --- | --- |
| Single queue | `HTB + pfifo`, `HTB + red`, `HTB + codel`, `HTB + pie` |
| Flow isolation | `HTB + sfq`, `HTB + fq`, `HTB + fq_codel`, `HTB + fq_pie` |
| Complete shaper | `CAKE` with its own shaping and AQM |

## HTB leaves

| Kind | Main fields | Purpose |
| --- | --- | --- |
| `pfifo` | `limit` | FIFO queue limited by packets |
| `red` | `limit`, `min`, `max`, `avpkt`, `burst`, `probability`, `ecn` | Average-queue AQM; thresholds are bytes |
| `codel` | `limit`, `target_ms`, `interval_ms`, `ecn` | Single-queue delay AQM |
| `pie` | `limit`, `target_ms`, `tupdate_ms`, `alpha`, `beta`, `ecn`, `bytemode` | Single-queue probabilistic AQM |
| `sfq` | `limit`, `quantum`, `perturb`, `flows` | Hash-based stochastic flow isolation |
| `fq` | `limit`, `quantum` | Flow scheduling and socket pacing |
| `fq_codel` | `limit`, `target_ms`, `interval_ms`, `ecn` | Flow isolation plus CoDel |
| `fq_pie` | `limit`, `target_ms`, `tupdate_ms`, `alpha`, `beta`, `ecn`, `bytemode` | Flow isolation plus PIE |

Example:

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

`pie` and `fq_pie` require kernel `sch_pie` support and a compatible `tc`. Missing support
is reported as `QDISC_UNSUPPORTED`. All qdisc parameters are applied by `tc` inside the
target namespace; pyroute2 remains responsible for topology operations.

## Root qdiscs

All listed qdiscs can also be used directly as root qdiscs. FIFO, fair-queueing, and AQM kinds
do not provide aggregate shaping; `tbf` and `cake` do. `tbf` uses `rate`,
`burst`, and `latency_ms`; `cake` uses `bandwidth`, `flow_mode`, `diffserv_mode`, `rtt_ms`,
and `nat`.

```yaml
qdisc:
  kind: cake
  bandwidth: 20mbit
  flow_mode: flows
  diffserv_mode: besteffort
  rtt_ms: 100
  nat: false
```

See the [qdisc example](examples/qdisc.md) for deploy, traffic, counters, and cleanup.
