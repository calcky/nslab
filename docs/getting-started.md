# 快速开始

## 环境要求

在 Ubuntu 22.04 或更新版本上安装基础工具：

```bash
sudo apt update
sudo apt install -y iproute2 iputils-ping
```

运行 `ospf` 或 `bgp` 示例时，再安装 FRRouting：

```bash
sudo apt install -y frr frr-pythontools
```

所有创建、修改或删除网络资源的生命周期命令都需要 root。图形渲染和 manifest 校验
不读取 live state，可以不使用 root 执行。

## 安装 nslab

### Linux x86_64 发布包

从 [GitHub Releases](https://github.com/calcky/nslab/releases) 下载目标版本，并校验
SHA-256：

```bash
VERSION=v0.1.0
curl -fLO "https://github.com/calcky/nslab/releases/download/${VERSION}/nslab-${VERSION}-linux-x86_64.tar.gz"
curl -fLO "https://github.com/calcky/nslab/releases/download/${VERSION}/SHA256SUMS"
sha256sum --check --ignore-missing SHA256SUMS
tar -xzf "nslab-${VERSION}-linux-x86_64.tar.gz"
sudo install -m 0755 nslab /usr/local/bin/nslab
nslab --help
```

发布包是 Ubuntu 22.04 x86_64 上构建的独立程序，不要求系统预装 Python。FRR 仍需
单独安装。

### 从源码运行

源码开发需要 Python 3.12 或 3.13 以及 [uv](https://docs.astral.sh/uv/)：

```bash
git clone https://github.com/calcky/nslab.git
cd nslab
uv python install 3.12
uv sync --python 3.12
```

之后可用 `.venv/bin/nslab` 替代已安装到 `PATH` 的 `nslab`。

## 创建和清理拓扑

在包含 `nslab.yaml` 的目录中，最短的生命周期流程是：

```bash
nslab graph
sudo nslab deploy
sudo nslab inspect
sudo nslab destroy
```

不传 `-t/--topo` 时只读取当前目录的 `./nslab.yaml`，不会向父目录搜索。不传
`-n/--name` 时使用 manifest 中的 `name`。也可以显式选择文件和 deployment 名称：

```bash
sudo nslab deploy --topo /path/to/lab.yaml --name my-lab
sudo nslab inspect --name my-lab
sudo nslab destroy --topo /path/to/lab.yaml --name my-lab
```

`inspect`、`exec` 和 `graph` 只传 `--name` 时，会从 `/var/lib/nslab/<name>.json`
加载保存的拓扑；`destroy` 同时传入 YAML 可以在 state 已删除后再次证明资源应当
不存在。

## 重建和恢复

编辑 manifest 后，用 `redeploy` 在同一个 deployment lock 下验证新计划、销毁旧资源
并创建新资源：

```bash
sudo nslab redeploy
```

`inspect` 的概要状态含义如下：

| 状态 | 含义 |
| --- | --- |
| `absent` | 没有 state，且计划资源不存在 |
| `deployed` | state、manifest 和 live 资源一致 |
| `degraded` | 资源缺失、被改动或出现额外资源 |
| `stale` | state 还在，但计划资源已全部消失 |

遇到 `degraded` 时先保存诊断：

```bash
sudo nslab inspect --name my-lab --format json
```

确认资源属于该 deployment 后，再用相同 YAML 执行 `destroy` 或 `redeploy`。不要手动
删除 state 文件；state 中记录了精确的 namespace 和接口名称，用于限制清理范围。
