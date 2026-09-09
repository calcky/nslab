# Manifest: qdisc

Set `qdisc` beside `endpoints` in a link. It is installed independently on both veth
egress directions. The complete qdisc field reference, including per-kind examples for
`pfifo`, `bfifo`, `pfifo_fast`, `prio`, `sfq`, `fq`, `codel`, `fq_codel`,
`tbf`, `htb`, `cake`, and `red`, is in the [qdisc section of the manifest reference](manifest.md#linksqdisc).

Start with the [qdisc lab](examples/qdisc.md) for a runnable topology. Note that the eight
new lightweight kinds currently have schema coverage but incomplete pyroute2 deployment
integration; the reference page calls out those limitations.
