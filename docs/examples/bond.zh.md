# Bond 概览

Linux bonding 将多个接口组合成一个逻辑链路。nslab 保留两个独立、可运行的实验，因为它们
虽然都使用“两台主机、两条链路”的拓扑，但要验证的问题不同。

| 模式 | 演示内容 | 链路行为 | 入口 |
| --- | --- | --- | --- |
| `active-backup` | 首选成员故障切换与恢复 | 同时只有一个成员转发；活动链路故障后切换到备用成员 | [Bond active-backup](bond-active-backup.md) |
| `802.3ad` | LACP 协商与链路聚合 | 多个成员可以同时转发；不同流按哈希分配 | [Bond 802.3ad](bond-8023ad.md) |

## 如何选择

如果目标是学习不依赖对端 LACP 的高可用切换，选择 `active-backup`。这个实验可以直接
观察 carrier 丢失、活动成员变化，以及首选成员恢复后的重新选举。

如果目标是学习链路聚合，选择 `802.3ad`。两端都必须运行 LACP；需要多条流才能观察
成员之间的分担，单条流通常会固定在一条链路上。

## 通用流程

在各自目录中独立运行实验：

```console
$ cd examples/bond-active-backup    # 也可换成 examples/bond-8023ad
$ nslab graph
$ sudo nslab deploy
$ sudo nslab inspect
$ sudo nslab destroy
```

具体的 manifest、预期输出、故障模拟和观察命令见对应页面：

- [Bond active-backup](bond-active-backup.md)：关闭 `eth0` 观察切换，再恢复首选成员。
- [Bond 802.3ad](bond-8023ad.md)：检查 LACP 聚合器并比较多流计数器。

两个实验都使用两个 Linux namespace、两条成员链路，并把 IP 地址配置在 `bond0` 上。
独立 manifest 可以让部署名称、地址范围和预期状态始终聚焦于一种 bonding 模式。
