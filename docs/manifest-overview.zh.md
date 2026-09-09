# Manifest 总览


`nslab.yaml` 使用严格 schema：未知字段、错误类型和无效引用都会在修改内核网络资源前
被拒绝。完整层级如下：

```text
version
name
topology
├─ nodes
│  └─ <node-name>
│     ├─ kind: linux
│     │  ├─ interfaces / devices / routes → nexthops / neighbors / rules / sysctls
│     │  ├─ devices → <device-name> → type: vlan | vrf | bond | gre | ipip | vxlan | dummy | geneve | macvlan | ipvlan
│     │  └─ routing
│     └─ kind: bridge
│        ├─ interfaces / devices / routes / neighbors / sysctls
│        ├─ devices → <device-name> → type: vxlan | geneve
│        └─ bridge → ports → vlans
└─ links
   └─ <link>
      ├─ endpoints / mtu
      ├─ netem
      │  └─ delay_ms / jitter_ms / loss_percent / rate
      └─ qdisc
         ├─ kind: tbf | fq_codel | htb | cake
         └─ leaf → kind: fq_codel（仅 htb）
```

## 顶层字段

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `version` | 是 | 无 | Schema 版本，目前只能是整数 `1` |
| `name` | 是 | 无 | Deployment 名称，匹配 `^[a-z][a-z0-9_-]{0,31}$` |
| `topology` | 是 | 无 | 包含 `nodes` 和 `links` 的拓扑对象 |

```yaml
version: 1
name: my-lab
topology:
  nodes: {}
  links: []
```

## `topology`

| 字段 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| `topology.nodes` | 是 | 映射 | Key 是节点名，value 是 Linux 或 bridge 节点 |
| `topology.links` | 是 | 列表 | 每项描述一条两端点 veth 链路 |
