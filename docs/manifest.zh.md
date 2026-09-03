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
│     │  ├─ interfaces / devices / routes / rules / sysctls
│     │  ├─ devices → <device-name> → type: vlan | vrf | bond | vxlan
│     │  └─ routing
│     └─ kind: bridge
│        ├─ interfaces / devices / routes / sysctls
│        ├─ devices → <device-name> → type: vxlan
│        └─ bridge → ports → vlans
└─ links
   └─ endpoints / mtu / netem | qdisc
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
Namespace 内部的 VLAN、VRF、bond 和 VXLAN 设备应声明在 `devices`，而不是
`interfaces`。

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
| `dev` | 是 | 无 | 可用出接口名，可以是 linked、namespace 内部设备或该节点的 bridge 设备 |
| `table` | 否 | 自动 | 路由表 ID，范围 `1..4294967295`，不能使用 local table `255` |

同一路由表中不能重复声明目标前缀，也不能把该表的直连网段再次声明为静态路由。
省略 `table` 时使用 main table 254，VRF 成员接口则自动使用对应 VRF table。若对 VRF
成员路由显式指定 `table`，其值必须与该 VRF 的 table 相同。

##### `sysctls`

当前只接受以下 key，value 必须是整数 `0` 或 `1`：

| Key | 说明 |
| --- | --- |
| `net.ipv4.ip_forward` | 关闭或开启 IPv4 转发 |
| `net.ipv6.conf.all.forwarding` | 关闭或开启 IPv6 转发 |

#### `kind: linux`

Linux 节点表示普通 network namespace，可配置公共字段、namespace 内部设备、策略规则以及
动态路由：

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

##### `rules[]`

`rules` 声明 Linux routing policy database（RPDB）条目。内核按 `priority` 从小到大
匹配；一条 rule 中声明的所有 selector 会同时生效。地址族优先从 `from` 或 `to` 推断，
无法推断时默认为 IPv4。

```yaml
routes:
  - dst: 203.0.113.0/24
    via: 10.0.0.2
    dev: eth1
    table: 100
rules:
  - priority: 100
    from: 192.0.2.0/24
    to: 203.0.113.0/24
    iif: eth0
    table: 100
```

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `priority` | 是 | 无 | Rule preference，范围 `1..4294967295`，同一地址族内不能重复 |
| `family` | 否 | 自动推断或 IPv4 | `ipv4` 或 `ipv6`；无地址 selector 的 IPv6 rule 需显式设置 |
| `action` | 否 | `lookup` | `lookup`、`goto`、`nop`、`blackhole`、`unreachable` 或 `prohibit` |
| `table` | 条件必填 | `null` | Table ID，范围 `1..4294967295`；`lookup` 必填，`l3mdev: true` 除外 |
| `goto` | 条件必填 | `null` | 要跳转到的更大 priority；`action: goto` 时必填 |
| `from` | 否 | 任意源 | 源 IPv4/IPv6 前缀 |
| `to` | 否 | 任意目的 | 目的 IPv4/IPv6 前缀 |
| `not` | 否 | `false` | 对整组 selector 的匹配结果取反 |
| `tos` | 否 | 未指定 | IPv4 TOS 或 IPv6 traffic class，范围 `0..255`；零会归一化为未指定 |
| `fwmark` | 否 | `null` | Packet mark，范围 `0..4294967295` |
| `fwmask` | 否 | 全位 mask | Mark mask，范围 `0..4294967295`；要求同时声明 `fwmark` |
| `iif` | 否 | 任意 | 入接口名，可以是 `lo` 或已声明的 Linux device |
| `oif` | 否 | 任意 | 出接口名，可以是 `lo` 或已声明的 Linux device |
| `l3mdev` | 否 | `false` | 匹配与 L3 master 关联的包，并使用该 master 的路由表 |
| `uid_range` | 否 | `null` | 本地 socket UID 范围 `{start, end}`，端点范围 `0..4294967295` |
| `protocol` | 否 | `0` | Rule 来源协议编号，范围 `0..255` |
| `ip_protocol` | 否 | `null` | IP 协议编号，范围 `0..255`；零会归一化为未指定 |
| `source_port` | 否 | `null` | 源端口范围 `{start, end}`，端点范围 `0..65535` |
| `destination_port` | 否 | `null` | 目的端口范围 `{start, end}`，端点范围 `0..65535` |
| `tunnel_id` | 否 | `null` | Tunnel key，范围 `0..18446744073709551615`；零表示未指定 |
| `suppress_prefix_length` | 否 | `null` | 忽略前缀长度不大于该值的 lookup 结果 |
| `suppress_interface_group` | 否 | `null` | 忽略使用 interface group `0..4294967294` 的结果 |
| `realms` | 否 | `null` | Route realms `{source, destination}`，端点范围 `0..65535` |

