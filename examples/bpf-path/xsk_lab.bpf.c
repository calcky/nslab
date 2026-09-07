#include "packet.bpf.h"

struct {
    __uint(type, BPF_MAP_TYPE_XSKMAP);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u32);
} sockets SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __be32);
} local_address SEC(".maps");

SEC("xdp")
int xsk_ingress(struct xdp_md *ctx)
{
    struct iphdr *ip = echo((void *)(long)ctx->data, (void *)(long)ctx->data_end);
    __u32 key = 0;
    __be32 *address = bpf_map_lookup_elem(&local_address, &key);

    if (!ip || !address || ip->ihl != 5 || ip->daddr != *address || ctx->rx_queue_index != 0)
        return XDP_PASS;
    return bpf_redirect_map(&sockets, ctx->rx_queue_index, XDP_PASS);
}

char LICENSE[] SEC("license") = "GPL";
