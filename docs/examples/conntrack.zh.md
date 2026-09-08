# 连接跟踪

```console
deployed topology: conntrack
status: deployed
destroyed topology: conntrack
status: absent
```

使用 `examples/conntrack/nslab.yaml`，在 r1 查看转发流量的 conntrack 状态。

```bash
sudo nslab graph --format mermaid
```

```mermaid
flowchart LR
    n0["h1\nlinux"]
    n1["r1\nlinux"]
    n2["h2\nlinux"]
    n0 -- "eth0 <-> eth0" --- n1
    n1 -- "eth1 <-> eth0" --- n2
```

```bash
sudo nslab deploy
deployed topology: conntrack
sudo nslab inspect
status: deployed
sudo nslab exec --node r1 -- sysctl -w net.ipv4.conf.all.rp_filter=0
sudo nslab exec --node r1 -- conntrack -F
sudo nslab exec --node h1 -- ping -n -c 1 10.73.2.2
sudo nslab exec --node r1 -- conntrack -L -o extended
sudo nslab exec --node h2 -- sh -c 'nc -u -l 9000 >/dev/null'
sudo nslab exec --node h1 -- sh -c 'printf hello | nc -u -w 1 10.73.2.2 9000'
sudo nslab exec --node r1 -- conntrack -L -p udp -o extended
sudo nslab exec --node h2 -- sh -c 'nc -l 9000 >/dev/null'
sudo nslab exec --node h1 -- sh -c 'printf hello | nc -w 2 10.73.2.2 9000'
sudo nslab exec --node r1 -- conntrack -L -p tcp -o extended
sudo nslab exec --node r1 -- conntrack -E
sudo nslab exec --node r1 -- conntrack -F
sudo nslab destroy
destroyed topology: conntrack
sudo nslab inspect
status: absent
```

ICMP 关注地址、协议、ICMP id 和 `ASSURED`；UDP 对比 `UNREPLIED` 与双向流量；TCP 在握手和关闭阶段查看 `SYN_SENT`、`SYN_RECV`、`ESTABLISHED`。需要事件流时运行 `conntrack -E`。
