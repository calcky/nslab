# Linux bridge VLAN 实验

这个实验使用两台 VLAN-aware Linux bridge。VLAN 10 和 VLAN 20 分别有两个 access
主机，两台 bridge 之间的 trunk 以 tagged 方式同时承载两个 VLAN：

```text
h10a -- access VLAN 10 -- sw1 ===== trunk 10,20 ===== sw2 -- access VLAN 10 -- h10b
h20a -- access VLAN 20 -- sw1                         sw2 -- access VLAN 20 -- h20b
```

四台主机故意配置在同一个 `10.0.0.0/24` 子网，以便直接观察 VLAN 的二层隔离效果。

## 运行

```bash
nslab graph --detail
sudo nslab deploy
sudo nslab inspect
```

## 查看端口 VLAN

```bash
sudo nslab exec --node sw1 -- bridge vlan show
sudo nslab exec --node sw2 -- bridge vlan show
```

`access10` 和 `access20` 分别显示对应 VLAN 的 `PVID Egress Untagged`；`trunk` 上的
VLAN 10、20 不带 PVID/untagged 标志，因此帧在 trunk 上传输时保留 802.1Q tag。

## 验证隔离

同 VLAN 流量应通过 trunk 正常转发：

```bash
sudo nslab exec --node h10a -- ping -c 3 10.0.0.2
sudo nslab exec --node h20a -- ping -c 3 10.0.0.4
```

跨 VLAN 主机虽然位于同一个 IP 子网，但 ARP 广播不会越过 VLAN 边界，下面的命令应
超时并返回非零状态：

```bash
sudo nslab exec --node h10a -- ping -c 2 -W 1 10.0.0.3
```

可以分别查看每个 VLAN 学到的 FDB 表项：

```bash
sudo nslab exec --node sw1 -- bridge fdb show br br0
sudo nslab exec --node sw2 -- bridge fdb show br br0
```

## 清理

```bash
sudo nslab destroy
```
