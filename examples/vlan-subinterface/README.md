# VLAN 子接口实验

这个实验在一条 veth 链路的两端分别创建 VLAN 10 子接口。IPv4 地址只配置在
`vlan10` 上，底层 `eth0` 仅承载带 802.1Q tag 的帧。

## 拓扑图

```console
$ nslab graph --format mermaid
flowchart LR
    n0["h1\nlinux\nvlan10: vlan 10 on eth0"]
    n1["h2\nlinux\nvlan10: vlan 10 on eth0"]
    n0 -- "eth0 <-> eth0" --- n1
```

```mermaid
flowchart LR
    n0["h1\nlinux\nvlan10: vlan 10 on eth0"]
    n1["h2\nlinux\nvlan10: vlan 10 on eth0"]
    n0 -- "eth0 <-> eth0" --- n1
```

以下为典型输出；接口索引、MAC 地址和 ICMP 时延会随运行变化。

## 部署

```console
$ sudo nslab deploy
deployed topology: vlan-subinterface

$ sudo nslab inspect
status: deployed

NAME  KIND   STATUS    NAMESPACE
----  -----  --------  ------------------------------------
h1    linux  matching  nslab-vlan-subinterface-h1-...
h2    linux  matching  nslab-vlan-subinterface-h2-...
```

## 查看 VLAN 设备

```console
$ sudo nslab exec --node h1 -- ip -d link show vlan10
<index>: vlan10@eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 ... state UP ...
    link/ether <mac> brd ff:ff:ff:ff:ff:ff
    vlan protocol 802.1Q id 10 <REORDER_HDR>

$ sudo nslab exec --node h1 -- ip -4 address show vlan10
<index>: vlan10@eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> ...
    inet 192.0.2.1/24 scope global vlan10

$ sudo nslab exec --node h1 -- ip -4 route show
192.0.2.0/24 dev vlan10 proto kernel scope link src 192.0.2.1
```

`vlan10@eth0` 表示 `vlan10` 的 lower device 是 `eth0`；`id 10` 是收发时加入或匹配的
802.1Q VLAN ID。

## 验证通信

```console
$ sudo nslab exec --node h1 -- ping -c 3 192.0.2.2
PING 192.0.2.2 (192.0.2.2) 56(84) bytes of data.
64 bytes from 192.0.2.2: icmp_seq=1 ttl=64 time=<time> ms
...
3 packets transmitted, 3 received, 0% packet loss
```

## 清理

```console
$ sudo nslab destroy
destroyed topology: vlan-subinterface
```
