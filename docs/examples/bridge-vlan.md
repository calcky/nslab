# Linux bridge VLAN

## 实验目标

两台 VLAN-aware bridge 通过 tagged trunk 承载 VLAN 10 和 VLAN 20。四台主机配置在
同一个 IPv4 子网，用于直接观察二层 VLAN 隔离。

## 拓扑图

```console
$ nslab graph
Topology: bridge-vlan

sw1 [bridge · br0]
├─ access10 ↔ eth0  h10a [linux]
│                   eth0: 10.0.0.1/24
├─ access20 ↔ eth0  h20a [linux]
│                   eth0: 10.0.0.3/24
└─ trunk ↔ trunk  sw2 [bridge · br0]
                  ├─ access10 ↔ eth0  h10b [linux]
                  │                   eth0: 10.0.0.2/24
                  └─ access20 ↔ eth0  h20b [linux]
                                      eth0: 10.0.0.4/24
```

使用 `nslab graph --detail` 还可以显示每个端口的 PVID、untagged 和 trunk VLAN。

## 运行

```bash
cd examples/bridge-vlan
sudo nslab deploy
sudo nslab inspect
```

## 观察和验证

```bash
sudo nslab exec --node sw1 -- bridge vlan show
sudo nslab exec --node sw2 -- bridge vlan show
sudo nslab exec --node h10a -- ping -c 3 10.0.0.2
sudo nslab exec --node h20a -- ping -c 3 10.0.0.4
```

同 VLAN 流量应通过 trunk 正常转发。下面的跨 VLAN ping 应超时，因为 ARP 广播不会
越过 VLAN 边界：

```bash
sudo nslab exec --node h10a -- ping -c 2 -W 1 10.0.0.3
sudo nslab exec --node sw1 -- bridge fdb show br br0
```

## 清理

```bash
sudo nslab destroy
```

[查看 nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/bridge-vlan/nslab.yaml) ·
[查看示例 README](https://github.com/calcky/nslab/blob/main/examples/bridge-vlan/README.md)
