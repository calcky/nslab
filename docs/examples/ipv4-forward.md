# Linux IPv4 转发

## 实验目标

`r1` 连接两个 IPv4 子网并开启 `net.ipv4.ip_forward=1`。两台主机通过精确静态路由
访问对端网段，用于观察 Linux 路由选择和转发路径。

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

```bash
cd examples/ipv4-forward
```

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

## 观察路由和转发

```console
$ sudo nslab exec --node r1 -- cat /proc/sys/net/ipv4/ip_forward
1

$ sudo nslab exec --node r1 -- ip -4 address show
... eth0 ...
    inet 192.0.2.1/24 scope global eth0
... eth1 ...
    inet 198.51.100.1/24 scope global eth1

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

## 验证通信

```console
$ sudo nslab exec --node h1 -- ping -c 3 198.51.100.2
64 bytes from 198.51.100.2: icmp_seq=1 ttl=63 time=<time> ms
...
3 packets transmitted, 3 received, 0% packet loss

$ sudo nslab exec --node h2 -- ping -c 3 192.0.2.2
64 bytes from 192.0.2.2: icmp_seq=1 ttl=63 time=<time> ms
...
3 packets transmitted, 3 received, 0% packet loss

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

[查看 nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/ipv4-forward/nslab.yaml) ·
[查看示例 README](https://github.com/calcky/nslab/blob/main/examples/ipv4-forward/README.md)
