# OSPFv2 动态路由实验

这个实验使用 FRRouting 的 `ospfd` 在三台 Linux 路由器之间建立一个 OSPFv2
三角形。`r1` 和 `r3` 之间的链路是备用路径；关闭 `r1:eth0` 后，路由会经由
`r3` 重新收敛。主机只保留到远端 LAN 的静态路由，路由器之间的远端网段由 OSPF
学习。

安装 daemon（Ubuntu）：

```bash
sudo apt update
sudo apt install -y frr frr-pythontools
```

## 运行

```bash
nslab graph --detail
sudo nslab deploy
sudo nslab inspect
```

部署时 nslab 会为每个配置了 `routing.ospf` 的节点生成 FRR 配置，并启动独立的
`zebra` 和 `ospfd` 进程。配置中的 `networks` 是 OSPF 要发布的前缀，
`passive_interfaces` 只发布主机侧网段而不建立邻居。

## 查看邻居和路由

```bash
sudo nslab exec --node r1 -- vtysh -N nslab-ospf-r1 -c "show ip ospf neighbor"
sudo nslab exec --node r1 -- vtysh -N nslab-ospf-r1 -c "show ip route ospf"
sudo nslab exec --node r1 -- ip -4 route
sudo nslab exec --node h1 -- ping -c 3 192.0.3.2
```

邻居状态稳定后，`r1` 应看到 `r2` 和 `r3`，并安装到 `192.0.3.0/24` 的 OSPF
路由。`nslab inspect --format json` 会保留这些学习到的内核路由，不把它们误报为
拓扑漂移。

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
