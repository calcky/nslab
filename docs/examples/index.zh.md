# 实验示例

每个示例都有独立页面、真实的 `nslab graph` 输出以及部署、观察和清理命令。示例目录
同时保留可重复执行的 `nslab.yaml` 和 README。

通用流程：

```bash
cd examples/<lab>
nslab graph
sudo nslab deploy
sudo nslab inspect
# 执行实验页中的观察命令
sudo nslab destroy
```

## 二层交换

| 示例 | 学习内容 |
| --- | --- |
| [Bridge FDB](bridge-fdb.md) | Linux bridge 转发、MAC 学习和接口计数器 |
| [Bridge VLAN](bridge-vlan.md) | Access VLAN、PVID、untagged 和 tagged trunk |
| [VLAN](vlan.md) | 802.1Q 子接口与单臂路由转发 |
| [Bridge STP](bridge-stp.md) | 根桥选举、端口选择和故障重收敛 |
| [VXLAN](vxlan.md) | Bridge 二层 overlay 和独立三层静态路由 |
| [Geneve](geneve.md) | 静态单播二层 Geneve overlay |

## Namespace 接口

| 示例 | 学习内容 |
| --- | --- |
| [虚拟设备](virtual-devices.md) | `dummy`、`macvlan` 和 `ipvlan` 接口 |

## 隧道设备

| 示例 | 学习内容 |
| --- | --- |
| [GRE 与 IPIP](ip-tunnels.md) | 带 key 的 GRE 与 IPv4-in-IPv4 点到点隧道 |

## 链路聚合

| 示例 | 学习内容 |
| --- | --- |
| [Bond 概览](bond.md) | 对比 bonding 模式并选择可运行实验 |
| [Bond active-backup](bond-active-backup.md) | 首选链路的故障切换与恢复 |
| [Bond 802.3ad](bond-8023ad.md) | LACP 协商与逐流哈希 |

## IP 转发

| 示例 | 学习内容 |
| --- | --- |
| [IPv4 转发](ipv4-forward.md) | IPv4 静态路由和 Linux 转发路径 |
| [IPv6 转发](ipv6-forward.md) | IPv6 默认路由、DAD 和 Linux 转发路径 |

## 路由域隔离

| 示例 | 学习内容 |
| --- | --- |
| [Linux VRF](vrf.md) | 多路由表、接口归属和重叠地址空间 |
| [策略路由](policy-routing.md) | RPDB selector、packet mark 和 rule 指定路由表 |

## 链路条件

| 示例 | 学习内容 |
| --- | --- |
| [netem](netem.md) | 双向延迟、抖动、随机丢包和 qdisc 统计 |
| [qdisc](qdisc.md) | netem 速率、令牌桶整形和 fq_codel |

## 数据包处理

| 示例 | 学习内容 |
| --- | --- |
| [XDP 收发包](xdp.md) | `XDP_PASS`、`XDP_DROP`、`XDP_TX`、`XDP_REDIRECT` 和 BPF map 计数器 |

## 动态路由

| 示例 | 学习内容 |
| --- | --- |
| [OSPFv2](ospf.md) | 邻居建立、链路状态路由和故障收敛 |
| [eBGP](bgp.md) | 邻居建立、AS_PATH 传播和边界路由 |

!!! note "权限和依赖"

    `graph` 不需要 root。创建 namespace、bridge、veth 或 qdisc 需要 root。OSPF/BGP
    页面还需要系统安装 `frr` 和 `frr-pythontools`。XDP 页面需要 Clang、libbpf
    头文件和 bpftool。
