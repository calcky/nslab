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
| [Bridge STP](bridge-stp.md) | 根桥选举、端口选择和故障重收敛 |

## IP 转发

| 示例 | 学习内容 |
| --- | --- |
| [IPv4 转发](ipv4-forward.md) | IPv4 静态路由和 Linux 转发路径 |
| [IPv6 转发](ipv6-forward.md) | IPv6 默认路由、DAD 和 Linux 转发路径 |

## 链路条件

| 示例 | 学习内容 |
| --- | --- |
| [netem](netem.md) | 双向延迟、抖动、随机丢包和 qdisc 统计 |

## 动态路由

| 示例 | 学习内容 |
| --- | --- |
| [OSPFv2](ospf.md) | 邻居建立、链路状态路由和故障收敛 |
| [eBGP](bgp.md) | 邻居建立、AS_PATH 传播和边界路由 |

!!! note "权限和依赖"

    `graph` 不需要 root。创建 namespace、bridge、veth 或 qdisc 需要 root。OSPF/BGP
    页面还需要系统安装 `frr` 和 `frr-pythontools`。
