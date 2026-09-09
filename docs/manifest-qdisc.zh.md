# Manifest：qdisc

在链路的 `endpoints` 同级设置 `qdisc`。它会分别安装到 veth 两端的 egress。
`pfifo`、`bfifo`、`pfifo_fast`、`prio`、`sfq`、`fq`、`codel`、`fq_codel`、
`tbf`、`htb`、`cake` 和 `red` 的字段与逐项示例见[完整参考中的 qdisc 章节](manifest.zh.md#linksqdisc)。

可从[qdisc 实验](examples/qdisc.zh.md)开始。注意，新增的八种轻量 qdisc 目前只有
schema 覆盖，pyroute2 部署接入尚不完整；完整参考中列出了限制。
