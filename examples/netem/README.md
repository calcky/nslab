# Linux netem 实验

这个实验在 `h1` 与 `h2` 的 veth 两端都安装 netem egress qdisc，用于观察延迟、抖动
和随机丢包：

```text
h1 10.30.0.1/24  <-- delay 100ms, jitter 10ms, loss 5% -->  10.30.0.2/24 h2
```

link 级 `netem` 是双向配置。echo request 在 `h1` 端经历一次 egress delay，echo reply
在 `h2` 端再经历一次，因此 ping RTT 的中心值约为 200ms，而不是 100ms。

## 运行

```bash
nslab graph --detail
sudo nslab deploy
sudo nslab inspect
```

## 查看 qdisc

```bash
sudo nslab exec --node h1 -- tc -s qdisc show dev eth0
sudo nslab exec --node h2 -- tc -s qdisc show dev eth0
```

两端都应显示 `netem`，参数包含 `delay 100ms 10ms` 和 `loss 5%`。

## 产生流量

```bash
sudo nslab exec --node h1 -- ping -c 20 -i 0.2 10.30.0.2
sudo nslab exec --node h2 -- ping -c 20 -i 0.2 10.30.0.1
```

少量样本可能恰好没有丢包；增加 ping 次数后，统计丢包率会逐渐接近配置值。双端 jitter
也会共同影响 RTT 分布。

## 修改参数

编辑 `nslab.yaml` 中的 `delay_ms`、`jitter_ms` 或 `loss_percent`，然后重建拓扑：

```bash
sudo nslab redeploy
sudo nslab inspect
sudo nslab exec --node h1 -- ping -c 20 10.30.0.2
```

## 清理

```bash
sudo nslab destroy
```
