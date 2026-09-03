# Linux 邻居表与代理

## 实验目标

这个实验同时演示固定接口 MAC、静态 ARP/NDP，以及 Proxy ARP/NDP。`h1` 认为
`192.0.2.200` 和 `2001:db8:1::200` 位于本地链路；`r1` 代理地址解析，再把数据包路由到
`h2` 的 `service0` dummy 设备。

## 拓扑图

```bash
nslab graph --format mermaid
```

```mermaid
flowchart LR
    n0["h1\nlinux"]
    n1["r1\nlinux"]
    n2["h2\nlinux\nservice0: dummy"]
    n0 -- "eth0 <-> eth0" --- n1
    n1 -- "eth1 <-> eth0" --- n2
```

以下为典型输出；namespace 后缀、接口索引、邻居状态和 ICMP 时延会随运行变化。

## 部署

```console
$ sudo nslab deploy
deployed topology: neighbors

$ sleep 2 && echo "IPv6 DAD complete"
IPv6 DAD complete

$ sudo nslab inspect
status: deployed

NAME  KIND   STATUS    NAMESPACE
----  -----  --------  ------------------------
h1    linux  matching  nslab-neighbors-h1-...
r1    linux  matching  nslab-neighbors-r1-...
h2    linux  matching  nslab-neighbors-h2-...
```

等待两秒是为了让内核完成 IPv6 duplicate address detection（DAD）。

## 查看固定 MAC

```console
$ sudo nslab exec -N h1 -- ip -brief link show dev eth0
eth0@if...       UP             02:00:00:00:01:01 <BROADCAST,MULTICAST,UP,LOWER_UP>
```

`nslab.yaml` 为四个 veth 端点分别声明了稳定的单播 MAC，静态邻居项因此不会依赖每次部署
随机生成的地址。

## 查看静态 ARP 与 NDP

```console
$ sudo nslab exec -N r1 -- ip -4 neigh show to 198.51.100.2 dev eth1 nud all
198.51.100.2 dev eth1 lladdr 02:00:00:00:02:02 PERMANENT

$ sudo nslab exec -N r1 -- ip -6 neigh show to 2001:db8:2::2 dev eth1 nud all
2001:db8:2::2 dev eth1 lladdr 02:00:00:00:02:02 REACHABLE

$ sudo nslab exec -N h2 -- ip -4 neigh show to 198.51.100.1 dev eth0 nud all
198.51.100.1 dev eth0 lladdr 02:00:00:00:02:01 STALE

$ sudo nslab exec -N h2 -- ip -6 neigh show to 2001:db8:2::1 dev eth0 nud all
2001:db8:2::1 dev eth0 lladdr 02:00:00:00:02:01 NOARP
```

`permanent` 不会老化；`reachable` 是已确认可达；`stale` 保留 MAC，但下次使用时会重新确认；
`noarp` 表示内核不执行邻居探测。使用流量后，`reachable` 或 `stale` 可能正常变为
`delay`、`probe` 或彼此转换，`nslab inspect` 会把这些健康的 NUD 迁移视为匹配。

## 查看 Proxy ARP 与 Proxy NDP

```console
$ sudo nslab exec -N r1 -- ip -4 neigh show proxy dev eth0
192.0.2.200 dev eth0 proxy

$ sudo nslab exec -N r1 -- ip -6 neigh show proxy dev eth0
2001:db8:1::200 dev eth0 proxy

$ sudo nslab exec -N r1 -- cat /proc/sys/net/ipv4/conf/eth0/proxy_arp
1

$ sudo nslab exec -N r1 -- cat /proc/sys/net/ipv6/conf/eth0/proxy_ndp
1
```

声明 `proxy: true` 时，nslab 会自动启用对应接口的 `proxy_arp` 或 `proxy_ndp`。普通
`ip neigh show` 不包含 proxy 条目，需要显式加 `proxy`。

## 验证代理转发

```console
$ sudo nslab exec -N h1 -- ping -4 -c 1 -W 2 192.0.2.200
PING 192.0.2.200 (192.0.2.200) 56(84) bytes of data.
64 bytes from 192.0.2.200: icmp_seq=1 ttl=63 time=<time> ms
1 packets transmitted, 1 received, 0% packet loss

$ sudo nslab exec -N h1 -- ping -6 -c 2 -W 2 2001:db8:1::200
64 bytes from 2001:db8:1::200: icmp_seq=1 ttl=63 time=<time> ms
64 bytes from 2001:db8:1::200: icmp_seq=2 ttl=63 time=<time> ms
2 packets transmitted, 2 received, 0% packet loss

$ sudo nslab inspect
status: deployed
```

`h1` 没有指向 `r1` 的显式 route，而是先在本地链路请求两个服务地址的 MAC。`r1` 代理
应答后完成三层转发；流量引起的健康 NUD 状态变化不会产生 drift。

## 清理

```console
$ sudo nslab destroy
destroyed topology: neighbors
```

[查看 nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/neighbors/nslab.yaml) ·
[查看示例 README](https://github.com/calcky/nslab/blob/main/examples/neighbors/README.md)