`from`、`to` 必须与选定地址族一致。端口范围要求非零 `ip_protocol`，所有 range 的
start 不能大于 end，两个 realm 不能同时为零。Suppress 选项只适用于 `lookup`。`goto`
目标必须大于当前 rule 的 priority，但可以暂时 unresolved；后续出现合适 rule 时由 Linux
完成解析。Lookup rule 可用数字选择标准表 253（`default`）、254（`main`）和
255（`local`）。

节点声明 VRF 时，Linux 会自动安装 priority 1000 的 `l3mdev` rule，因此该节点不能再
声明相同 priority。旧式 route-NAT rule action 不受支持，因为当前 Linux 无法通过用于
确定性 drift 检查的 netlink inventory 保留其地址。

##### `devices`

`devices` 会在所有 veth endpoint 移入节点后，在 Linux 节点内部创建设备。设备名遵循
接口名规则，不能是 `lo`，不能与 linked endpoint 或 `interfaces` key 冲突。必须通过
`type` 选择 `vlan`、`vrf`、`bond` 或 `vxlan`。

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

###### `type: vxlan`

独立 VXLAN 设备是 Linux 三层接口。它使用静态单播远端 VTEP，可以直接承载地址和路由：

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `devices.<name>.type` | 是 | 无 | 必须为 `vxlan` |
| `devices.<name>.vni` | 是 | 无 | VXLAN Network Identifier，范围 `1..16777215`，节点内唯一 |
| `devices.<name>.link` | 是 | 无 | 同一节点中的 linked underlay interface |
| `devices.<name>.local` | 是 | 无 | 配置在 `link` 上的单播 IPv4/IPv6 源地址 |
| `devices.<name>.remote` | 是 | 无 | 与 `local` 地址族相同的静态单播 VTEP 地址 |
| `devices.<name>.addresses` | 否 | `[]` | 配置在 VXLAN 接口上的 IPv4/IPv6 地址 |
| `devices.<name>.dst_port` | 否 | `4789` | UDP 目的端口，范围 `1..65535` |
| `devices.<name>.learning` | 否 | `true` | 是否开启源 MAC 学习 |
| `devices.<name>.mtu` | 否 | 自动 | 上限为 underlay MTU 减封装开销 |

underlay `link` 必须是 linked interface，并包含完全相同的 `local` 地址。自动 MTU 在
IPv4 下减 50 字节，在 IPv6 下减 70 字节。独立 VXLAN 不设置 bridge master，因此可以
作为 `routes[].dev`，示例见 `examples/vxlan/nslab.yaml`。

###### `type: bond`

Bond 把两个或更多 linked interface 组合成一个逻辑接口。IP 地址和路由配置在 bond
上，成员接口不能声明地址。所有成员链路必须使用相同 MTU，一个 linked interface 不能
同时属于多个 bond。

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `devices.<name>.type` | 是 | 无 | 必须是 `bond` |
| `devices.<name>.mode` | 是 | 无 | `active-backup` 或 `802.3ad` |
| `devices.<name>.interfaces` | 是 | 无 | 至少两个不重复的 linked member interface |
| `devices.<name>.addresses` | 否 | `[]` | 配置在 bond 上且不重复的 IPv4/IPv6 CIDR 地址 |
| `devices.<name>.miimon_ms` | 否 | `100` | `0..60000` ms 的 MII carrier 轮询间隔；零表示禁用 |
| `devices.<name>.primary` | 否 | `null` | 首选成员；仅用于 `active-backup`，且必须属于成员列表 |
| `devices.<name>.lacp_rate` | 否 | `slow` | `slow` 或 `fast`；仅用于 `802.3ad` |
| `devices.<name>.xmit_hash_policy` | 否 | `layer2` | `layer2`、`layer2+3` 或 `layer3+4`；仅用于 `802.3ad` |
| `devices.<name>.min_links` | 否 | `0` | `0..65535` 的最少活动链路数；仅用于 `802.3ad`，且不能超过成员数量 |

`802.3ad` 的对端也必须运行 LACP。单流通常只会哈希到一个成员，需要多条流才能观察
跨链路分担。路由和动态路由可以使用 bond，但 bond 当前不能作为 VLAN parent 或 VRF
member。

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
| `devices` | 否 | `{}` | 挂到此 bridge 的静态 VXLAN 设备 |
| `bridge` | 是 | 无 | Linux bridge 设备及端口配置对象 |

