# Getting started

## Requirements

Install the basic tools on Ubuntu 22.04 or newer:

```bash
sudo apt update
sudo apt install -y iproute2 iputils-ping
```

Install FRRouting as well when running the `ospf` or `bgp` examples:

```bash
sudo apt install -y frr frr-pythontools
```

All lifecycle commands that create, change, or remove network resources require root. Graph
rendering and manifest validation do not read live state and can run without root.

## Install nslab

### Linux x86_64 release

Download the required version from [GitHub Releases](https://github.com/calcky/nslab/releases)
and verify its SHA-256 checksum:

```bash
VERSION=v0.1.0
curl -fLO "https://github.com/calcky/nslab/releases/download/${VERSION}/nslab-${VERSION}-linux-x86_64.tar.gz"
curl -fLO "https://github.com/calcky/nslab/releases/download/${VERSION}/SHA256SUMS"
sha256sum --check --ignore-missing SHA256SUMS
tar -xzf "nslab-${VERSION}-linux-x86_64.tar.gz"
sudo install -m 0755 nslab /usr/local/bin/nslab
nslab --help
```

The standalone binary is built on Ubuntu 22.04 x86_64 and does not require a system Python.
FRRouting must still be installed separately.

### Run from source

Source development requires Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/calcky/nslab.git
cd nslab
uv python install 3.12
uv sync --python 3.12
```

Use `.venv/bin/nslab` in place of a globally installed `nslab` command.

## Create and remove a topology

The shortest lifecycle from a directory containing `nslab.yaml` is:

```bash
nslab graph
sudo nslab deploy
sudo nslab inspect
sudo nslab destroy
```

Without `-t/--topo`, nslab reads only `./nslab.yaml` in the current directory and does not
search parent directories. Without `-n/--name`, it uses the manifest's `name`. You can select
both explicitly:

```bash
sudo nslab deploy --topo /path/to/lab.yaml --name my-lab
sudo nslab inspect --name my-lab
sudo nslab destroy --topo /path/to/lab.yaml --name my-lab
```

When `inspect`, `exec`, or `destroy` receives only `--name`, it restores the saved topology from
`/var/lib/nslab/<name>.json`. Passing the YAML to `destroy` also allows nslab to prove that the
planned resources are absent after state has already been removed.

## Redeploy and recover

After editing the manifest, use `redeploy` to validate the replacement plan, destroy the old
resources, and create the new topology under the same deployment lock:

```bash
sudo nslab redeploy
```

`inspect` reports one of four summary states:

| Status | Meaning |
| --- | --- |
| `absent` | No state exists and the planned resources are absent |
| `deployed` | State, manifest, and live resources match |
| `degraded` | A resource is missing, changed, or unexpectedly present |
| `stale` | State exists but all planned resources have disappeared |

Capture JSON diagnostics before recovering a degraded deployment:

```bash
sudo nslab inspect --name my-lab --format json
```

After confirming that the resources belong to that deployment, use the same YAML with `destroy`
or `redeploy`. Do not delete state files manually. They contain the exact namespace and interface
names used to keep cleanup scoped.
