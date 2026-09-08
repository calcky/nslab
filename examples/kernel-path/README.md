# 内核数据路径观测

观察正常 IPv4 转发、本机 INPUT/OUTPUT，也能从“ping 不通”定位具体报文和内核丢包原因。所有配置只作用于实验 namespace，不新增 nslab 功能，也不修改宿主机 sysctl。原 drop-diagnosis 已合并至本目录；旧部署可先用 `nslab destroy --name drop-diagnosis` 清理。

在此目录执行，依赖 `nslab`、`python3`、`iproute2`、`iputils-ping`、`tcpdump` 和 `bpftrace`。推荐 Ubuntu 24.04 或更新的 Linux，需要 BTF、tracepoint/kprobe 和 root/BPF 权限。仅 drop 模式额外要求 `skb:kfree_skb` 的 `reason` 字段（上游 Linux 5.17+）；实际可用探针与字段以运行内核为准。tracefs 必须已挂载，脚本不会自动挂载或安装依赖。以下输出为示意。

## 拓扑与基线

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

h1 为 `10.73.1.1`，h2 为 `10.73.2.2`，r1 两端为各网段的 `.254`。manifest 固定 MAC 和永久邻居项，排除 ARP 解析对故障结果的干扰；ARP/邻居状态实验参见 neighbors 示例。部署后显式关闭 r1 的反向路径过滤作为基线，不依赖发行版默认值。

```console
$ sudo nslab deploy
deployed topology: kernel-path
$ sudo nslab inspect
status: deployed
...
$ sudo nslab exec --node r1 -- sysctl -w net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0 net.ipv4.conf.eth0.rp_filter=0 net.ipv4.conf.eth1.rp_filter=0
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0
net.ipv4.conf.eth0.rp_filter = 0
net.ipv4.conf.eth1.rp_filter = 0
$ sudo nslab exec --node h1 -- ping -n -c 3 -W 1 10.73.2.2
...
3 packets transmitted, 3 received, 0% packet loss
```

## 正常数据路径：转发与本机收发

先保持上述正常基线，不注入故障。在终端 A 启动 path 模式，等待 ready；在终端 B 依次发送下面三组 ping。跟踪在 30 秒后自动停止；若需要更多操作时间可设 --seconds 60。--stacks 可选，未开启时不收集调用栈。

```console
$ sudo python3 ./trace.py --mode path --seconds 30 --stacks | tee /tmp/kernel-path.log
probes={"enabled": [...], "unavailable": [...]}
ready netns=<inode>
path ts=<ns> cpu=<cpu> ns=<inode> ifindex=<index> ifname=eth0 skb=0x... src=10.73.1.1 dst=10.73.2.2 type=8 id=<id> seq=1 stage=forward hook=kprobe:ip_forward
    ip_forward+...
    ...
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

| 流量 | r1 上重点观察 | 区别 |
| --- | --- | --- |
| h1 → h2，请求和回复 | `ip_rcv`、`ip_forward`、`ip_output`、发送队列 | 经过 r1 转发 |
| h1 → r1 | 请求：`ip_local_deliver`；回复：输出与发送队列 | 请求交给本机，回复由本机生成 |
| r1 → h2 | 请求：输出与发送队列；回复：`ip_local_deliver` | 本机主动发送，再接收回复 |

结束后按报文查看，不需要从混在一起的日志中手工找同一包：

```console
$ python3 ./paths.py /tmp/kernel-path.log
10.73.1.1 -> 10.73.2.2 ICMP request id=<id> seq=1 netns=<inode>
  +    0.000 us  tracepoint:net:netif_receive_skb     dev=eth0(...) cpu=... skb=0x...
  +   ...       kprobe:ip_rcv                        dev=eth0(...) cpu=... skb=0x...
  +   ...       kprobe:ip_forward                    dev=eth0(...) cpu=... skb=0x...
  +   ...       kprobe:ip_output                     dev=eth0(...) cpu=... skb=0x...
  +   ...       tracepoint:net:net_dev_queue          dev=eth1(...) cpu=... skb=0x...
...
$ python3 ./paths.py /tmp/kernel-path.log --stacks
10.73.1.1 -> 10.73.2.2 ICMP request id=<id> seq=1 netns=<inode>
  ...
  +   ...       kprobe:ip_forward ...
      ip_forward+...
      ...
