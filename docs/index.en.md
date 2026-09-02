# nslab

`nslab` is a declarative CLI for Linux network namespace labs. Describe nodes, veth pairs,
Linux bridges, addresses, routes, sysctls, STP, VLANs, and netem in a strictly validated
`nslab.yaml`, then use the same commands to deploy, inspect, execute, and destroy the topology
as often as needed.

It is intended for learning Linux kernel networking paths and for saving labs as reproducible
notes. A topology file contains only network resources. Traffic and observation commands are
run explicitly with `nslab exec`, so a manifest cannot silently launch arbitrary hooks or embed
packet captures.

!!! warning "Root privileges are required"

    Creating network namespaces, veth pairs, and bridges requires root. A command started by
    `nslab exec` as root still has root access to the host filesystem. A network namespace is
    not a security sandbox.

## Run your first lab

From the repository's `examples/bridge-fdb` directory:

```bash
cd examples/bridge-fdb
nslab graph
sudo nslab deploy
sudo nslab exec --node h1 -- ping -c 3 10.10.0.2
sudo nslab destroy
```

Both `deploy` and `destroy` are repeatable. Deploying an unchanged topology returns a successful
no-op. As long as `nslab.yaml` remains available, destroying an already absent topology also
succeeds.

## Documentation

| Page | Contents |
| --- | --- |
| [Getting started](getting-started.md) | Installation, privileges, lifecycle, and recovery |
| [Manifest](manifest.md) | `nslab.yaml` fields, constraints, and complete snippets |
| [CLI reference](cli.md) | Commands, options, output formats, and completion |
| [Examples](examples/index.md) | Bridge, STP, VLAN, forwarding, netem, OSPF, and BGP labs |

## Scope

- Supports x86_64 Linux; Ubuntu 22.04 or newer is recommended.
- Manages network resources through pyroute2 without `ip`, `bridge`, or lifecycle shell hooks.
- Currently supports `linux` and `bridge` node kinds and `veth` links.
- Runs OSPFv2 and eBGP through system-installed FRRouting daemons; FRR is not bundled.
- Generates Bash and Zsh completion scripts without modifying shell configuration files.

Source code and issue tracking are available on [GitHub](https://github.com/calcky/nslab).
