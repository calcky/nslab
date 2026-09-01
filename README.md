# nslab

`nslab` 是面向 Linux network namespace 实验的声明式 CLI。拓扑写在严格校验的
`nslab.yaml` 中，namespace、veth、bridge、IPv4 地址、路由和 sysctl 均通过
pyroute2 创建，不依赖 `ip`、`bridge` 或生命周期 shell hook。

首个完整示例是两台 Linux 主机通过 Linux bridge 转发 IPv4 数据包：
[examples/bridge-fdb/nslab.yaml](examples/bridge-fdb/nslab.yaml)。拓扑文件不包含
`traffic`、`observe`、抓包或任意命令字段；流量和观察操作通过 `nslab exec` 单独执行。

## 环境要求

- x86_64 Linux；推荐 Ubuntu 22.04 或更高版本
- root 权限，用于 deploy、destroy、redeploy、inspect 和 exec
- `iproute2` 与 `iputils-ping`，分别用于人工检查和示例 ping

## 安装

### Linux x86_64 发布包

GitHub Release 提供基于 Ubuntu 22.04 构建的独立可执行程序，不要求系统预装 Python。
安装最新版本时，将 `VERSION` 调整为目标 tag：

```bash
sudo apt update
sudo apt install -y curl iproute2 iputils-ping

VERSION=v0.1.0
curl -fLO "https://github.com/calcky/nslab/releases/download/${VERSION}/nslab-${VERSION}-linux-x86_64.tar.gz"
curl -fLO "https://github.com/calcky/nslab/releases/download/${VERSION}/SHA256SUMS"
sha256sum --check --ignore-missing SHA256SUMS
tar -xzf "nslab-${VERSION}-linux-x86_64.tar.gz"
sudo install -m 0755 nslab /usr/local/bin/nslab
nslab --help
```

### 从源码安装