$ python3 ./paths.py /tmp/kernel-path.log --json
[
  [
    {"ts": ..., "namespace": ..., "hook": "...", "stack": [...]}
  ],
  ...
]
```

这是**观测点序列，不是完整内核调用图**。每组使用 namespace、源/目的 IP、ICMP type/id/seq 关联，按内核时间戳排序，显示相对首个观测点的时间。request/reply 分组分开，但保留相同 id/seq 便于对照。skb 地址变化不会拆开同一个报文组；skb clone 也可能产生多个分支，列表不是证明它们都属于唯一线性路径。一次采集内避免重复使用完全相同的报文字段。

时间用于观察这次事件的相对顺序，不是无探针开销的延迟基准，也不能直接和 pcap 的墙上时钟相减。调用栈可能包含 softirq 上下文中的上游发送路径；它是事件发生时的栈，不是该报文完整的历史。

`ip_local_out` / `__ip_local_out` 在支持时挂探针，但编译器可能将实际 ICMP 调用点内联：**符号存在、探针挂载成功，不等于每条路径都会触发它**。自动检查将这种情况放入 `enabled_but_unseen`，不补出虚假的事件。本机 OUTPUT 检查要求观察到 `ip_output` 和发送队列，并结合从 r1 发包的命令及端点抓包；不声称单独验证了 Netfilter LOCAL_OUT hook。

`dev=- (ifindex=0)` 表示该时刻 skb 没有关联设备；本机输出探针使用函数的 net 参数过滤 namespace。其它时刻的 dev 是 skb 当前关联接口，未必已是最终出口；发送队列事件能补充出口信息。入包批处理、函数内联、探针缺失或日志丢事件都可能造成空白，不能把“未观测到”解释为“没有经过”。

### 一次性正常路径检查

```console
$ sudo python3 ./check_paths.py
results: /tmp/nslab-path-results-<random>
forward: observed
input: observed
output: observed
$ sudo python3 ./check_paths.py --scenario input --output /tmp/kernel-input-run1
results: /tmp/kernel-input-run1
input: observed
$ sudo python3 ./paths.py /tmp/kernel-input-run1/input/paths.log --stacks
10.73.1.1 -> 10.73.1.254 ICMP request id=20000 seq=1 netns=<inode>
  ...
  +   ...       kprobe:ip_local_deliver ...
      ip_local_deliver+...
      ...
10.73.1.254 -> 10.73.1.1 ICMP reply id=20000 seq=1 netns=<inode>
  ...
  +   ...       kprobe:ip_output ...
      ...
