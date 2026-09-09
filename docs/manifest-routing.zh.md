# Manifest：路由

##### `routes[]`

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `dst` | 是 | 无 | IPv4/IPv6 目标前缀；`default` 等价于 `0.0.0.0/0` |
| `via` | 否 | `null` | 单路径下一跳地址，地址族必须与 `dst` 一致；不能与 `nexthops` 同时使用 |
| `dev` | 条件必填 | `null` | 单路径出接口；未配置 `nexthops` 时必填 |
| `nexthops` | 条件必填 | `[]` | 两个或更多 ECMP 下一跳；不能与顶层 `via`、`dev` 同时使用 |
| `table` | 否 | 自动 | 路由表 ID，范围 `1..4294967295`，不能使用 local table `255` |

同一路由表中不能重复声明目标前缀，也不能把该表的直连网段再次声明为静态路由。
省略 `table` 时使用 main table 254，VRF 成员接口则自动使用对应 VRF table。若对 VRF
成员路由显式指定 `table`，其值必须与该 VRF 的 table 相同。

Multipath route 使用 `nexthops` 取代顶层 `via` 和 `dev`：

```yaml
routes:
  - dst: 192.0.2.0/24
    nexthops:
      - via: 10.0.12.2
        dev: eth1
        weight: 1
      - via: 10.0.13.2
        dev: eth2
        weight: 1
```

每个 `nexthops[]` 项包含：

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `via` | 否 | `null` | 与目标地址族相同的 gateway；直连下一跳可省略 |
| `dev` | 是 | 无 | 可用出接口 |
| `weight` | 否 | `1` | 相对权重，范围 `1..256` |

一条 multipath route 至少需要两个唯一的 `via + dev` 组合。省略 `table` 时，所有下一跳
接口必须归属于同一路由表；因此可以在一个 VRF 内使用 ECMP，但会拒绝意外跨越路由域的
配置。相同权重是 ECMP，不同权重是加权多路径；分流按多个 flow 统计接近权重，而不是
逐包轮询。

##### `neighbors[]`

`neighbors` 声明 IPv4 ARP 或 IPv6 NDP 表项。普通条目将 IP 地址映射到固定链路层地址；
代理条目使 Linux 代替通过其他接口到达的地址响应地址解析请求。

```yaml
neighbors:
  - dst: 192.0.2.2
    dev: eth0
    lladdr: 02:00:00:00:00:02
    state: permanent
  - dst: 2001:db8:1::200
    dev: eth0
    proxy: true
```

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `dst` | 是 | 无 | 单播 IPv4 或 IPv6 邻居地址 |
| `dev` | 是 | 无 | 承载条目的 linked interface、bridge interface 或已声明设备 |
| `lladdr` | 仅普通条目 | 无 | 冒号分隔的单播邻居 MAC 地址 |
| `state` | 否 | `permanent` | 普通条目的 NUD 状态：`permanent`、`reachable`、`stale` 或 `noarp` |
| `proxy` | 否 | `false` | 安装代理条目，而不是普通 IP 到 MAC 映射 |

每个 `dst + dev` 组合必须唯一。普通条目必须声明 `lladdr`；代理条目不能声明 `lladdr`
或 `state`。声明 IPv4 代理会自动开启 `net.ipv4.conf.<dev>.proxy_arp`；声明 IPv6 代理会
自动开启 `net.ipv6.conf.<dev>.proxy_ndp`。

`permanent` 不会老化；`reachable` 表示已确认可达；`stale` 保留 MAC，并在使用时要求 NUD
重新确认；`noarp` 禁止邻居探测。正常流量可能使已声明的 `reachable` 或 `stale` 条目在
`reachable`、`stale`、`delay` 和 `probe` 之间迁移，inventory 和 drift 检查会把这些健康
迁移视为匹配。Manifest 中未声明的动态学习条目会被忽略。

##### `sysctls`

当前只接受以下 key，value 必须是整数 `0` 或 `1`：

| Key | 说明 |
| --- | --- |
| `net.ipv4.ip_forward` | 关闭或开启 IPv4 转发 |
| `net.ipv6.conf.all.forwarding` | 关闭或开启 IPv6 转发 |

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

##### `routing`

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `routing.ospf` | 条件必填 | `null` | OSPFv2 配置 |
| `routing.bgp` | 条件必填 | `null` | IPv4 eBGP 配置 |
| `routing.pim` | 条件必填 | `null` | IPv4 PIM-SM 与 IGMP 配置 |

声明 `routing` 时至少启用一个协议，多个协议可以同时启用。节点必须设置
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

###### `routing.pim`

| 字段 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `rp_address` | 是 | 无 | 静态 RP 的单播 IPv4 地址 |
| `interfaces` | 是 | 无 | 启用 PIM-SM 的非空接口列表 |
| `igmp_interfaces` | 否 | `[]` | 同时启用 IGMP 的 PIM 接口列表 |

列表中的接口必须存在并配置 IPv4 地址，接口名不能重复；每个 IGMP 接口也必须出现在
`interfaces` 中。同一拓扑中的所有 PIM 节点必须使用相同 RP，nslab 会把它映射到 ASM
范围 `224.0.0.0/4`。RP 地址必须能通过单播路由表到达，通常由 OSPF 或 BGP 提供路由。
在 RP 节点上，还应当对承载 RP 地址的 loopback 或 dummy 接口启用 PIM。

`pimd` 运行时，FRRouting 和内核会自动创建内部接口 `pimreg`。nslab 在 drift 检查中把它
视为运行时资源，并在 PIM 节点上保留该名称。
