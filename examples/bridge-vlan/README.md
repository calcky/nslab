# Linux bridge VLAN 实验

这个实验使用两台 VLAN-aware Linux bridge。VLAN 10 和 VLAN 20 分别有两个 access
主机，两台 bridge 之间的 tagged trunk 同时承载两个 VLAN。四台主机故意位于同一个
`10.0.0.0/24` 子网，以便直接观察二层隔离。

## 拓扑图

```bash
nslab graph --format mermaid
```

```mermaid
flowchart LR
    n0["h10a\nlinux"]
    n1["sw1\nbridge"]
    n2["h20a\nlinux"]
    n3["sw2\nbridge"]
    n4["h10b\nlinux"]
    n5["h20b\nlinux"]
    n0 -- "eth0 <-> access10" --- n1
    n2 -- "eth0 <-> access20" --- n1
    n1 -- "trunk <-> trunk" --- n3
    n3 -- "access10 <-> eth0" --- n4
    n3 -- "access20 <-> eth0" --- n5
```

以下为典型输出；接口索引、MAC 地址和计数器会随运行变化。

## 运行

```console
$ sudo nslab deploy
deployed topology: bridge-vlan

$ sudo nslab inspect
status: deployed

NAME  KIND    STATUS    NAMESPACE
----  ------  --------  -----------------------------
h10a  linux   matching  nslab-bridge-vlan-h10a-...
sw1   bridge  matching  nslab-bridge-vlan-sw1-...
h20a  linux   matching  nslab-bridge-vlan-h20a-...
sw2   bridge  matching  nslab-bridge-vlan-sw2-...
h10b  linux   matching  nslab-bridge-vlan-h10b-...
h20b  linux   matching  nslab-bridge-vlan-h20b-...
```

## 查看端口 VLAN

```console
$ sudo nslab exec --node sw1 -- bridge vlan show
port      vlan-id
access10  10 PVID Egress Untagged
access20  20 PVID Egress Untagged
trunk     10
          20

$ sudo nslab exec --node sw2 -- bridge vlan show
port      vlan-id
trunk     10
          20
access10  10 PVID Egress Untagged
access20  20 PVID Egress Untagged
```

access 端口发送 untagged 帧；trunk 上 VLAN 10、20 保留 802.1Q tag。

## 验证隔离

同 VLAN 流量能够通过 trunk：

```console
$ sudo nslab exec --node h10a -- ping -c 3 10.0.0.2
64 bytes from 10.0.0.2: icmp_seq=1 ttl=64 time=<time> ms
...
3 packets transmitted, 3 received, 0% packet loss

$ sudo nslab exec --node h20a -- ping -c 3 10.0.0.4
64 bytes from 10.0.0.4: icmp_seq=1 ttl=64 time=<time> ms
...
3 packets transmitted, 3 received, 0% packet loss
```

跨 VLAN 的 ARP 广播不会越过边界，因此下面的命令按预期返回非零状态：

```console
$ sudo nslab exec --node h10a -- ping -c 2 -W 1 10.0.0.3
From 10.0.0.1 icmp_seq=1 Destination Host Unreachable
From 10.0.0.1 icmp_seq=2 Destination Host Unreachable
2 packets transmitted, 0 received, +2 errors, 100% packet loss
```

FDB 表项会携带对应的 VLAN：

```console
$ sudo nslab exec --node sw1 -- bridge fdb show br br0
<h10a-mac> dev access10 vlan 10 master br0
<h10b-mac> dev trunk vlan 10 master br0
<h20a-mac> dev access20 vlan 20 master br0
<h20b-mac> dev trunk vlan 20 master br0
...

$ sudo nslab exec --node sw2 -- bridge fdb show br br0
<h10a-mac> dev trunk vlan 10 master br0
<h10b-mac> dev access10 vlan 10 master br0
<h20a-mac> dev trunk vlan 20 master br0
<h20b-mac> dev access20 vlan 20 master br0
...
```

## 清理

```console
$ sudo nslab destroy
destroyed topology: bridge-vlan
```
