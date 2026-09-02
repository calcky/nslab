# Linux netem 实验

这个实验在 `h1` 与 `h2` 的 veth 两端都安装 netem egress qdisc，用于观察延迟、抖动
和随机丢包。echo request 和 reply 各经历一次 egress delay，所以 RTT 中心值约为
`200ms`。

## 拓扑图

```bash
nslab graph --format mermaid
```

```mermaid
flowchart LR
    n0["h1\nlinux"]
    n1["h2\nlinux"]
    n0 -- "eth0 <-> eth0" --- n1
```

该链路双向配置 `100ms` delay、`10ms` jitter 和 `5%` loss。以下统计输出会随随机
丢包和流量变化。

## 运行

```console
$ sudo nslab deploy
deployed topology: netem

$ sudo nslab inspect
status: deployed

NAME  KIND   STATUS    NAMESPACE
----  -----  --------  ------------------------
h1    linux  matching  nslab-netem-h1-...
h2    linux  matching  nslab-netem-h2-...
```

## 查看 qdisc

两端都应显示相同的 netem 参数：

```console
$ sudo nslab exec --node h1 -- tc -s qdisc show dev eth0
qdisc netem ... root ... limit 1000 delay 100ms 10ms loss 5%
 Sent ... bytes ... pkt (dropped ..., overlimits ... requeues 0)

$ sudo nslab exec --node h2 -- tc -s qdisc show dev eth0
qdisc netem ... root ... limit 1000 delay 100ms 10ms loss 5%
 Sent ... bytes ... pkt (dropped ..., overlimits ... requeues 0)
```

## 产生流量

```console
$ sudo nslab exec --node h1 -- ping -c 20 -i 0.2 10.30.0.2
PING 10.30.0.2 (10.30.0.2) 56(84) bytes of data.
64 bytes from 10.30.0.2: icmp_seq=1 ttl=64 time=<about-200> ms
...
20 packets transmitted, <received> received, <loss>% packet loss

$ sudo nslab exec --node h2 -- ping -c 20 -i 0.2 10.30.0.1
PING 10.30.0.1 (10.30.0.1) 56(84) bytes of data.
64 bytes from 10.30.0.1: icmp_seq=1 ttl=64 time=<about-200> ms
...
20 packets transmitted, <received> received, <loss>% packet loss
```

少量样本可能恰好没有丢包；增加样本后，统计丢包率会逐渐接近配置值。

## 修改参数

编辑 `nslab.yaml` 中的 `delay_ms`、`jitter_ms` 或 `loss_percent`，然后重建拓扑：

```console
$ sudo nslab redeploy
redeployed topology: netem

$ sudo nslab inspect
status: deployed
...

$ sudo nslab exec --node h1 -- ping -c 20 10.30.0.2
64 bytes from 10.30.0.2: icmp_seq=1 ttl=64 time=<new-delay> ms
...
20 packets transmitted, <received> received, <loss>% packet loss
```

## 清理

```console
$ sudo nslab destroy
destroyed topology: netem
```
