# Manifest：节点

节点写在 `topology.nodes` 下，支持 `linux` 和 `bridge` 两种 kind。公共字段包括
`interfaces`、`routes`、`neighbors`、`sysctls`；Linux 节点还可以配置 `routing`。

```yaml
topology:
  nodes:
    h1:
      kind: linux
      interfaces:
        eth0:
          addresses: [10.0.0.1/24]
    sw1:
      kind: bridge
      bridge:
        name: br0
  links: []
```

`vlan`、`vrf`、`bond`、`gre`、`ipip`、`vxlan`、`geneve`、`dummy`、`macvlan`
和 `ipvlan` 等 namespace 内设备放在 Linux 节点的 `devices` 下。各设备字段见
[完整参考](manifest.zh.md#topologynodes)。
