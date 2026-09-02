# Linux IPv4 转发

## 实验目标

`r1` 连接两个 IPv4 子网并开启 `net.ipv4.ip_forward=1`。两台主机通过精确静态路由
访问对端网段，用于观察 Linux 路由选择和转发路径。

## 拓扑图

```console
$ nslab graph
Topology: ipv4-forward

r1 [linux]
  eth0: 192.0.2.1/24
  eth1: 198.51.100.1/24
├─ eth0 ↔ eth0  h1 [linux]
│               eth0: 192.0.2.2/24
└─ eth1 ↔ eth0  h2 [linux]
                eth0: 198.51.100.2/24
```

## 运行

```bash
cd examples/ipv4-forward
sudo nslab deploy
sudo nslab inspect
```

## 观察路由和转发

```bash
sudo nslab exec --node r1 -- cat /proc/sys/net/ipv4/ip_forward
sudo nslab exec --node r1 -- ip -4 address show
sudo nslab exec --node h1 -- ip -4 route show
sudo nslab exec --node h1 -- ip -4 route get 198.51.100.2
sudo nslab exec --node h2 -- ip -4 route show
```

转发开关应为 `1`；`h1` 到 `198.51.100.0/24` 的下一跳应为 `192.0.2.1`。

## 验证通信

```bash
sudo nslab exec --node h1 -- ping -c 3 198.51.100.2
sudo nslab exec --node h2 -- ping -c 3 192.0.2.2
sudo nslab exec --node r1 -- ip -s link show eth0
sudo nslab exec --node r1 -- ip -s link show eth1
```

ICMP 包经过 `r1` 后 TTL 会减一，路由器两个接口的计数器会增加。

## 清理

```bash
sudo nslab destroy
```

[查看 nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/ipv4-forward/nslab.yaml) ·
[查看示例 README](https://github.com/calcky/nslab/blob/main/examples/ipv4-forward/README.md)