```

check_paths.py 复用丢包检查的受控进程和部署清理逻辑，但不注入故障。每个场景同时验证 3 个请求和 3 个回复、端点 pcap、r1 namespace 过滤、必要观测点顺序和调用栈；本机收发不应出现匹配的 ip_forward 事件。结果还包括 `paths.log`、`paths.txt`、`paths-stacks.txt` 和结构化 events。

本机收发和转发要求的观测点不同。缺少必要证据标记 `inconclusive`，依赖不可用标记 `skipped`，不把没观测到的节点补进输出。`probes.unavailable` 记录不可用探针，`enabled_but_unseen` 记录本次未产生匹配事件的已启用探针。无事件不是全路径无流量的证明。

`NSLAB_BIN` / `BPFTRACE_BIN`、新输出目录要求、退出码及中断清理与下方 check.py 相同。特权检查不加入 CI；没有安装或修改宿主机工具与内核设置。

## 丢包定位：同时观察三个证据来源

分别在终端 A 运行跟踪器、终端 B/C 抓包，终端 D 注入后面的故障并发包。每次实验重新启动观测，等到 `ready` / `listening on` 后再发包。正常基线不会出现匹配的 drop 事件；下面 drop 行是随后注入故障时的示意。抓包先于常规 IP 输入和 TC ingress，所以三个故障都可能呈现“r1 看得到，h2 看不到”。

```console
$ sudo python3 ./trace.py --mode drop --name kernel-path --node r1 --seconds 60
reason_names={"2": "NOT_SPECIFIED", ...}
ready netns=<inode>
...
drop ns=<inode> ifindex=<index> id=<id> seq=1 reason=<code> location=<kernel-function>
$ sudo nslab exec --node r1 -- env -u LD_LIBRARY_PATH tcpdump -nni eth0 'icmp and src host 10.73.1.1 and dst host 10.73.2.2'
... 10.73.1.1 > 10.73.2.2: ICMP echo request, id <id>, seq 1 ...
$ sudo nslab exec --node h2 -- env -u LD_LIBRARY_PATH tcpdump -nni eth0 'icmp and src host 10.73.1.1 and dst host 10.73.2.2'
... 10.73.1.1 > 10.73.2.2: ICMP echo request, id <id>, seq 1 ...
$ sudo nslab exec --node r1 -- nstat -aszj
{"kernel": {...}}
```

`nstat -aszj` 读取当前 namespace 的绝对计数，不更新历史文件；故障前后各读一次再相减，不能把累计值当成本次丢包数。`env -u LD_LIBRARY_PATH` 避免打包版 nslab 自带的动态库影响系统 tcpdump。

**跟踪器在宿主机执行，不要包在 nslab exec 中。** 内核 tracepoint 本身不是按 namespace 隔离的；进入 namespace 不会自动限制事件，且可能看不到宿主机 tracefs。脚本通过 inspect 找到目标 namespace inode，在 BPF 内检查 `skb->dev->nd_net.net->ns.inum`，再过滤固定源/目的 IPv4 和 ICMP Echo Request。不是按当前 PID 过滤，因为收包可能发生于 softirq/ksoftirqd。

输出保留 ICMP `id/seq`、`ifindex`、数字 `reason` 和释放位置 `location`。数字按开头的 `reason_names` 表解释，该表从运行内核的 trace format 读取，不硬编码跨版本编号。一次性检查会额外生成可直接阅读的 `reason_name` 字段。

## 故障一：目的路由缺失

删除 r1 到 h2 网段的路由。期望 r1 入接口看到请求、h2 看不到，并出现 `IP_INNOROUTES` 或对应版本的 `IP_OUTNOROUTES`；结合 `IpExtInNoRoutes` / `IpOutNoRoutes` 增量。ICMP 错误回复可能被限速，不要求每个请求都有错误回复。这里 `ip route get` 只是本地选路检查，不是完整的转发路径模拟。

```console
$ sudo nslab exec --node r1 -- ip route del 10.73.2.0/24 dev eth1
(no output)
$ sudo nslab exec --node r1 -- ip route get 10.73.2.2
RTNETLINK answers: Network is unreachable
$ sudo nslab exec --node h1 -- ping -n -c 3 -W 1 10.73.2.2
From 10.73.1.254 icmp_seq=1 Destination Net Unreachable
...
3 packets transmitted, 0 received, ... 100% packet loss
$ sudo nslab exec --node r1 -- ip route add 10.73.2.0/24 dev eth1
(no output)
$ sudo nslab exec --node h1 -- ping -n -c 3 -W 1 10.73.2.2
...
3 packets transmitted, 3 received, 0% packet loss
```

## 故障二：严格反向路径过滤

在 r1:eth0 启用 strict `rp_filter=1`，再放一条更具体的错误反向路由。请求从 eth0 进入，但查到源地址的最佳出口是 eth1，因此校验失败。证据是 `IP_RPFILTER` 事件与 `TcpExtIPReversePathFilter` 增量，而不仅是“没有回包”。

```console
$ sudo nslab exec --node r1 -- sysctl -w net.ipv4.conf.eth0.rp_filter=1
net.ipv4.conf.eth0.rp_filter = 1
$ sudo nslab exec --node r1 -- ip route add 10.73.1.1/32 via 10.73.2.2 dev eth1
(no output)
$ sudo nslab exec --node r1 -- ip route get 10.73.1.1
10.73.1.1 via 10.73.2.2 dev eth1 src 10.73.2.254 ...
$ sudo nslab exec --node h1 -- ping -n -c 3 -W 1 10.73.2.2
...
3 packets transmitted, 0 received, 100% packet loss
$ sudo nslab exec --node r1 -- sysctl -w net.ipv4.conf.eth0.rp_filter=0
net.ipv4.conf.eth0.rp_filter = 0
$ sudo nslab exec --node r1 -- ip route del 10.73.1.1/32 via 10.73.2.2 dev eth1
(no output)
$ sudo nslab exec --node h1 -- ping -n -c 3 -W 1 10.73.2.2
...
3 packets transmitted, 3 received, 0% packet loss
```

有效 rp_filter 取 `conf.all` 和入口接口配置的最大值；基线已将 all 设为 0。恢复需要同时撤销错误路由，不能只关闭过滤，否则回包仍会走错路径。永久邻居项确保本实验不是先卡在 ARP。

## 故障三：TC ingress 丢弃

使用 flower 匹配同一实验流，gact 执行 drop。在 IP 路由输入之前丢弃时，r1 抓包仍可见，而 IP 层计数未必增加。把 TC action 的 drops 增量与 `TC_INGRESS` 事件对应起来；部分旧内核只报告 `NOT_SPECIFIED`，不能强行解释为精确原因。

```console
$ sudo nslab exec --node r1 -- tc qdisc add dev eth0 clsact
(no output)
$ sudo nslab exec --node r1 -- tc filter add dev eth0 ingress protocol ip pref 10 flower src_ip 10.73.1.1 dst_ip 10.73.2.2 ip_proto icmp action drop
(no output)
$ sudo nslab exec --node h1 -- ping -n -c 3 -W 1 10.73.2.2
...
3 packets transmitted, 0 received, 100% packet loss
$ sudo nslab exec --node r1 -- tc -s filter show dev eth0 ingress
filter ... flower ...
action order 1: gact action drop
... Sent ... bytes 3 pkt (dropped 3, overlimits 0 requeues 0)
$ sudo nslab exec --node r1 -- tc qdisc del dev eth0 clsact
(no output)
$ sudo nslab exec --node h1 -- ping -n -c 3 -W 1 10.73.2.2
...
3 packets transmitted, 3 received, 0% packet loss
```

| 场景 | r1 eth0 请求 | h2 请求 | 关键证据 |
| --- | --- | --- | --- |
| 正常基线 | 3 | 3 | ping 成功，无匹配的 drop |
| no-route | 3 | 0 | `IP_INNOROUTES` / `IP_OUTNOROUTES`、选路失败 |
| rp-filter | 3 | 0 | `IP_RPFILTER`、反向路由出口错误 |
| tc-ingress | 3 | 0 | `TC_INGRESS`、TC action dropped 增加 |

## 一次性检查与结果

`check.py` 创建独立随机名称的临时拓扑，不复用手动部署。先检查连通性，然后逐个故障采集跟踪、r1/h2 pcap、nstat 和路由/TC 状态；每次撤销故障后再次 ping，最后 destroy 并检查 absent。它不会下载工具，不在 CI 执行特权实验。

```console
$ sudo python3 ./check.py
results: /tmp/nslab-drop-results-<random>
baseline: observed
no-route: observed
rp-filter: observed
tc-ingress: observed
$ sudo python3 ./check.py --scenario rp-filter --output /tmp/drop-rpf-run1
results: /tmp/drop-rpf-run1
rp-filter: observed
$ sudo python3 -m json.tool /tmp/drop-rpf-run1/rp-filter/result.json
{
    "scenario": "rp-filter",
    ...
    "events": [
        {
            ...
            "reason_name": "IP_RPFILTER",
            "location": "<kernel-function>"
        }
    ],
    ...
}
```

`--output` 必须是不存在的新目录。程序或 bpftrace 不在 sudo 的 PATH 中时，使用 `sudo env NSLAB_BIN=/absolute/path/nslab BPFTRACE_BIN=/absolute/path/bpftrace python3 ./check.py`。

检查还会同时启动过滤 h2 namespace 的对照跟踪器：r1 丢包时它不应收到相同流的 drop 事件，用于验证没有混入其它 namespace 的流量，日志保存在 `other-netns.log`。若前面的命令失败导致停止，未执行场景在汇总中标为 `not-run`。

结果保留在打印的目录：`summary.json` 包含环境、版本、源码 SHA256、完整命令/退出码/输出和最终清理检查；各场景有 `result.json`、`drops.log`、`r1.pcap`、`h2.pcap` 和解码文本。原始 `drops.log` 保留运行内核的 reason 表。

`observed` 要求发包结果、抓包位置、三个 ICMP 序号的 namespace/id/reason 对应和恢复验证都满足。基线要求交付成功且无匹配 drop。证据不完整或旧内核原因过于笼统标 `inconclusive`；缺少跟踪依赖标 `skipped`（独立 trace.py 退出 77）；命令失败标 `error` 并停止后续场景，以免叠加未撤销的故障。结果不完整时 check.py 退出 1，不伪装为成功。

Ctrl+C/SIGTERM 会停止子进程、卸载跟踪并销毁临时拓扑；部署事务期间延后处理中断，完成后立即清理。SIGKILL/掉电无法保证清理，可根据 summary.json 的 deployment 手动 destroy。跟踪程序不 pin BPF 对象，不修改 tracefs 全局 enable/filter，不清空其它人的 trace buffer。

## 观测边界

- `kfree_skb` 是被丢弃 skb 的释放事件，不是所有包的路径追踪，也不能证明“没有事件就没有丢包”。设备为 NULL、头部不可读、非线性头部或不符合本例 IPv4/ICMP 格式的 skb 不纳入统计。
- 这里读取线性、无 IP options、未分片的 IPv4 Echo Request，不支持任意 TCP/UDP/IPv6 流；限制是有意的，用来保证每条证据能对应实验报文。
- XDP、驱动、硬件和其它路径可能不经过此事件，或不携带可用 reason。XDP_DROP 要结合 XDP 程序计数，不能靠这个跟踪器判定。
- `location` 是释放调用位置，不一定是故障最初发生的位置；内核可能内联函数，符号名/偏移和 reason 编号也会变化。不要以某个固定函数名作为唯一判据。
- 跟踪有开销和丢事件风险，本例是低速学习实验，不是生产全流量监控。需要跨 hook 关联 skb 时，可进一步使用 [retis](https://github.com/retis-org/retis)，仍需按 namespace 和报文过滤。

## 清理

先停止手动跟踪和抓包，再销毁。trace.py 达到 --seconds 或 Ctrl+C 后退出，BPF 链接随进程释放；自动检查的临时拓扑已自行清理。

```console
$ sudo nslab destroy
destroyed topology: kernel-path
$ sudo nslab inspect
status: absent
...
```
