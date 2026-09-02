# Linux 策略路由实验

这个实验让 `r1` 根据 RPDB rule 选择不同的路由表。`isp1` 和 `isp2` 都拥有
`203.0.113.1/32`：来自 `h1` 的普通流量由 `from + to + iif` 规则送入 table 100，
带 mark `2` 的查询优先命中 `fwmark/fwmask` 规则并进入 table 200。

## 拓扑图

```console
$ nslab graph --format mermaid
flowchart LR
    n0["h1\nlinux"]
    n1["r1\nlinux"]
    n2["isp1\nlinux"]
    n3["isp2\nlinux"]
    n0 -- "eth0 <-> eth0" --- n1
    n1 -- "eth1 <-> eth0" --- n2
    n1 -- "eth2 <-> eth0" --- n3
```

```mermaid
flowchart LR
    n0["h1\nlinux"]
    n1["r1\nlinux"]
    n2["isp1\nlinux"]
    n3["isp2\nlinux"]
    n0 -- "eth0 <-> eth0" --- n1
    n1 -- "eth1 <-> eth0" --- n2
    n1 -- "eth2 <-> eth0" --- n3
```

以下为典型输出；namespace 后缀、接口索引和 ICMP 时延会随运行变化。

## 部署

```console
$ cd examples/policy-routing

$ sudo nslab deploy
deployed topology: policy-routing

$ sudo nslab inspect
status: deployed

NAME  KIND   STATUS    NAMESPACE
----  -----  --------  -------------------------------
h1    linux  matching  nslab-policy-routing-h1-...
r1    linux  matching  nslab-policy-routing-r1-...
isp1  linux  matching  nslab-policy-routing-isp1-...
isp2  linux  matching  nslab-policy-routing-isp2-...
```

## 查看 RPDB 和路由表

```console
$ sudo nslab exec -N r1 -- ip -4 rule show
0:      from all lookup local
90:     from all fwmark 0x2/0xff lookup 200
100:    from 192.0.2.0/24 to 203.0.113.0/24 iif eth0 lookup 100
32766:  from all lookup main
32767:  from all lookup default

$ sudo nslab exec -N r1 -- ip -4 route show table 100
203.0.113.1 via 10.0.1.2 dev eth1 proto static

$ sudo nslab exec -N r1 -- ip -4 route show table 200
203.0.113.1 via 10.0.2.2 dev eth2 proto static
```

priority 90 先检查 packet mark；没有命中时，priority 100 再同时检查源前缀、目的前缀和
入接口。两张表拥有相同目标，却使用不同的下一跳和出接口。

## 对比路由查询

```console
$ sudo nslab exec -N r1 -- ip -4 route get 203.0.113.1 from 192.0.2.2 iif eth0
203.0.113.1 from 192.0.2.2 via 10.0.1.2 dev eth1 table 100
    cache iif eth0

$ sudo nslab exec -N r1 -- ip -4 route get 203.0.113.1 from 192.0.2.2 iif eth0 mark 2
203.0.113.1 from 192.0.2.2 via 10.0.2.2 dev eth2 table 200 mark 2
    cache iif eth0
```

第二次只多了 mark `2`，选中的 table、下一跳和出接口便全部改变。

## 验证普通转发

```console
$ sudo nslab exec -N h1 -- ping -c 1 -W 2 203.0.113.1
PING 203.0.113.1 (203.0.113.1) 56(84) bytes of data.
64 bytes from 203.0.113.1: icmp_seq=1 ttl=63 time=<time> ms
1 packets transmitted, 1 received, 0% packet loss
```

`h1` 发出的流量没有 mark，因此经由 `isp1` 往返。

## 清理

```console
$ sudo nslab destroy
destroyed topology: policy-routing
```
