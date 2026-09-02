# Linux netem 链路条件

## 实验目标

在 veth 两端安装 netem egress qdisc，独立观察链路延迟、抖动、随机丢包和 qdisc
统计。这个示例属于链路条件实验，不依赖 IP 转发。

## 拓扑图

```console
$ nslab graph
Topology: netem

h1 [linux]
  eth0: 10.30.0.1/24
└─ eth0 ↔ eth0  h2 [linux]
                eth0: 10.30.0.2/24
```

该链路在两个方向分别配置 `100ms` delay、`10ms` jitter 和 `5%` loss。

## 运行

```bash
cd examples/netem
sudo nslab deploy
sudo nslab inspect
```

## 观察 qdisc

```bash
sudo nslab exec --node h1 -- tc -s qdisc show dev eth0
sudo nslab exec --node h2 -- tc -s qdisc show dev eth0
```

两端都应显示 netem 参数。echo request 在 `h1` 经历一次 egress delay，reply 在 `h2`
再经历一次，因此 RTT 中心值约为 `200ms`。

## 产生流量

```bash
sudo nslab exec --node h1 -- ping -c 20 -i 0.2 10.30.0.2
sudo nslab exec --node h2 -- ping -c 20 -i 0.2 10.30.0.1
```

少量样本可能恰好没有丢包。修改 `delay_ms`、`jitter_ms` 或 `loss_percent` 后可执行
`sudo nslab redeploy` 对比结果。

## 清理

```bash
sudo nslab destroy
```

[查看 nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/netem/nslab.yaml) ·
[查看示例 README](https://github.com/calcky/nslab/blob/main/examples/netem/README.md)
