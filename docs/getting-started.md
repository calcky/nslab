# Getting started

## Requirements

Install the basic tools on Ubuntu 22.04 or newer:

```bash
sudo apt update
sudo apt install -y iproute2 iputils-ping
```

Install FRRouting as well when running the `ospf`, `bgp`, or `pim` examples:

```bash
sudo apt install -y frr frr-pythontools
```

The packet captures and path-MTU probes in the `pmtu` example use two optional tools:

```bash
sudo apt install -y tcpdump iputils-tracepath
```

The concurrent-flow steps in the `qdisc` and `cake` examples use `iperf3`:

```bash
sudo apt install -y iperf3
```

The CAKE example also requires a kernel that provides `sch_cake`. Check or load it with
`sudo modprobe sch_cake`; nslab reports `QDISC_UNSUPPORTED` when the qdisc is unavailable.

All lifecycle commands that create, change, or remove network resources require root. Graph
rendering and manifest validation do not read live state and can run without root.

## Install nslab

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
