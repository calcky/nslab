# 连接跟踪

```console
deployed topology: conntrack
status: deployed
destroyed topology: conntrack
status: absent
```

观察 Linux netfilter conntrack 为转发流量建立、更新和删除状态。需要 root、`conntrack`、`iproute2`、`iputils-ping`、`netcat-openbsd`。

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
```

ICMP 没有端口，关注协议、地址、ICMP id 和 `ASSURED`。UDP 单向发送通常显示 `UNREPLIED`，双向流量后出现 reply tuple：

```bash
sudo nslab exec --node h2 -- sh -c 'nc -u -l 9000 >/dev/null'
sudo nslab exec --node h1 -- sh -c 'printf hello | nc -u -w 1 10.73.2.2 9000'
sudo nslab exec --node r1 -- conntrack -L -p udp -o extended
```

TCP 在握手、传输和关闭阶段重复查询，观察 `SYN_SENT`、`SYN_RECV`、`ESTABLISHED` 和超时：

```bash
sudo nslab exec --node h2 -- sh -c 'nc -l 9000 >/dev/null'
sudo nslab exec --node h1 -- sh -c 'printf hello | nc -w 2 10.73.2.2 9000'
sudo nslab exec --node r1 -- conntrack -L -p tcp -o extended
sudo nslab exec --node r1 -- conntrack -E
```

`conntrack -L` 是快照，`conntrack -E` 才是事件流。完成后清理：

```bash
sudo nslab exec --node r1 -- conntrack -F
sudo nslab destroy
destroyed topology: conntrack
sudo nslab inspect
status: absent
```

工具缺失、模块未加载、流量未经过 netfilter hook 或记录已经超时，都可能导致查询为空。
