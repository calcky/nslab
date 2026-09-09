# Manifest: links

### `topology.links`

Each link joins two `node:interface` endpoints. An endpoint may appear only once in the entire
topology. Node and interface references must be valid and cannot use `lo`.

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `kind` | No | `veth` | Link kind; currently only `veth` |
| `endpoints` | Yes | None | Exactly two `node:interface` strings |
| `mtu` | No | `1500` | MTU on both ends in `576..9216`; at least `1280` when an endpoint carries IPv6 |
| `netem` | No | `null` | Netem conditions applied to egress at both ends; mutually exclusive with `qdisc` |
| `qdisc` | No | `null` | Independent egress queues at both ends; see [Qdisc](manifest-qdisc.md) for root and HTB leaf configuration |

```yaml
links:
  - endpoints: [h1:eth0, h2:eth0]
    mtu: 1500
```

#### `links[].netem`

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `delay_ms` | No | `0` | Per-end egress delay in `0..60000` ms |
| `jitter_ms` | No | `0` | Delay jitter in `0..60000` ms; requires `delay_ms > 0` |
| `loss_percent` | No | `0` | Random packet loss as an integer percentage in `0..100` |
| `rate` | No | `null` | Optional egress rate such as `10mbit`, `500kbit`, or `1gbit` |

All four values cannot be zero. netem is installed on both veth ends, so a bidirectional
ping experiences one egress condition on the request and another on the reply.

```yaml
netem:
  rate: 10mbit
  delay_ms: 20
  jitter_ms: 5
  loss_percent: 1
```
