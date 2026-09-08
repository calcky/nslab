# 内核数据路径观测

手动部署、运行 bt、发包、看输出、清理，不使用 Python 自动化脚本。依赖 `nslab bpftrace jq iproute2 iputils-ping`，可选 `tcpdump`；需要 root、内核 BTF、tracefs 和对应 tracepoint/kprobe。drop 模式还需 kfree_skb 的 reason 字段（上游 Linux 5.17+）。输出为示意，推荐 Ubuntu 24.04 或更新的 Linux。

## 1. 部署

在本目录执行。拓扑固定 MAC 和永久邻居项，避免 ARP 干扰。

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

```console
$ sudo nslab deploy
deployed topology: kernel-path
$ sudo nslab inspect
status: deployed
...
$ sudo nslab exec --node r1 -- sysctl -w net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.eth0.rp_filter=0 net.ipv4.conf.eth1.rp_filter=0
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.eth0.rp_filter = 0
net.ipv4.conf.eth1.rp_filter = 0
```

```bash
lab_ns=$(sudo nslab inspect --format json | jq -er '.nodes[] | select(.name == "r1") | .namespace')
lab_inode=$(sudo stat -Lc '%i' "/run/netns/$lab_ns")
```

变量在启动 bpftrace 的终端设置。重新 deploy 后要重新获取 inode。**bpftrace 在宿主机运行**，不套 nslab exec；bt 内按 skb 的 namespace 过滤，不能用当前进程 PID 代替。旧部署可用 `nslab destroy --name drop-diagnosis` 清理。

## 2. 正常路径

终端 A 启动跟踪，等 ready；终端 B 依次运行三条 ping。第二个参数设为 `1` 可显示调用栈，`0` 为简洁输出。Ctrl+C 停止；可用 `| tee /tmp/kernel-path.log` 保存日志。

```console
$ sudo bpftrace paths.bt "$lab_inode" 0
...
ready netns=<inode> stacks=0
ts=... cpu=... ns=... dev=eth0(...) skb=0x... 10.73.1.1 -> 10.73.2.2 type=8 id=... seq=1 kprobe:ip_forward
...
```

```console
$ sudo nslab exec --node h1 -- ping -n -c 3 -W 1 10.73.2.2
...
3 packets transmitted, 3 received, 0% packet loss
$ sudo nslab exec --node h1 -- ping -n -c 3 -W 1 10.73.1.254
...
3 packets transmitted, 3 received, 0% packet loss
$ sudo nslab exec --node r1 -- ping -n -c 3 -W 1 10.73.2.2
...
3 packets transmitted, 3 received, 0% packet loss
```

| 流量 | 在 r1 上看什么 |
| --- | --- |
| h1 → h2 | ip_rcv → ip_forward → ip_output → net_dev_queue |
| h1 → r1 | 请求到 ip_local_deliver；回复经过 __ip_local_out、ip_output |
| r1 → h2 | 请求经过 __ip_local_out、ip_output；回复到 ip_local_deliver |

按源/目的 IP、type（8 请求、0 回复）、id/seq 和时间戳对应同一报文；skb 地址仅作辅助。输出是选定观测点，不是完整调用链。dev 表示当时 skb 关联接口，不一定是最终出口；本机输出可能显示 `-(0)`，此时用函数 net 参数过滤 namespace。内核内联/批处理可能绕过某些探针，未观测到不代表没有经过。时间含探针开销，不是性能基准。

## 3. 丢包定位

停止 paths.bt，改运行 drops.bt。故障在另一终端逐个注入、发包、撤销，不叠加；ping 非零是预期结果，不要用 set -e 串行执行。

```console
$ sudo bpftrace drops.bt "$lab_inode"
...
ready netns=<inode>
drop ns=... ifindex=... id=... seq=1 reason=<number> location=<function>
```

```bash
sudo cat /sys/kernel/tracing/events/skb/kfree_skb/format
```

数字 reason 按运行内核 format 中的符号表解释，不能跨版本硬编码。预期分别是 `IP_INNOROUTES/IP_OUTNOROUTES`、`IP_RPFILTER`、`TC_INGRESS`；旧内核可能只有 `NOT_SPECIFIED`。下列顺序为路由缺失、严格反向路径过滤、TC ingress drop：

