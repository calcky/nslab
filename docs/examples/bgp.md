# eBGP 动态路由

## 实验目标

`r1`、`r2`、`r3` 分属 AS 65001、65002、65003，形成 eBGP 链。两端 LAN 前缀通过
BGP 传播，用于观察邻居状态、前缀接收和 AS_PATH。

## 拓扑图

```console
$ nslab graph
Topology: bgp

r1 [linux]
  eth0: 10.1.12.1/30
  eth1: 198.18.1.1/24
├─ eth1 ↔ eth0  h1 [linux]
│               eth0: 198.18.1.2/24
└─ eth0 ↔ eth0  r2 [linux]
                eth0: 10.1.12.2/30
                eth1: 10.1.23.1/30
                └─ eth1 ↔ eth0  r3 [linux]
                                eth0: 10.1.23.2/30
                                eth1: 198.18.3.1/24
                                └─ eth1 ↔ eth0  h2 [linux]
                                                eth0: 198.18.3.2/24
```

## 准备和运行

```bash
sudo apt install -y frr frr-pythontools
cd examples/bgp
sudo nslab deploy
sudo nslab inspect
```

## 查看邻居和路由

每个节点使用独立 FRR pathspace。以 `r2` 为例：

```bash
sudo nslab exec --node r2 -- vtysh -N nslab-bgp-r2 -c "show ip bgp summary"
sudo nslab exec --node r2 -- vtysh -N nslab-bgp-r2 -c "show ip bgp"
sudo nslab exec --node r2 -- ip -4 route
```

会话建立后，`r2` 应从两侧收到 `198.18.1.0/24` 和 `198.18.3.0/24`。继续验证端到端
通信：

```bash
sudo nslab exec --node h1 -- ping -c 3 198.18.3.2
sudo nslab exec --node r1 -- vtysh -N nslab-bgp-r1 -c "show ip bgp"
```

`deploy` 成功只代表 daemon 已启动；应以 `show ip bgp summary` 确认邻居收敛。

## 清理

```bash
sudo nslab destroy
```

[查看 nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/bgp/nslab.yaml) ·
[查看示例 README](https://github.com/calcky/nslab/blob/main/examples/bgp/README.md)
