# Linux bridge FDB

## 实验目标

用两台主机和一台 Linux bridge 观察二层转发、动态 MAC 学习以及端口计数器变化。

## 拓扑图

```console
$ nslab graph
Topology: bridge-fdb

sw1 [bridge · br0]
├─ swp1 ↔ eth0  h1 [linux]
│               eth0: 10.10.0.1/24
└─ swp2 ↔ eth0  h2 [linux]
                eth0: 10.10.0.2/24
```

## 运行

```bash
cd examples/bridge-fdb
sudo nslab deploy
sudo nslab inspect
```

## 观察 FDB

先读取初始 FDB，再发送 ICMP 流量并观察动态表项和计数器：

```bash
sudo nslab exec --node sw1 -- bridge fdb show br br0
sudo nslab exec --node h1 -- ping -c 3 10.10.0.2
sudo nslab exec --node sw1 -- bridge fdb show br br0
sudo nslab exec --node sw1 -- ip -s link show swp1
sudo nslab exec --node sw1 -- ip -s link show swp2
```

收到数据帧后，`sw1` 会把源 MAC 地址关联到入端口；`swp1` 和 `swp2` 的 RX/TX
计数也会增加。

## 清理

```bash
sudo nslab destroy
```

[查看 nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/bridge-fdb/nslab.yaml) ·
[查看示例 README](https://github.com/calcky/nslab/blob/main/examples/bridge-fdb/README.md)
