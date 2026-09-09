# Manifest: routing

Routing-related fields live on Linux nodes. Use `routes` for static routes, `neighbors`
for ARP/NDP entries, `rules` for policy routing, and `sysctls` for permitted network
sysctls. Dynamic routing is configured under `routing` with OSPF, BGP, or PIM.

```yaml
nodes:
  r1:
    kind: linux
    routes:
      - dst: 10.20.0.0/24
        via: 10.10.0.2
        dev: eth0
    routing:
      ospf:
        router_id: 1.1.1.1
        networks: [10.10.0.0/30]
```

See [routes](manifest.md#routes), [policy rules](manifest.md#rules), and
[dynamic routing](manifest.md#routing) in the complete reference.
