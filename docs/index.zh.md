# nslab

`nslab` 是一个面向 Linux network namespace 实验的声明式 CLI。用一份严格校验的
`nslab.yaml` 描述节点、veth、Linux bridge、地址、路由、sysctl、STP、VLAN 和 netem，
然后用同一组命令反复部署、检查、执行和销毁拓扑。

它适合学习 Linux 内核网络路径，也适合把一次实验保存成可以再次运行的笔记。拓扑
文件只描述网络资源；流量和观察动作通过 `nslab exec` 显式执行，因此实验定义不会
偷偷启动后台进程或携带抓包数据。

!!! warning "需要 root"

    创建 network namespace、veth 和 bridge 需要 root 权限。`nslab exec` 中以 root
    启动的命令仍然拥有主机文件系统的 root 权限，namespace 不是安全沙箱。

## 三步运行第一个实验

在仓库的 `examples/bridge-fdb` 目录中执行：

```bash
cd examples/bridge-fdb
nslab graph
sudo nslab deploy
sudo nslab exec --node h1 -- ping -c 3 10.10.0.2
sudo nslab destroy
```

`deploy` 和 `destroy` 都是可重复的。拓扑已经存在时再次 `deploy` 会返回成功的
no-op；保留 `nslab.yaml` 时，重复 `destroy` 也会成功。

## 文档导航

| 页面 | 内容 |
| --- | --- |
| [快速开始](getting-started.md) | 安装、权限、生命周期和故障恢复 |
| [Manifest](manifest.md) | `nslab.yaml` 的字段、约束和完整片段 |
| [CLI 参考](cli.md) | 命令、选项、输出格式和补全 |
| [实验示例](examples/index.md) | bridge、STP、VLAN、转发、netem、OSPF、BGP |

## 设计边界

- 支持 Linux x86_64，推荐 Ubuntu 22.04 或更高版本。
- 网络资源由 pyroute2 管理，不依赖 `ip`、`bridge` 或生命周期 shell hook。
- 当前 node 类型是 `linux` 和 `bridge`；链路类型是 `veth`。
- OSPFv2 与 eBGP 通过系统安装的 FRRouting daemon 运行，发布包不内置 FRR。
- Bash、Zsh 补全只输出脚本，不修改 shell 配置文件。

源码和问题追踪位于 [GitHub](https://github.com/calcky/nslab)。
