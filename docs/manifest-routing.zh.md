# Manifest：路由

路由相关字段位于 Linux 节点中：`routes` 配置静态路由，`neighbors` 配置 ARP/NDP，
`rules` 配置策略路由规则，`sysctls` 配置允许的网络 sysctl。OSPF、BGP、PIM 位于
`routing` 下。

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

完整参考：[routes](manifest.zh.md#routes)、[策略规则](manifest.zh.md#rules)、
[动态路由](manifest.zh.md#routing)。