```yaml
sw1:
  kind: bridge
  interfaces:
    underlay0:
      addresses: [192.0.2.1/30]
  devices:
    vxlan100:
      type: vxlan
      vni: 100
      link: underlay0
      local: 192.0.2.1
      remote: 192.0.2.2
  bridge:
    name: br0
    stp: true
    vlan_filtering: false
```

##### `devices.<name>`：`type: vxlan`

Bridge 节点中的 VXLAN 设备创建静态单播二层隧道，并自动加入 `bridge.name`。它的 lower `link` 必须是
同一节点的 linked interface，且 `local` 地址必须准确配置在该接口的 `interfaces` 中；
这个 underlay 接口不会加入 bridge。

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `type` | 是 | 无 | 必须为 `vxlan` |
| `vni` | 是 | 无 | VXLAN Network Identifier，范围 `1..16777215`，同一节点内唯一 |
| `link` | 是 | 无 | 同一 bridge 节点中的 linked underlay interface |
| `local` | 是 | 无 | 配置在 `link` 上的单播 IPv4/IPv6 源地址 |
| `remote` | 是 | 无 | 静态单播远端 VTEP 地址，地址族必须与 `local` 相同 |
| `dst_port` | 否 | `4789` | UDP 目的端口，范围 `1..65535` |
| `learning` | 否 | `true` | 是否在 VXLAN 接口上开启源 MAC 学习 |
| `mtu` | 否 | 自动 | VXLAN MTU，范围 `576..9216`，上限为 underlay MTU 减封装开销 |

自动 MTU 在 IPv4 underlay 上减 50 字节，在 IPv6 underlay 上减 70 字节。自定义值不能
超过该上限。`local` 和 `remote` 不能相同，也不能是 unspecified 或 multicast 地址。
内核会为静态 remote 安装永久的全零 MAC FDB 条目。`bridge.ports` 可以引用 VXLAN
设备来配置 STP 或 VLAN，但不能引用 VXLAN underlay interface。Bridge 节点的 VXLAN
设备不能声明 `addresses`；三层 VXLAN 请使用 Linux 节点。

##### `bridge`

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `name` | 是 | 无 | namespace 内的 bridge 设备名，不能是 `lo` |
| `stp` | 是 | 无 | 是否开启 Linux bridge STP |
| `vlan_filtering` | 是 | 无 | 是否开启 VLAN-aware filtering |
| `priority` | 否 | `null` | Bridge priority，范围 `0..65535` |
| `ports` | 否 | `{}` | Linked access 或 VXLAN 端口名到 STP/VLAN 配置的映射 |

`bridge.name` 不能与链接 endpoint 使用同名接口。若要给 bridge 本身配置 IP，可在节点
`interfaces` 中使用相同的 `bridge.name`。

###### `bridge.ports.<ifname>`

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `path_cost` | 否 | `null` | STP path cost，范围 `1..65535`，要求 `stp: true` |
| `priority` | 否 | `null` | Linux STP port priority，范围 `0..63`，要求 `stp: true` |
| `vlans` | 否 | `[]` | 端口 VLAN 列表，要求 `vlan_filtering: true` |

端口配置至少要包含一个 STP 或 VLAN 设置；端口必须是 linked access interface 或已声明
的 VXLAN 设备。

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
| `netem` | 否 | `null` | 同时应用到两端 egress 的 netem 条件；不能与 `qdisc` 同时使用 |
| `qdisc` | 否 | `null` | 同时应用到两端 egress 的根 qdisc，目前支持 `tbf` 和 `fq_codel` |

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
| `rate` | 否 | `null` | 可选 egress 速率，例如 `10mbit`、`500kbit` 或 `1gbit` |

四个值不能同时为零。netem 同时安装在 veth 两端，因此双向 ping 会在 request 和 reply
方向各经历一次 egress 条件。

```yaml
netem:
  rate: 10mbit
  delay_ms: 20
  jitter_ms: 5
  loss_percent: 1
```

#### `links[].qdisc`

`qdisc` 选择一个根 egress 队列规则。它不能与同一链路的 `netem` 同时使用，并会安装
在 veth 两端。

令牌桶限速 (`tbf`)：

```yaml
qdisc:
  kind: tbf
  rate: 10mbit
  burst: 32kb
  latency_ms: 400
```

`burst` 是正的字节数，也可以使用 `kb`、`mb` 或 `gb` 后缀。`latency_ms` 用于推导 TBF
队列字节上限。

公平队列和受控延迟 (`fq_codel`)：

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

## Manifest 边界

Manifest 不接受 `traffic`、`observe`、抓包或任意命令字段。流量和观察动作使用
[`nslab exec`](cli.md#exec) 显式执行。所有可运行配置见[实验示例](examples/index.md)。
