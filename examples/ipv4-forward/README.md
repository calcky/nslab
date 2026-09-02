# Linux IPv4 转发实验

这个实验用于观察 Linux 的 IPv4 路由与转发路径。`r1` 连接两个不同子网，并通过
`net.ipv4.ip_forward=1` 转发数据包：

```text
h1                 r1                           h2
192.0.2.2/24  --  192.0.2.1/24 | 198.51.100.1/24  --  198.51.100.2/24
```

`h1` 和 `h2` 都只配置到对端子网的精确静态路由，没有使用默认路由。

## 运行

```bash
nslab graph --detail
sudo nslab deploy
sudo nslab inspect
```

## 观察转发配置

分别检查路由器的转发开关、两端路由表和内核选出的下一跳：

```bash
sudo nslab exec --node r1 -- cat /proc/sys/net/ipv4/ip_forward
sudo nslab exec --node r1 -- ip -4 address show
sudo nslab exec --node h1 -- ip -4 route show
sudo nslab exec --node h1 -- ip -4 route get 198.51.100.2
sudo nslab exec --node h2 -- ip -4 route show
```

转发开关应输出 `1`，`h1` 到 `198.51.100.0/24` 的下一跳应为 `192.0.2.1`，
`h2` 到 `192.0.2.0/24` 的下一跳应为 `198.51.100.1`。

## 验证双向通信

```bash
sudo nslab exec --node h1 -- ping -c 3 198.51.100.2
sudo nslab exec --node h2 -- ping -c 3 192.0.2.2
sudo nslab exec --node r1 -- ip -s link show eth0
sudo nslab exec --node r1 -- ip -s link show eth1
```

ping 的 TTL 会在经过 `r1` 时减一，路由器两个接口的 RX/TX 计数会随流量增加。

## 清理

```bash
sudo nslab destroy
```

修改 `nslab.yaml` 后可以用 `sudo nslab redeploy` 重新创建整个实验。
