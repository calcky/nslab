# Linux IPv6 转发

## 实验目标

`r1` 连接两个 `/64` 子网并开启 `net.ipv6.conf.all.forwarding=1`。两台主机使用显式
IPv6 默认路由访问对端网段。

## 拓扑图

```console
$ nslab graph
Topology: ipv6-forward

r1 [linux]
  eth0: 2001:db8:1::1/64
  eth1: 2001:db8:2::1/64
├─ eth0 ↔ eth0  h1 [linux]
│               eth0: 2001:db8:1::2/64
└─ eth1 ↔ eth0  h2 [linux]
                eth0: 2001:db8:2::2/64
```

## 运行

```bash
cd examples/ipv6-forward
sudo nslab deploy
sleep 2
sudo nslab inspect
```

短暂等待用于让内核完成 IPv6 duplicate address detection（DAD）。

## 观察路由和转发

```bash
sudo nslab exec --node r1 -- cat /proc/sys/net/ipv6/conf/all/forwarding
sudo nslab exec --node r1 -- ip -6 address show
sudo nslab exec --node h1 -- ip -6 route show
sudo nslab exec --node h1 -- ip -6 route get 2001:db8:2::2
sudo nslab exec --node h2 -- ip -6 route show
```

转发开关应为 `1`。内核自动生成的 link-local 地址不属于 manifest 声明状态。

## 验证通信

```bash
sudo nslab exec --node h1 -- ping -6 -c 3 2001:db8:2::2
sudo nslab exec --node h2 -- ping -6 -c 3 2001:db8:1::2
sudo nslab exec --node r1 -- ip -s link show eth0
sudo nslab exec --node r1 -- ip -s link show eth1
```

ICMPv6 包经过 `r1` 后 hop limit 会减一。

## 清理

```bash
sudo nslab destroy
```

[查看 nslab.yaml](https://github.com/calcky/nslab/blob/main/examples/ipv6-forward/nslab.yaml) ·
[查看示例 README](https://github.com/calcky/nslab/blob/main/examples/ipv6-forward/README.md)
