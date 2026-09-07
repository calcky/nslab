#include "packet.bpf.h"

struct {
    __uint(type, BPF_MAP_TYPE_CPUMAP);
    __uint(max_entries, 1); /* Resized to possible CPUs before load. */
    __type(key, __u32);
    __type(value, struct bpf_cpumap_val);
} cpu_targets SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u32);
} target_cpu SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 2);
    __type(key, __u32);
    __type(value, __u64);
} cpu_stats SEC(".maps");

SEC("xdp")
int cpu_ingress(struct xdp_md *ctx)
{
    __u32 key = 0;
    __u32 *cpu;
    __u64 *count;

    if (!echo((void *)(long)ctx->data, (void *)(long)ctx->data_end))
        return XDP_PASS;
    count = bpf_map_lookup_elem(&cpu_stats, &key);
    if (count)
        (*count)++;
    cpu = bpf_map_lookup_elem(&target_cpu, &key);
    if (!cpu)
        return XDP_PASS;
    return bpf_redirect_map(&cpu_targets, *cpu, XDP_PASS);
}

SEC("xdp/cpumap")
int cpu_remote(struct xdp_md *ctx)
{
    __u32 key = 1;
    __u64 *count = bpf_map_lookup_elem(&cpu_stats, &key);

    (void)ctx;
    if (count)
        (*count)++;
    /* No MAC or TTL changes: the target CPU resumes the normal stack. */
    return XDP_PASS;
}

char LICENSE[] SEC("license") = "GPL";