```console
$ sudo nslab exec --node r1 -- ip route del 10.73.2.0/24 dev eth1
(no output)
$ sudo nslab exec --node h1 -- ping -n -c 3 -W 1 10.73.2.2
From 10.73.1.254 icmp_seq=1 Destination Net Unreachable
...
$ sudo nslab exec --node r1 -- ip route add 10.73.2.0/24 dev eth1
(no output)
$ sudo nslab exec --node r1 -- sysctl -w net.ipv4.conf.eth0.rp_filter=1
net.ipv4.conf.eth0.rp_filter = 1
$ sudo nslab exec --node r1 -- ip route add 10.73.1.1/32 via 10.73.2.2 dev eth1
(no output)
$ sudo nslab exec --node h1 -- ping -n -c 3 -W 1 10.73.2.2
...
3 packets transmitted, 0 received, 100% packet loss
$ sudo nslab exec --node r1 -- sysctl -w net.ipv4.conf.eth0.rp_filter=0
net.ipv4.conf.eth0.rp_filter = 0
$ sudo nslab exec --node r1 -- ip route del 10.73.1.1/32 via 10.73.2.2 dev eth1
(no output)
$ sudo nslab exec --node r1 -- tc qdisc add dev eth0 clsact
(no output)
$ sudo nslab exec --node r1 -- tc filter add dev eth0 ingress protocol ip pref 10 flower src_ip 10.73.1.1 dst_ip 10.73.2.2 ip_proto icmp action drop
(no output)
$ sudo nslab exec --node h1 -- ping -n -c 3 -W 1 10.73.2.2
...
3 packets transmitted, 0 received, 100% packet loss
$ sudo nslab exec --node r1 -- tc -s filter show dev eth0 ingress
... gact action drop ... dropped 3 ...
$ sudo nslab exec --node r1 -- tc qdisc del dev eth0 clsact
(no output)
$ sudo nslab exec --node h1 -- ping -n -c 3 -W 1 10.73.2.2
...
3 packets transmitted, 3 received, 0% packet loss
```

可同时在 r1 eth0 和 h2 eth0 用 `tcpdump -nni eth0 icmp` 抓包，并比较故障前后的 `nstat -aszj`。三种故障通常都是 r1 看得到请求、h2 看不到；需要 reason、路由和 TC 计数区分。rp_filter 恢复必须同时撤销错误回程路由。

## 4. 收包与 CPU 调度

`context` 简要显示 `NET_RX`、`NET_TX`、`ksoftirqd` 或 `task/unknown`。保留 `comm` 与 `softirq`，因此 `context=ksoftirqd softirq=NET_RX` 能同时表达执行载体和工作类型。`task/unknown` 只表示未识别到这两种网络 softirq 或 ksoftirqd，不能证明是进程上下文；其他 softirq、硬 IRQ、threaded NAPI 以及挂载探针前已开始的 softirq 不由这个轻量脚本完整分类。

`receive.bt` 将 r1 的实验 ICMP 在 `netif_receive_skb`、`ip_rcv` 按 CPU 计数，并记录 `pid/comm`。`NET_RX` softirq 和 `ksoftirqd` 调度是宿主机全局信号，只能作为背景参考。

```console
$ sudo bpftrace receive.bt "$lab_inode"
ready netns=<inode>
rx ts=... cpu=2 pid=... comm=ping context=task/unknown softirq=none dev=eth0 skb=0x... type=8 id=... seq=1
ip ts=... cpu=2 pid=... comm=ping context=task/unknown softirq=none dev=eth0 skb=0x... type=8 id=... seq=1
```

另一个终端记录基线并发包，之后 Ctrl+C 查看各 CPU 和执行上下文汇总：

```bash
sudo ip netns exec "$lab_ns" cat /sys/class/net/eth0/queues/rx-0/rps_cpus
sudo cat /proc/softirqs
sudo cat /proc/net/softnet_stat
sudo nslab exec --node h1 -- ping -n -f -c 10000 10.73.2.2
sudo cat /proc/softirqs
sudo cat /proc/net/softnet_stat
```

接收可能在发送 veth 的进程上下文（`softirq=none`）、`NET_RX` softirq，或 `ksoftirqd/N + NET_RX` 中运行。veth 通常直接在发送 CPU 上触发对端接收，不经过物理网卡硬中断。softirq 也可能借普通进程或 `swapper/N` 的当前 task 运行；只有 comm 是 `ksoftirqd/N` 才表示推迟给内核线程。PID 不能用于判断报文属于哪个应用。

