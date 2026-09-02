# Manifest 字段参考

`nslab.yaml` 使用严格 schema：未知字段、错误类型和无效引用都会在修改内核网络资源前
被拒绝。完整层级如下：

```text
version
name
topology
├─ nodes
│  └─ <node-name>
│     ├─ kind: linux
│     │  ├─ interfaces / devices / routes / sysctls
│     │  ├─ devices → <device-name> → type: vlan | vrf
│     │  └─ routing
│     └─ kind: bridge
│        ├─ interfaces / routes / sysctls
│        └─ bridge → ports → vlans
└─ links
   └─ endpoints / mtu / netem
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

### `topology.nodes`

节点名使用与 deployment 相同的格式：必须以小写字母开头，最长 32 个字符，可包含
小写字母、数字、`_` 和 `-`。当前支持 `linux` 与 `bridge` 两种 `kind`。

#### 节点公共字段

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `kind` | 是 | 无 | Discriminator，只能是 `linux` 或 `bridge` |
| `interfaces` | 否 | `{}` | 接口名到接口配置的映射 |
| `routes` | 否 | `[]` | 静态 IPv4/IPv6 路由列表 |
| `sysctls` | 否 | `{}` | nslab 允许修改的网络 sysctl |
| `routing` | 否 | `null` | OSPF/BGP 配置，仅允许用于 `linux` 节点 |

接口名必须为 1 到 15 个字符，可包含字母、数字、`_`、`.` 和 `-`。除 bridge 设备名外，
`interfaces` 中声明的接口必须在 `links[].endpoints` 中出现。
Namespace 内部的 VLAN 和 VRF 设备应声明在 `devices`，而不是 `interfaces`。

##### `interfaces.<ifname>`

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `addresses` | 否 | `[]` | 唯一的 IPv4/IPv6 CIDR 地址列表，例如 `10.0.0.1/24` 或 `2001:db8::1/64` |

一个接口可以同时声明多个地址，也可以不声明地址。重复地址会被拒绝。

##### `routes[]`

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `dst` | 是 | 无 | IPv4/IPv6 目标前缀；`default` 等价于 `0.0.0.0/0` |
| `via` | 否 | `null` | 下一跳地址，地址族必须与 `dst` 一致 |
| `dev` | 是 | 无 | 出接口名，必须是该节点可用的链接接口或 bridge 设备 |

同一路由表中不能重复声明目标前缀，也不能把该表的直连网段再次声明为静态路由。
VRF 成员接口会自动选择对应 VRF table。

##### `sysctls`

当前只接受以下 key，value 必须是整数 `0` 或 `1`：

| Key | 说明 |
| --- | --- |
| `net.ipv4.ip_forward` | 关闭或开启 IPv4 转发 |
| `net.ipv6.conf.all.forwarding` | 关闭或开启 IPv6 转发 |

#### `kind: linux`

Linux 节点表示普通 network namespace，可配置公共字段、namespace 内部设备以及动态路由：

```yaml
r1:
  kind: linux
  interfaces:
    eth0:
      addresses: [10.0.12.1/30]
  devices:
    vlan10:
      type: vlan
      link: eth0
      id: 10
      addresses: [192.168.10.1/24]
  sysctls:
    net.ipv4.ip_forward: 1
