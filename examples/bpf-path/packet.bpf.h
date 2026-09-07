#ifndef NSLAB_PACKET_BPF_H
#define NSLAB_PACKET_BPF_H

#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>

/* Full, unfragmented IPv4 Echo Requests; other traffic stays in Linux. */
static __always_inline struct iphdr *echo(void *data, void *end)
{
    struct ethhdr *eth = data;
    struct iphdr *ip = (void *)(eth + 1);
    unsigned char *icmp;
    __u32 length;

    if ((void *)(eth + 1) > end || eth->h_proto != bpf_htons(ETH_P_IP))
        return 0;
    if ((void *)(ip + 1) > end || ip->version != 4 || ip->protocol != 1)
        return 0;
    if (ip->frag_off & bpf_htons(0x3fff))
        return 0;
    length = (__u32)ip->ihl * 4;
    if (length < sizeof(*ip) || bpf_ntohs(ip->tot_len) < length + 8)
        return 0;
    if ((void *)ip + bpf_ntohs(ip->tot_len) > end)
        return 0;
    icmp = (void *)ip + length;
    if ((void *)(icmp + 8) > end || icmp[0] != 8 || icmp[1] != 0)
        return 0;
    return ip;
}
#endif