选择在线 CPU（下面以 CPU 2、十六进制掩码 `4` 为例），启用 RPS 后重新跟踪和发包，再恢复：

```bash
grep '^processor' /proc/cpuinfo
sudo ip netns exec "$lab_ns" sh -c 'echo 4 > /sys/class/net/eth0/queues/rx-0/rps_cpus'
sudo nslab exec --node h1 -- ping -n -f -c 10000 10.73.2.2
sudo ip netns exec "$lab_ns" sh -c 'echo 0 > /sys/class/net/eth0/queues/rx-0/rps_cpus'
```

掩码 `4` 选择 CPU 2，`3` 选择 CPU 0、1。单流可能稳定落在一个 CPU；可用多个并发流观察分布。`/proc/softirqs` 的 `NET_RX` 行和 `/proc/net/softnet_stat` 都是宿主机累计值，实验前后作差；softnet 每行对应一个 CPU，前三列依次关注 processed、dropped、time_squeeze。veth/RPS 不能说明物理 NIC 的 IRQ affinity、RSS 队列或硬件 steering。

## 5. 发包与 CPU 调度

`transmit.bt` 对应观察 r1 的 `ip_output`、`net_dev_queue` 和 `net_dev_start_xmit`，分别代表 IPv4 输出、进入设备发送路径和调用驱动发送前。终端 A 启动跟踪，终端 B 从 h1 发包：

```console
$ sudo bpftrace transmit.bt "$lab_inode"
ready netns=<inode>
ipout ts=... cpu=2 pid=... comm=ping context=task/unknown softirq=none dev=eth0 skb=0x... type=8 id=... seq=1
queue ts=... cpu=2 pid=... comm=ping context=task/unknown softirq=none dev=eth1 skb=0x... len=98 type=8 id=... seq=1
xmit ts=... cpu=2 pid=... comm=ping context=task/unknown softirq=none dev=eth1 skb=0x... q=0 len=98 gso=0/0 type=8 id=... seq=1
```

```bash
sudo nslab exec --node h1 -- ping -n -c 3 -W 1 10.73.2.2
sudo nslab exec --node r1 -- tc -s -d qdisc show dev eth1
```

发送可能在系统调用的进程上下文（`none`）、转发所在的 `NET_RX`、延迟发送所在的 `NET_TX`，或相应的 `ksoftirqd/N` 中运行。用 type/id/seq 和 skb 地址关联同一个报文，再比较时间戳、CPU、comm 和 softirq。转发报文在 `ip_output` 处的 dev 仍可能是入口接口，设备队列处才显示实际出口。`q` 是 TX queue mapping；小 ICMP 的 `gso=size/segs` 通常为 `0/0`。

`net_dev_start_xmit` 是调用虚拟驱动前的边界，不表示物理线上已经发送。脚本不解引用驱动返回后的 `net_dev_xmit` skb，因为成功发送后 skb 可能已经释放；发送结果和错误结合接口计数、`tc -s` 及 drop trace 判断。

## 6. 清理与限制

先 Ctrl+C 停止所有 bpftrace/抓包，再 destroy。bt 不 pin 对象、不修改 tracefs 全局配置；进程退出释放探针。**不会自动撤销故障或销毁拓扑**，中途退出也要执行下面的清理。

```console
$ sudo nslab destroy
destroyed topology: kernel-path
$ sudo nslab inspect
status: absent
...
```

探针不可用时 bpftrace 会报错；可用 `sudo bpftrace -l 'kprobe:ip_*'` 和 `sudo bpftrace -l 'tracepoint:net:*'` 检查，按内核实际支持移除不可用挂载点，不要忽略报错继续解释空日志。

仅观察实验网段的线性、无 IP options、未分片 IPv4 ICMP，drops.bt 只看 h1 → h2 请求。NULL 设备、不可读头部、日志丢事件都可能导致遗漏；XDP/驱动/硬件丢包也不一定经过 kfree_skb。location 是释放位置，调用栈是当前上下文，不是报文完整历史。需要更完整的跨 hook 关联可使用 [retis](https://github.com/retis-org/retis)。
