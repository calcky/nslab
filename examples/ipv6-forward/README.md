# Linux IPv6 转发实验

这个实验用于观察 Linux 的 IPv6 路由与转发路径。`r1` 连接两个 `/64` 子网，并通过
`net.ipv6.conf.all.forwarding=1` 开启 IPv6 转发：

```text
h1                         r1                                  h2
2001:db8:1::2/64  --  2001:db8:1::1/64 | 2001:db8:2::1/64  --  2001:db8:2::2/64
```

两台主机使用显式 `::/0` 默认路由，分别指向 `r1` 的相邻接口。

## 运行

```bash
nslab graph --detail
sudo nslab deploy
sleep 2
sudo nslab inspect
```

短暂等待用于让内核完成 IPv6 duplicate address detection（DAD），然后再开始发流量。

## 观察 IPv6 配置

```bash
sudo nslab exec --node r1 -- cat /proc/sys/net/ipv6/conf/all/forwarding
sudo nslab exec --node r1 -- ip -6 address show
sudo nslab exec --node h1 -- ip -6 route show
sudo nslab exec --node h1 -- ip -6 route get 2001:db8:2::2
sudo nslab exec --node h2 -- ip -6 route show
```

转发开关应输出 `1`。除 manifest 声明的全局地址外，内核还会自动生成 link-local
地址；它们不属于 nslab 的声明状态。

## 验证双向转发

```bash
sudo nslab exec --node h1 -- ping -6 -c 3 2001:db8:2::2
sudo nslab exec --node h2 -- ping -6 -c 3 2001:db8:1::2
sudo nslab exec --node r1 -- ip -s link show eth0
sudo nslab exec --node r1 -- ip -s link show eth1
```

ICMPv6 echo 包经过 `r1` 后 hop limit 会减一，路由器两侧接口计数器会随流量增加。

## 清理

```bash
sudo nslab destroy
```

修改 `nslab.yaml` 后可以用 `sudo nslab redeploy` 重新创建整个实验。
