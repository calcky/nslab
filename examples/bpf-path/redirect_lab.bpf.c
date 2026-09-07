#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/pkt_cls.h>
#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>
#include "packet.bpf.h"

enum counter { SEEN, REDIRECTED, MAP_MISS, FIB_FALLBACK, PASSED, COUNTERS };

struct {
    __uint(type, BPF_MAP_TYPE_DEVMAP_HASH);
    __uint(max_entries, 16);
    __type(key, __u32);
    __type(value, __u32);
} devices SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, COUNTERS);
    __type(key, __u32);
    __type(value, __u64);
} counters SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u32);
} settings SEC(".maps");

static __always_inline void count(__u32 key)
{
    __u64 *value = bpf_map_lookup_elem(&counters, &key);
    if (value)
        (*value)++;
}

static __always_inline void prepare_fib(struct bpf_fib_lookup *fib,
                                        struct iphdr *ip, __u32 ingress)
{
    fib->family = 2;
    fib->tos = ip->tos;
    fib->l4_protocol = ip->protocol;
    fib->tot_len = bpf_ntohs(ip->tot_len);
    fib->ipv4_src = ip->saddr;
    fib->ipv4_dst = ip->daddr;
    fib->ifindex = ingress;
}

static __always_inline void rewrite(struct ethhdr *eth, struct iphdr *ip,
                                   struct bpf_fib_lookup *fib)
{
    /* Decrement TTL exactly once, including its IPv4 checksum contribution. */
    __u32 sum = (__u32)bpf_ntohs(ip->check) + 0x100;
    ip->check = bpf_htons((sum & 0xffff) + (sum >> 16));
    ip->ttl--;
    __builtin_memcpy(eth->h_source, fib->smac, ETH_ALEN);
    __builtin_memcpy(eth->h_dest, fib->dmac, ETH_ALEN);
}

SEC("xdp")
int redirect_devmap(struct xdp_md *ctx)
{
    void *data = (void *)(long)ctx->data;
    void *end = (void *)(long)ctx->data_end;
    struct bpf_fib_lookup fib = {};
    struct iphdr *ip = echo(data, end);
    int action;

    count(SEEN);
    if (!ip || ip->ttl <= 1)
        goto pass;
    prepare_fib(&fib, ip, ctx->ingress_ifindex);
    if (bpf_fib_lookup(ctx, &fib, sizeof(fib), 0) != BPF_FIB_LKUP_RET_SUCCESS) {
        count(FIB_FALLBACK);
        goto pass;
    }
    action = bpf_redirect_map(&devices, fib.ifindex, XDP_PASS);
    if (action != XDP_REDIRECT) {
        count(MAP_MISS);
        goto pass;
    }
    /* Resolve map fallback before rewriting: Linux must see the original TTL. */
    rewrite(data, ip, &fib);
    count(REDIRECTED);
    return action;
pass:
    count(PASSED);
    return XDP_PASS;
}

SEC("tc")
int redirect_tc(struct __sk_buff *skb)
{
    void *data = (void *)(long)skb->data;
    void *end = (void *)(long)skb->data_end;
    struct bpf_fib_lookup fib = {};
    struct iphdr *ip = echo(data, end);
    __u32 key = 0;
    __u32 *output = bpf_map_lookup_elem(&settings, &key);

    count(SEEN);
    if (!ip || ip->ttl <= 1 || !output)
        goto pass;
    prepare_fib(&fib, ip, skb->ifindex);
    if (bpf_fib_lookup(skb, &fib, sizeof(fib), 0) != BPF_FIB_LKUP_RET_SUCCESS) {
        count(FIB_FALLBACK);
        goto pass;
    }
    if (fib.ifindex != *output)
        goto pass;
    rewrite(data, ip, &fib);
    count(REDIRECTED);
    return bpf_redirect(fib.ifindex, 0);
pass:
    count(PASSED);
    return TC_ACT_OK;
}

char LICENSE[] SEC("license") = "GPL";
