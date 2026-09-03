# Bond overview

Linux bonding combines multiple interfaces into one logical link. nslab keeps the two modes in
separate runnable labs because they answer different questions while sharing the same two-host,
two-link shape.

| Mode | What it demonstrates | Link behavior | Best starting point |
| --- | --- | --- | --- |
| `active-backup` | Preferred-member failover and recovery | One member forwards; the backup takes over when the active link fails | [Bond active-backup](bond-active-backup.md) |
| `802.3ad` | LACP negotiation and aggregation | Multiple members can forward; flows are distributed by a hash | [Bond 802.3ad](bond-8023ad.md) |

## Which lab should I run?

Choose `active-backup` when the goal is availability without requiring LACP on the peer. It is the
shorter path to seeing carrier loss, active-slave changes, and recovery to a preferred member.

Choose `802.3ad` when the goal is link aggregation. Both ends must run LACP, and multiple flows are
needed to observe distribution across members; a single flow normally remains on one link.

## Common workflow

Run either lab independently from its own directory:

```console
$ cd examples/bond-active-backup    # choose examples/bond-8023ad instead
$ nslab graph
$ sudo nslab deploy
$ sudo nslab inspect
$ sudo nslab destroy
```

The individual pages contain the mode-specific manifest, expected output, failure simulation, and
observation commands:

- [Bond active-backup](bond-active-backup.md): fail over `eth0`, then restore it as the primary.
- [Bond 802.3ad](bond-8023ad.md): inspect the LACP aggregator and compare multi-flow counters.

Both labs use two Linux namespaces, two linked member interfaces, and put the IP address on
`bond0`. The independent manifests keep deployment names, address ranges, and expected state
focused on one bonding mode at a time.
