# OSPFv2 动态路由

## 实验目标

三台 Linux 路由器运行独立的 FRRouting `zebra` 和 `ospfd`，形成 OSPFv2 三角形。
`r1` 到 `r3` 的直连链路提供备用路径，可观察邻居、学习路由和故障收敛。

## 拓扑图

```console
$ nslab graph
Topology: ospf

r1 [linux]
  eth0: 10.0.12.1/30
  eth1: 10.0.13.1/30
  eth2: 192.0.1.1/24
├─ eth2 ↔ eth0  h1 [linux]
│               eth0: 192.0.1.2/24
├─ eth0 ↔ eth0  r2 [linux]
│               eth0: 10.0.12.2/30
│               eth1: 10.0.23.1/30
└─ eth1 ↔ eth1  r3 [linux]
                eth0: 10.0.23.2/30
                eth1: 10.0.13.2/30
                eth2: 192.0.3.1/24
                └─ eth2 ↔ eth0  h2 [linux]
                                eth0: 192.0.3.2/24
Cross-links:
  ↩ [L2] r2:eth1 ↔ r3:eth0
```

## 准备和运行

```bash
sudo apt install -y frr frr-pythontools
cd examples/ospf
sudo nslab deploy
sudo nslab inspect
```

`deploy` 只等待 daemon 启动，不等待 OSPF 邻居收敛。

## 查看邻居和路由

```bash
sudo nslab exec --node r1 -- vtysh -N nslab-ospf-r1 -c "show ip ospf neighbor"
sudo nslab exec --node r1 -- vtysh -N nslab-ospf-r1 -c "show ip route ospf"
sudo nslab exec --node r1 -- ip -4 route
sudo nslab exec --node h1 -- ping -c 3 192.0.3.2
```

邻居进入 `Full` 后，`r1` 应安装到 `192.0.3.0/24` 的 OSPF 路由。

## 观察故障收敛

```bash
sudo nslab exec --node r1 -- ip link set eth0 down
sleep 10
sudo nslab exec --node r1 -- vtysh -N nslab-ospf-r1 -c "show ip ospf neighbor"
sudo nslab exec --node r1 -- ip -4 route get 192.0.3.2
sudo nslab exec --node h1 -- ping -c 3 192.0.3.2
sudo nslab exec --node r1 -- ip link set eth0 up
```

## 清理

```bash
sudo nslab destroy
```

[查看 nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/ospf/nslab.yaml) ·
[查看示例 README](https://github.com/calcky/nslab/blob/main/examples/ospf/README.md)
