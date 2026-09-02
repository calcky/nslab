# Linux bridge STP

## 实验目标

四台 Linux bridge 构成冗余二层拓扑，用于观察根桥选举、等 cost 链路的端口优先级、
path cost 以及链路故障后的 STP 重收敛。

## 拓扑图

```console
$ nslab graph
Topology: bridge-stp

sw1 [bridge · br0]
├─ host1 ↔ eth0  h1 [linux]
│                eth0: 10.20.0.1/24
├─ swp1 ↔ swp1  sw2 [bridge · br0]
│               └─ swp3 ↔ swp1  sw4 [bridge · br0]
│                               └─ host1 ↔ eth0  h2 [linux]
│                                                eth0: 10.20.0.2/24
└─ swp3 ↔ swp1  sw3 [bridge · br0]
Cross-links:
  ↩ [L2] sw1:swp2 ↔ sw2:swp2
  ↩ [L5] sw3:swp2 ↔ sw4:swp2
```

`nslab graph --detail` 会同时显示 bridge priority、端口 priority 和 path cost。

## 运行并等待收敛

```bash
cd examples/bridge-stp
sudo nslab deploy
sudo nslab inspect
sleep 35
```

经典 Linux bridge STP 首次进入 forwarding 状态可能需要约 30 秒。

## 观察端口角色

```bash
sudo nslab exec --node sw1 -- ip -d link show br0
sudo nslab exec --node sw2 -- bridge -d link show
sudo nslab exec --node sw4 -- bridge -d link show
sudo nslab exec --node h1 -- ping -c 3 10.20.0.2
```

`sw1` 应成为根桥。正常情况下，`sw4:swp1` forwarding，cost 更高的 `sw4:swp2`
blocking。

## 验证故障切换

```bash
sudo nslab exec --node sw4 -- ip link set swp1 down
sleep 35
sudo nslab exec --node sw4 -- bridge -d link show
sudo nslab exec --node h2 -- ping -c 1 10.20.0.1
sudo nslab exec --node h1 -- ping -c 3 10.20.0.2
sudo nslab exec --node sw4 -- ip link set swp1 up
```

端口关闭期间 `inspect` 报告 `degraded` 是预期行为。先从 `h2` 发包可以帮助新路径
重新学习 MAC 地址。

## 清理

```bash
sudo nslab destroy
```

[查看 nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/bridge-stp/nslab.yaml) ·
[查看示例 README](https://github.com/calcky/nslab/blob/main/examples/bridge-stp/README.md)