源码运行需要 Python 3.12 或 3.13 和 [uv](https://docs.astral.sh/uv/)。Ubuntu 中安装
系统依赖并初始化开发环境：

```bash
sudo apt update
sudo apt install -y iproute2 iputils-ping
git clone https://github.com/calcky/nslab.git
cd nslab
uv python install 3.12
uv sync --python 3.12
```

生成拓扑图不需要 root，也不会读取 live state。默认输出适合终端阅读的 Unicode
拓扑树，其中保留 bridge 名称和 IPv4 地址；STP 与 VLAN filtering 状态仅在传入
`--detail` 时显示：

```bash
uv run nslab graph -t examples/bridge-fdb/nslab.yaml
uv run nslab graph -t examples/bridge-fdb/nslab.yaml --detail
```

需要 root 的命令应直接使用虚拟环境中的绝对可执行文件，避免由 `sudo` 重新解析
Python 环境：

```bash
sudo "$(pwd)/.venv/bin/nslab" deploy -t examples/bridge-fdb/nslab.yaml
sudo "$(pwd)/.venv/bin/nslab" inspect --name bridge-fdb
sudo "$(pwd)/.venv/bin/nslab" exec --name bridge-fdb --node h1 -- ping -c 3 10.10.0.2
sudo "$(pwd)/.venv/bin/nslab" destroy --name bridge-fdb
```

## 默认拓扑

不传 `-t/--topo` 和 `-n/--name` 时，nslab 只读取当前目录的
`./nslab.yaml`，不会向父目录搜索。从示例目录可以直接运行：

```bash
cd /home/captain/nslab/examples/bridge-fdb
../../.venv/bin/nslab graph
../../.venv/bin/nslab graph --detail
../../.venv/bin/nslab graph --format box --detail
../../.venv/bin/nslab graph --format mermaid
sudo ../../.venv/bin/nslab deploy
sudo ../../.venv/bin/nslab inspect
sudo ../../.venv/bin/nslab exec --node h1 -- ping -c 3 10.10.0.2
sudo ../../.venv/bin/nslab destroy
```

`-t` 与 `--topo` 等价，`-n` 与 `--name` 等价。对于 deploy，`--name`
覆盖 YAML 中的名称。其他命令只传 `--name` 时，会从
`/var/lib/nslab/<name>.json` 加载已保存的拓扑，不再需要原始 YAML。若同时传入
`--topo` 和 `--name`，则加载指定 YAML 并使用名称覆盖值。

## 命令

```text
nslab deploy    [-t PATH] [-n NAME]
nslab destroy   [-t PATH] [-n NAME]
nslab redeploy  [-t PATH] [-n NAME]
nslab inspect   [-t PATH] [-n NAME] [--format table|json]
nslab exec      [-t PATH] [-n NAME] --node NODE -- COMMAND [ARG ...]
nslab graph     [-t PATH] [-n NAME] [--detail]
                [--format tree|box|mermaid|dot|json]
```

- `deploy`：创建拓扑；相同拓扑已完整部署时成功返回 unchanged/no-op。
- `destroy`：删除该计划精确拥有的资源；使用 YAML 对已不存在的拓扑重复执行仍成功。
- `redeploy`：先完整验证新计划，再在同一 deployment lock 下 destroy 和 deploy。
- `inspect`：比较 desired、持久化 state 和 live kernel state。
- `exec`：在指定节点 namespace 中直接执行 `--` 后的 argv，不隐式启动 shell；
  命令继承当前终端的输入、输出与错误流，因此长时间运行的输出会实时显示。按 Ctrl-C
  会终止命令，nslab 以状态码 130 退出，不输出 pyroute2 辅助进程 traceback。
- `graph`：默认输出 Unicode 终端拓扑树；也可输出二维 Unicode 方框图、Mermaid、
  DOT 或 JSON，且不读取 live state。默认 tree 和 box 保留 bridge 名称与 IPv4 地址，
  `--detail` 会追加 STP 与 VLAN filtering 状态，且仅适用于 tree 和 box。

此前依赖隐式 Mermaid 源码的脚本现在必须显式传入 `--format mermaid`。

示例完整流程：

```bash
NSLAB=/home/captain/nslab/.venv/bin/nslab
TOPO=/home/captain/nslab/examples/bridge-fdb/nslab.yaml

"$NSLAB" graph -t "$TOPO"
sudo "$NSLAB" deploy -t "$TOPO"
sudo "$NSLAB" deploy -t "$TOPO"
sudo "$NSLAB" inspect --name bridge-fdb --format json
sudo "$NSLAB" exec --name bridge-fdb --node h1 -- ping -c 3 10.10.0.2
sudo "$NSLAB" redeploy -t "$TOPO"
sudo "$NSLAB" destroy -t "$TOPO"
sudo "$NSLAB" destroy -t "$TOPO"
```

第二次 deploy 是成功 no-op。重复 destroy 时保留 `-t`，这样 state 已删除后仍可从
YAML 精确证明预期资源应当不存在。

## Obsidian Execute Code

nslab 不安装或修改 Obsidian 插件，也不修改 sudoers。Obsidian 现有的 Execute Code
能力只需调用同一个 CLI。在 Linux 上可以直接执行前面的 `nslab` 命令。若 Obsidian
运行在 Windows，可选用以下 PowerShell 代码块调用 WSL；将 distribution 名称和
Linux 路径调整为本机实际值：

```powershell
$Distro = "Ubuntu-24.04"
$Repo = "/home/captain/nslab"
$Nslab = "$Repo/.venv/bin/nslab"
$Topo = "$Repo/examples/bridge-fdb/nslab.yaml"

wsl.exe --distribution $Distro --user root --cd $Repo $Nslab deploy -t $Topo
wsl.exe --distribution $Distro --user root --cd $Repo $Nslab inspect --name bridge-fdb
wsl.exe --distribution $Distro --user root --cd $Repo $Nslab exec --name bridge-fdb --node h1 -- ping -c 3 10.10.0.2
wsl.exe --distribution $Distro --user root --cd $Repo $Nslab destroy -t $Topo
```

每个代码块也可以只放一个命令，便于在实验笔记中分别设置“部署”“检查”“执行”和
“销毁”按钮。不要把不可信文本拼接进 `exec` 参数。

> [!WARNING]
> Network namespace 只隔离网络栈。以 root 运行的 `nslab exec` 进程仍拥有 Linux
> 主机文件系统的 root 权限；它不是容器或安全沙箱。

## 状态与恢复

`inspect` 的概要状态有四种：

- `absent`：没有 state，且能够证明计划资源不存在。
- `deployed`：state、拓扑语义和 live 资源完全匹配。
- `degraded`：资源缺失、被修改、额外存在，或 veth identity 与 state 不一致。
- `stale`：state 仍存在，但计划资源已全部消失。

恢复时先保存 JSON 诊断：

```bash
sudo /home/captain/nslab/.venv/bin/nslab inspect --name bridge-fdb --format json
```

对于 `degraded`，确认差异只涉及该 deployment 后，使用同一 YAML 执行 destroy，修正
YAML 后重新 deploy；也可以用 redeploy 完成这两个动作。对于 `stale`，先执行
`destroy --name <name>` 清除 stale state，再 deploy。

redeploy 会先验证替代计划。若新 deploy 失败，旧拓扑不会自动恢复；成功回滚后拓扑为
absent，可修正 YAML 后重新 deploy。若回滚不完整，nslab 会保留 `deploying` 或
`destroying` state 以避免猜测所有权；此时不要删除 state 文件或使用前缀批量清理，
应根据 inspect JSON 和 state 中的精确 namespace/interface 名称处理残留资源。

## 开发验证

```bash
uv run pytest tests/unit -q
sudo -E "$(pwd)/.venv/bin/pytest" \
  tests/integration -m root -q \
  --ignore=tests/integration/test_bridge_fdb_e2e_root.py
uv run ruff check src tests
uv run mypy src
uv build
```

GitHub Actions 会在 Ubuntu 22.04 x86_64 上执行静态检查、Python 3.12/3.13 测试和
root network namespace 集成测试。推送 `v*` tag 后，只有所有检查通过才会发布 Linux
x86_64 可执行程序：

```bash
git tag v0.1.0
git push origin v0.1.0
```
