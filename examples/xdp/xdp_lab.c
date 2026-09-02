#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>

#include <bpf/bpf_endian.h>
#include <bpf/bpf_helpers.h>

#define ICMP_ECHOREPLY 0
#define ICMP_ECHO 8
#define IPPROTO_ICMP 1
#define AF_INET 2

struct icmp_echo_header {
    __u8 type;
    __u8 code;
    __sum16 checksum;
    __be16 identifier;
    __be16 sequence;
};

enum stat_key {
    STAT_RX,
    STAT_PASS,
    STAT_DROP,
    STAT_TX,
    STAT_REDIRECT,
    STAT_MAX,
};

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, STAT_MAX);
    __type(key, __u32);
    __type(value, __u64);
} nslab_xdp_stats SEC(".maps");

struct echo_packet {
    struct ethhdr *eth;
    struct iphdr *ip;
    struct icmp_echo_header *icmp;
};

struct ipv4_packet {
    struct ethhdr *eth;
    struct iphdr *ip;
};

static __always_inline void count_packet(__u32 key)
{
    __u64 *counter = bpf_map_lookup_elem(&nslab_xdp_stats, &key);

    if (counter != NULL)
        __sync_fetch_and_add(counter, 1);
}

static __always_inline int parse_echo_request(struct xdp_md *ctx, struct echo_packet *packet)
{
    void *data = (void *)(long)ctx->data;
    void *data_end = (void *)(long)ctx->data_end;
    struct ethhdr *eth = data;
    struct iphdr *ip;
    struct icmp_echo_header *icmp;
    __u32 ip_header_length;

    if ((void *)(eth + 1) > data_end || eth->h_proto != bpf_htons(ETH_P_IP))
        return 0;

    ip = (void *)(eth + 1);
    if ((void *)(ip + 1) > data_end || ip->version != 4 || ip->protocol != IPPROTO_ICMP)
        return 0;

    ip_header_length = (__u32)ip->ihl * 4;
    if (ip_header_length < sizeof(*ip) || (void *)ip + ip_header_length > data_end)
        return 0;

    icmp = (void *)ip + ip_header_length;
    if ((void *)(icmp + 1) > data_end || icmp->type != ICMP_ECHO || icmp->code != 0)
        return 0;

    packet->eth = eth;
    packet->ip = ip;
    packet->icmp = icmp;
    return 1;
}

static __always_inline int parse_ipv4_packet(struct xdp_md *ctx, struct ipv4_packet *packet)
{
    void *data = (void *)(long)ctx->data;
    void *data_end = (void *)(long)ctx->data_end;
    struct ethhdr *eth = data;
    struct iphdr *ip;
    __u32 ip_header_length;

    if ((void *)(eth + 1) > data_end || eth->h_proto != bpf_htons(ETH_P_IP))
        return 0;

    ip = (void *)(eth + 1);
    if ((void *)(ip + 1) > data_end || ip->version != 4)
        return 0;

    ip_header_length = (__u32)ip->ihl * 4;
    if (ip_header_length < sizeof(*ip) || (void *)ip + ip_header_length > data_end)
        return 0;

    packet->eth = eth;
    packet->ip = ip;
    return 1;
}

static __always_inline __sum16 replace_checksum_word(
    __sum16 checksum,
    __u16 old_word,
    __u16 new_word)
{
    __u32 sum = (~bpf_ntohs(checksum) & 0xffff) + (~old_word & 0xffff) + new_word;

    sum = (sum & 0xffff) + (sum >> 16);
    sum = (sum & 0xffff) + (sum >> 16);
    return bpf_htons((__u16)~sum);
}

SEC("xdp/pass")
int xdp_pass(struct xdp_md *ctx)
{
    (void)ctx;
    count_packet(STAT_RX);
    count_packet(STAT_PASS);
    return XDP_PASS;
}

SEC("xdp/drop")
int xdp_drop_icmp(struct xdp_md *ctx)
{
    struct echo_packet packet;

    count_packet(STAT_RX);
    if (parse_echo_request(ctx, &packet)) {
        count_packet(STAT_DROP);
        return XDP_DROP;
    }

    count_packet(STAT_PASS);
    return XDP_PASS;
}

SEC("xdp/tx")
int xdp_icmp_echo(struct xdp_md *ctx)
{
    struct echo_packet packet;
    __be32 address;
    __u16 old_type_code;
    __u16 new_type_code;
    __u8 octet;
    int index;

    count_packet(STAT_RX);
    if (!parse_echo_request(ctx, &packet)) {
        count_packet(STAT_PASS);
        return XDP_PASS;
    }

#pragma unroll
    for (index = 0; index < ETH_ALEN; index++) {
        octet = packet.eth->h_source[index];
        packet.eth->h_source[index] = packet.eth->h_dest[index];
        packet.eth->h_dest[index] = octet;
    }

    address = packet.ip->saddr;
    packet.ip->saddr = packet.ip->daddr;
    packet.ip->daddr = address;

    old_type_code = ((__u16)packet.icmp->type << 8) | packet.icmp->code;
    new_type_code = ((__u16)ICMP_ECHOREPLY << 8) | packet.icmp->code;
    packet.icmp->checksum = replace_checksum_word(
        packet.icmp->checksum,
        old_type_code,
        new_type_code);
    packet.icmp->type = ICMP_ECHOREPLY;

    count_packet(STAT_TX);
    return XDP_TX;
}

SEC("xdp/redirect")
int xdp_redirect(struct xdp_md *ctx)
{
    struct bpf_fib_lookup fib = {};
    struct ipv4_packet packet;
    __u16 old_ttl_protocol;
    __u16 new_ttl_protocol;
    int result;

    count_packet(STAT_RX);
    if (!parse_ipv4_packet(ctx, &packet) || packet.ip->ttl <= 1) {
        count_packet(STAT_PASS);
        return XDP_PASS;
    }

    fib.family = AF_INET;
    fib.tos = packet.ip->tos;
    fib.l4_protocol = packet.ip->protocol;
    fib.tot_len = bpf_ntohs(packet.ip->tot_len);
    fib.ipv4_src = packet.ip->saddr;
    fib.ipv4_dst = packet.ip->daddr;
    fib.ifindex = ctx->ingress_ifindex;

    result = bpf_fib_lookup(ctx, &fib, sizeof(fib), BPF_FIB_LOOKUP_DIRECT);
    if (result != BPF_FIB_LKUP_RET_SUCCESS) {
        count_packet(STAT_PASS);
        return XDP_PASS;
    }

    old_ttl_protocol = ((__u16)packet.ip->ttl << 8) | packet.ip->protocol;
    new_ttl_protocol = ((__u16)(packet.ip->ttl - 1) << 8) | packet.ip->protocol;
    packet.ip->check = replace_checksum_word(
        packet.ip->check,
        old_ttl_protocol,
        new_ttl_protocol);
    packet.ip->ttl--;
    __builtin_memcpy(packet.eth->h_dest, fib.dmac, ETH_ALEN);
    __builtin_memcpy(packet.eth->h_source, fib.smac, ETH_ALEN);

    count_packet(STAT_REDIRECT);
    return bpf_redirect(fib.ifindex, 0);
}

char LICENSE[] SEC("license") = "GPL";
