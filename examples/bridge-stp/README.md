# Linux bridge STP 实验

这个综合实验用一个冗余二层拓扑观察根桥选举、等 cost 链路的端口优先级、路径 cost
以及链路故障后的 STP 重收敛：

```text
                    sw2
                  ==   \
h1 -- sw1                 sw4 -- h2
          \              /
                   sw3
```

`sw1` 与 `sw2` 之间有两条并行链路。`sw1` 的 bridge priority 最低，因此成为根桥；
`sw4` 经 `sw2` 的路径 cost 低于经 `sw3` 的路径。

## 运行并等待收敛

```bash
nslab graph --detail
sudo nslab deploy
sudo nslab inspect
sleep 35
```

Linux bridge 使用经典 STP 默认定时器，首次进入 forwarding 状态可能需要约 30 秒。

## 观察端口角色

```bash
sudo nslab exec --node sw1 -- ip -d link show br0
sudo nslab exec --node sw2 -- bridge -d link show
sudo nslab exec --node sw4 -- bridge -d link show
sudo nslab exec --node h1 -- ping -c 3 10.20.0.2
```

收敛后的关键状态：

- `sw1` 以 priority 4096 成为根桥。
- `sw1:swp2` 的 port priority 16 优于 `sw1:swp1` 的 32，因此
  `sw2:swp2` forwarding、`sw2:swp1` blocking。
- `sw4:swp1` 的 path cost 为 10，`sw4:swp2` 为 100，因此正常情况下
  `sw4:swp1` forwarding、`sw4:swp2` blocking。

## 验证故障切换

关闭 `sw4` 的主路径，等待备份路径转为 forwarding：

```bash
sudo nslab exec --node sw4 -- ip link set swp1 down
sleep 35
sudo nslab exec --node sw4 -- bridge -d link show
sudo nslab exec --node h2 -- ping -c 1 10.20.0.1
sudo nslab exec --node h1 -- ping -c 3 10.20.0.2
```

此时 `sw4:swp1` 应为 disabled，`sw4:swp2` 应转为 forwarding。STP 已收敛时，旧
FDB 表项仍可能短暂指向故障路径；先从 `h2` 发包可以触发新路径上的 MAC 学习。

手动关闭端口后，`nslab inspect` 报告 `degraded` 是预期行为，因为 live state 与
manifest 不再一致。

## 恢复与清理

```bash
sudo nslab exec --node sw4 -- ip link set swp1 up
sudo nslab destroy
```

重新打开端口后若要继续观察原始状态，需要再次等待 STP 收敛。
