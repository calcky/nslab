# Linux IPv6 转发

## 实验目标

`r1` 连接两个 `/64` 子网并开启 `net.ipv6.conf.all.forwarding=1`。两台主机使用显式
IPv6 默认路由访问对端网段。

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

以下为典型输出；link-local 地址、接口索引、计数器和 ICMP 时延会随运行变化。

## 运行

```bash
cd examples/ipv6-forward
```

```console
$ sudo nslab deploy
deployed topology: ipv6-forward
```

等待 DAD 完成后检查状态：

```bash
sleep 2
```

```console
$ sudo nslab inspect
status: deployed

NAME  KIND   STATUS    NAMESPACE
----  -----  --------  -------------------------------
h1    linux  matching  nslab-ipv6-forward-h1-...
r1    linux  matching  nslab-ipv6-forward-r1-...
h2    linux  matching  nslab-ipv6-forward-h2-...
```

## 观察路由和转发

```console
$ sudo nslab exec --node r1 -- cat /proc/sys/net/ipv6/conf/all/forwarding
1

$ sudo nslab exec --node r1 -- ip -6 address show
... eth0 ...
    inet6 2001:db8:1::1/64 scope global
    inet6 fe80::.../64 scope link
... eth1 ...
    inet6 2001:db8:2::1/64 scope global
    inet6 fe80::.../64 scope link

$ sudo nslab exec --node h1 -- ip -6 route show
2001:db8:1::/64 dev eth0 proto kernel metric 256 pref medium
default via 2001:db8:1::1 dev eth0 metric 1024 pref medium

$ sudo nslab exec --node h1 -- ip -6 route get 2001:db8:2::2
2001:db8:2::2 via 2001:db8:1::1 dev eth0 src 2001:db8:1::2 metric 1024 pref medium

$ sudo nslab exec --node h2 -- ip -6 route show
2001:db8:2::/64 dev eth0 proto kernel metric 256 pref medium
default via 2001:db8:2::1 dev eth0 metric 1024 pref medium
```

## 验证通信

```console
$ sudo nslab exec --node h1 -- ping -6 -c 3 2001:db8:2::2
64 bytes from 2001:db8:2::2: icmp_seq=1 ttl=63 time=<time> ms
...
3 packets transmitted, 3 received, 0% packet loss

$ sudo nslab exec --node h2 -- ping -6 -c 3 2001:db8:1::2
64 bytes from 2001:db8:1::2: icmp_seq=1 ttl=63 time=<time> ms
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
destroyed topology: ipv6-forward
```

[查看 nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/ipv6-forward/nslab.yaml) ·
[查看示例 README](https://github.com/calcky/nslab/blob/main/examples/ipv6-forward/README.md)