```

##### `devices`

`devices` 会在所有 veth endpoint 移入节点后，在 Linux 节点内部创建设备。设备名遵循
接口名规则，不能是 `lo`，不能与 linked endpoint 或 `interfaces` key 冲突。必须通过
`type` 选择 `vlan` 或 `vrf`。

###### `type: vlan`

802.1Q VLAN 子接口可用于 `routes[].dev` 和 `routing.ospf.passive_interfaces`：

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `devices.<name>.type` | 是 | 无 | 必须为 `vlan` |
| `devices.<name>.link` | 是 | 无 | Lower interface，必须是同一节点中的 linked interface |
| `devices.<name>.id` | 是 | 无 | VLAN ID，范围 `1..4094`，同一 lower interface 上不能重复 |
| `devices.<name>.addresses` | 否 | `[]` | 配置到 VLAN 设备的唯一 IPv4/IPv6 CIDR 地址 |

当前只支持一层设备：VLAN 设备不能再以另一个声明设备作为 lower interface。MTU 继承
lower interface。直连路由、BGP 直连邻居检查以及 OSPF/BGP 自动 network statement 都会
包含设备地址。

###### `type: vrf`

VRF 是三层 master，将成员接口放进独立的 Linux 路由表：

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `devices.<name>.type` | 是 | 无 | 必须为 `vrf` |
| `devices.<name>.table` | 是 | 无 | Table ID 范围 `1..4294967295`，不能使用保留表 `253`、`254`、`255` |
| `devices.<name>.interfaces` | 是 | 无 | 非空的 linked interface 或已声明 VLAN 设备列表 |

同一节点中的 table ID 不能重复，一个接口也只能属于一个 VRF。成员接口的直连路由和
声明式静态路由会自动进入对应 VRF table，因此相同目的前缀可在每个路由域中各出现一次。
当前 VRF 设备不能与声明式 OSPF/BGP 同时使用；高级 VRF 动态路由实验可通过
`nslab exec` 显式运行 daemon。

##### `routing`

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `routing.ospf` | 条件必填 | `null` | OSPFv2 配置 |
| `routing.bgp` | 条件必填 | `null` | IPv4 eBGP 配置 |

声明 `routing` 时至少启用一个协议，可以同时启用两者。节点必须设置
`net.ipv4.ip_forward: 1`。nslab 会为每个节点启动独立 FRRouting daemon 和 pathspace。

###### `routing.ospf`

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `router_id` | 是 | 无 | 唯一 IPv4 router ID |
| `area` | 否 | `0.0.0.0` | 所有 network statement 使用的 OSPF area |
| `networks` | 否 | `[]` | 要发布的 IPv4 前缀；为空时发布节点所有 IPv4 直连网段 |
| `passive_interfaces` | 否 | `[]` | 发布网段但不建立邻居的接口列表 |

`passive_interfaces` 中的接口必须属于该节点；OSPF `router_id` 在同一 manifest 内不能
重复，`networks` 前缀也不能重复。

###### `routing.bgp`

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `local_as` | 是 | 无 | 本地 ASN，范围 `1..4294967295` |
| `router_id` | 是 | 无 | 唯一 IPv4 router ID |
| `neighbors` | 是 | 无 | 直接相连的 IPv4 BGP 邻居列表 |
| `networks` | 否 | `[]` | 要发布的 IPv4 前缀；为空时发布节点所有 IPv4 直连网段 |

每个 `neighbors[]` 项：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `address` | 是 | 邻居 IPv4 地址，必须属于本节点某个直连 IPv4 网段 |
| `remote_as` | 是 | 对端 ASN，范围 `1..4294967295` |

邻居地址和 `networks` 前缀不能重复；BGP `router_id` 在同一 manifest 内不能重复。

#### `kind: bridge`

Bridge 节点在自己的 namespace 中创建一台 Linux bridge。它可以使用公共的
`interfaces`、`routes` 和 `sysctls` 字段，但不能声明 `routing`。

| 节点字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `bridge` | 是 | 无 | Linux bridge 设备及端口配置对象 |

```yaml
sw1:
  kind: bridge
  bridge:
    name: br0
    stp: true
    vlan_filtering: false
```

##### `bridge`

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `name` | 是 | 无 | namespace 内的 bridge 设备名，不能是 `lo` |
| `stp` | 是 | 无 | 是否开启 Linux bridge STP |
| `vlan_filtering` | 是 | 无 | 是否开启 VLAN-aware filtering |
| `priority` | 否 | `null` | Bridge priority，范围 `0..65535` |
| `ports` | 否 | `{}` | 链接端口名到 STP/VLAN 配置的映射 |

`bridge.name` 不能与链接 endpoint 使用同名接口。若要给 bridge 本身配置 IP，可在节点
`interfaces` 中使用相同的 `bridge.name`。

###### `bridge.ports.<ifname>`

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `path_cost` | 否 | `null` | STP path cost，范围 `1..65535`，要求 `stp: true` |
| `priority` | 否 | `null` | Linux STP port priority，范围 `0..63`，要求 `stp: true` |
| `vlans` | 否 | `[]` | 端口 VLAN 列表，要求 `vlan_filtering: true` |

端口配置至少要包含一个 STP 或 VLAN 设置；端口名必须在 `links` 中连接。

每个 `vlans[]` 项：

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `vid` | 是 | 无 | VLAN ID，范围 `1..4094`，同一端口不能重复 |
| `pvid` | 否 | `false` | 是否作为 ingress 未标记帧的 PVID；每端口最多一个 |
| `untagged` | 否 | `false` | egress 时是否移除 802.1Q tag |

### `topology.links`

每条链路连接两个 `node:interface` endpoint。一个 endpoint 在整个拓扑中只能使用一次；
节点和接口引用都必须有效，且不能使用 `lo`。

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `kind` | 否 | `veth` | 链路类型，目前只能是 `veth` |
| `endpoints` | 是 | 无 | 恰好两个 `node:interface` 字符串 |
| `mtu` | 否 | `1500` | 两端 MTU，范围 `576..9216` |
| `netem` | 否 | `null` | 同时应用到两端 egress 的链路条件 |

```yaml
links:
  - endpoints: [h1:eth0, h2:eth0]
    mtu: 1500
```

#### `links[].netem`

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `delay_ms` | 否 | `0` | 单端 egress 延迟，范围 `0..60000` ms |
| `jitter_ms` | 否 | `0` | 延迟抖动，范围 `0..60000` ms；要求 `delay_ms > 0` |
| `loss_percent` | 否 | `0` | 随机丢包率，范围 `0..100` 的整数百分比 |

三个值不能同时为零。netem 同时安装在 veth 两端，因此双向 ping 会在 request 和 reply
方向各经历一次 egress 条件。

## Manifest 边界

Manifest 不接受 `traffic`、`observe`、抓包或任意命令字段。流量和观察动作使用
[`nslab exec`](cli.md#exec) 显式执行。所有可运行配置见[实验示例](examples/index.md)。
