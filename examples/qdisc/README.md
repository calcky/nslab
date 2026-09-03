# Linux qdisc 实验

这个实验把三条独立的点到点链路分别配置为 netem（带 `rate`）、TBF 和 fq_codel，便于
比较延迟/丢包、令牌桶整形以及公平队列。每个 qdisc 都会安装在链路两端的 egress。

## 拓扑图

```bash
nslab graph --format mermaid
```

```mermaid
flowchart LR
    n0["h1\nlinux"]
    n1["h2\nlinux"]
    n2["h3\nlinux"]
    n3["h4\nlinux"]
    n4["h5\nlinux"]
    n5["h6\nlinux"]
    n0 -- "eth0 <-> eth0" --- n1
    n2 -- "eth0 <-> eth0" --- n3
    n4 -- "eth0 <-> eth0" --- n5
```

## 运行

```console
$ sudo nslab deploy
deployed topology: qdisc

$ sudo nslab inspect
status: deployed

NAME  KIND   STATUS    NAMESPACE
----  -----  --------  -------------------------
h1    linux  matching  nslab-qdisc-h1-...
h2    linux  matching  nslab-qdisc-h2-...
h3    linux  matching  nslab-qdisc-h3-...
h4    linux  matching  nslab-qdisc-h4-...
h5    linux  matching  nslab-qdisc-h5-...
h6    linux  matching  nslab-qdisc-h6-...
```

## 观察 netem + rate

`h1`/`h2` 的链路同时有 10mbit 限速、20ms 延迟、5ms 抖动和 1% 随机丢包：

```console
$ sudo nslab exec --node h1 -- tc -s qdisc show dev eth0
qdisc netem ... root ... delay 20ms 5ms loss 1% rate 10Mbit
 Sent ... bytes ... pkt (dropped ..., overlimits ...)

$ sudo nslab exec --node h1 -- ping -c 5 10.60.1.2
PING 10.60.1.2 (10.60.1.2) 56(84) bytes of data.
64 bytes from 10.60.1.2: icmp_seq=1 ttl=64 time=<about-40> ms
...
5 packets transmitted, <received> received, <loss>% packet loss
```

## 观察 TBF

`h3`/`h4` 使用令牌桶整形：速率为 5mbit，burst 为 32kb，队列等待上限为 400ms。

```console
$ sudo nslab exec --node h3 -- tc -s qdisc show dev eth0
qdisc tbf ... root ... rate 5Mbit burst 32Kb latency 400ms
 Sent ... bytes ... pkt (dropped ..., overlimits ...)

$ sudo nslab exec --node h3 -- ping -c 5 10.60.2.2
PING 10.60.2.2 (10.60.2.2) 56(84) bytes of data.
64 bytes from 10.60.2.2: icmp_seq=1 ttl=64 time=<time> ms
...
5 packets transmitted, <received> received, 0% packet loss
```

## 观察 fq_codel

`h5`/`h6` 使用 fq_codel，目标队列延迟为 5ms，检测间隔为 100ms，队列上限为 10240 个
报文，并启用 ECN：

```console
$ sudo nslab exec --node h5 -- tc -s qdisc show dev eth0
qdisc fq_codel ... root ... limit 10240p ... target 5ms interval 100ms ... ecn
 Sent ... bytes ... pkt (dropped ..., overlimits ...)

$ sudo nslab exec --node h5 -- ping -c 5 10.60.3.2
PING 10.60.3.2 (10.60.3.2) 56(84) bytes of data.
64 bytes from 10.60.3.2: icmp_seq=1 ttl=64 time=<time> ms
...
5 packets transmitted, 5 received, 0% packet loss
```

每条链路的 qdisc 都会同时出现在两端；可将 `h1`/`h3`/`h5` 换成对应的对端节点观察
另一方向。`netem` 与 `qdisc` 是互斥字段，同一条链路只能选择其中一种。

## 清理

```console
$ sudo nslab destroy
destroyed topology: qdisc
```

[查看 nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/qdisc/nslab.yaml) ·
[查看示例 README](https://github.com/calcky/nslab/blob/main/examples/qdisc/README.md)
