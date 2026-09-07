#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/pkt_cls.h>
#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>

/* Ethernet/IPv4 ICMP echo requests only; ARP, IPv6 and fragments pass. */
static __always_inline int is_echo(void *data, void *end)
{
    struct ethhdr *eth = data;
    struct iphdr *ip;
    unsigned char *icmp;
    __u32 length;

    if ((void *)(eth + 1) > end || eth->h_proto != bpf_htons(ETH_P_IP))
        return 0;
    ip = (void *)(eth + 1);
    if ((void *)(ip + 1) > end || ip->version != 4 || ip->protocol != 1)
        return 0;
    if (ip->frag_off & bpf_htons(0x3fff))
        return 0;
    length = (__u32)ip->ihl * 4;
    if (length < sizeof(*ip))
        return 0;
    icmp = (void *)ip + length;
    if ((void *)(icmp + 8) > end)
        return 0;
    return icmp[0] == 8 && icmp[1] == 0;
}

SEC("xdp/drop")
int xdp_drop(struct xdp_md *ctx)
{
    return is_echo((void *)(long)ctx->data, (void *)(long)ctx->data_end)
        ? XDP_DROP : XDP_PASS;
}

SEC("classifier/drop")
int tc_drop(struct __sk_buff *skb)
{
    return is_echo((void *)(long)skb->data, (void *)(long)skb->data_end)
        ? TC_ACT_SHOT : TC_ACT_OK;
}

SEC("classifier/pass")
int tc_pass(struct __sk_buff *skb)
{
    (void)skb;
    return TC_ACT_OK;
}

char LICENSE[] SEC("license") = "GPL";
