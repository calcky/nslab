# Linux IPv4 转发实验

这个实验用于观察 Linux 的 IPv4 路由与转发路径。`r1` 连接两个不同子网，并通过
`net.ipv4.ip_forward=1` 转发数据包。`h1` 和 `h2` 只配置到对端子网的精确静态路由。

## 拓扑图

```bash
nslab graph --format mermaid
```

```mermaid
flowchart LR
    n0["h1\nlinux"]
    n1["r1\nlinux"]
    n2["h2\nlinux"]
    n0 -- "eth0 <-> eth0" --- n1
    n1 -- "eth1 <-> eth0" --- n2
```

以下为典型输出；接口索引、计数器和 ICMP 时延会随运行变化。

## 运行

```console
$ sudo nslab deploy
deployed topology: ipv4-forward

$ sudo nslab inspect
status: deployed

NAME  KIND   STATUS    NAMESPACE
----  -----  --------  -------------------------------
h1    linux  matching  nslab-ipv4-forward-h1-...
r1    linux  matching  nslab-ipv4-forward-r1-...
h2    linux  matching  nslab-ipv4-forward-h2-...
```

## 观察转发配置

检查转发开关和路由器地址：

```console
$ sudo nslab exec --node r1 -- cat /proc/sys/net/ipv4/ip_forward
1

$ sudo nslab exec --node r1 -- ip -4 address show
... eth0 ...
    inet 192.0.2.1/24 scope global eth0
... eth1 ...
    inet 198.51.100.1/24 scope global eth1
```

两端主机都包含直连路由和到远端网段的静态路由：

```console
$ sudo nslab exec --node h1 -- ip -4 route show
192.0.2.0/24 dev eth0 proto kernel scope link src 192.0.2.2
198.51.100.0/24 via 192.0.2.1 dev eth0

$ sudo nslab exec --node h1 -- ip -4 route get 198.51.100.2
198.51.100.2 via 192.0.2.1 dev eth0 src 192.0.2.2
    cache

$ sudo nslab exec --node h2 -- ip -4 route show
192.0.2.0/24 via 198.51.100.1 dev eth0
198.51.100.0/24 dev eth0 proto kernel scope link src 198.51.100.2
```

## 验证双向通信

回复包经过 `r1`，所以观察到的 TTL 为 63：

```console
$ sudo nslab exec --node h1 -- ping -c 3 198.51.100.2
PING 198.51.100.2 (198.51.100.2) 56(84) bytes of data.
64 bytes from 198.51.100.2: icmp_seq=1 ttl=63 time=<time> ms
...
3 packets transmitted, 3 received, 0% packet loss

$ sudo nslab exec --node h2 -- ping -c 3 192.0.2.2
PING 192.0.2.2 (192.0.2.2) 56(84) bytes of data.
64 bytes from 192.0.2.2: icmp_seq=1 ttl=63 time=<time> ms
...
3 packets transmitted, 3 received, 0% packet loss
```

路由器两个接口的计数器会增加：

```console
$ sudo nslab exec --node r1 -- ip -s link show eth0
... eth0 ... state UP ...
    RX: ... packets ...
    TX: ... packets ...

$ sudo nslab exec --node r1 -- ip -s link show eth1
... eth1 ... state UP ...
    RX: ... packets ...
    TX: ... packets ...
```

## 清理

```console
$ sudo nslab destroy
destroyed topology: ipv4-forward
```

修改 `nslab.yaml` 后可以用 `sudo nslab redeploy` 重新创建整个实验。
