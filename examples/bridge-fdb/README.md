# Linux bridge FDB 实验

这个实验用于观察 Linux bridge 的二层转发和 FDB（Forwarding Database）学习过程。
`h1` 和 `h2` 位于同一 IPv4 子网，中间由 `sw1` 的 `br0` 转发：

```text
h1:eth0 (10.10.0.1/24) -- sw1:br0 -- h2:eth0 (10.10.0.2/24)
```

## 运行

从当前目录执行。`graph` 不需要 root，其余命令需要 root：

```bash
nslab graph --detail
sudo nslab deploy
sudo nslab inspect
```

部署和重复部署都是安全的。第二次 `deploy` 会报告拓扑已经存在，不会重复创建资源。

## 观察 FDB 学习

先查看初始 FDB，再从 `h1` 向 `h2` 发包，最后重新查看 FDB 和端口计数器：

```bash
sudo nslab exec --node sw1 -- bridge fdb show br br0
sudo nslab exec --node h1 -- ping -c 3 10.10.0.2
sudo nslab exec --node sw1 -- bridge fdb show br br0
sudo nslab exec --node sw1 -- ip -s link show swp1
sudo nslab exec --node sw1 -- ip -s link show swp2
```

ping 前主要能看到本地和永久表项。收到数据帧后，`sw1` 会把源 MAC 与入端口关联，
FDB 中会出现动态表项；两个 bridge 端口的 RX/TX 计数也会增加。

## 清理

```bash
sudo nslab destroy
sudo nslab destroy
```

在示例目录中保留 `nslab.yaml` 时，重复 `destroy` 也是成功操作，便于反复执行实验。
