# eBGP 动态路由实验

这个实验用三个不同自治系统组成一条 eBGP 链路：`r1 (65001)`、`r2 (65002)` 和
`r3 (65003)`。两端 LAN 前缀由边缘路由器发布，`r2` 通过 eBGP 学习并转发它们。

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

`routing.bgp.local_as` 是本地 AS，`neighbors` 声明直连邻居及其 remote AS，
`networks` 声明要发布的 IPv4 前缀。省略 `networks` 时，nslab 会发布该节点的所有
直连 IPv4 网段，便于快速搭建实验。

## 查看 BGP 会话和路由

```bash
sudo nslab exec --node r1 -- vtysh -N nslab-bgp-r1 -c "show ip bgp summary"
sudo nslab exec --node r2 -- vtysh -N nslab-bgp-r2 -c "show ip bgp"
sudo nslab exec --node r2 -- ip -4 route
sudo nslab exec --node h1 -- ping -c 3 198.18.3.2
```

会话建立后，`r2` 应从 `r1` 学到 `198.18.1.0/24`，从 `r3` 学到
`198.18.3.0/24`。FRR 安装的动态路由会出现在内核主路由表中。

## 观察会话撤销

```bash
sudo nslab exec --node r2 -- ip link set eth0 down
sleep 5
sudo nslab exec --node r2 -- vtysh -N nslab-bgp-r2 -c "show ip bgp summary"
sudo nslab exec --node r2 -- ip -4 route
sudo nslab exec --node r2 -- ip link set eth0 up
```

## 清理

```bash
sudo nslab destroy
```
