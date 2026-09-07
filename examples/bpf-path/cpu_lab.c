#define _GNU_SOURCE
#include <sched.h>
#include "xdp_common.h"

static int select_cpu(const char *argument, int possible)
{
    cpu_set_t *allowed = CPU_ALLOC(possible);
    size_t size = CPU_ALLOC_SIZE(possible);
    char *end;
    long selected;

    if (!allowed)
        return -1;
    CPU_ZERO_S(size, allowed);
    if (sched_getaffinity(0, size, allowed)) {
        CPU_FREE(allowed);
        return -1;
    }
    if (!strcmp(argument, "auto")) {
        selected = -1;
        for (int cpu = 0; cpu < possible; cpu++) {
            if (CPU_ISSET_S(cpu, size, allowed)) {
                if (selected >= 0) {
                    selected = cpu;
                    break;
                }
                selected = cpu;
            }
        }
    } else {
        errno = 0;
        selected = strtol(argument, &end, 10);
        if (errno || end == argument || *end || selected < 0 || selected >= possible)
            selected = -1;
    }
    if (selected >= 0 && !CPU_ISSET_S(selected, size, allowed))
        selected = -1;
    CPU_FREE(allowed);
    return selected;
}

static int show_stats(int fd, int possible, __u64 *values)
{
    for (__u32 stage = 0; stage < 2; stage++) {
        if (bpf_map_lookup_elem(fd, &stage, values))
            return -1;
        for (int cpu = 0; cpu < possible; cpu++) {
            if (values[cpu])
                printf("stage=%s cpu=%d packets=%llu\n", stage ? "remote" : "ingress",
                       cpu, (unsigned long long)values[cpu]);
        }
    }
    fflush(stdout);
    return 0;
}

int main(int argc, char **argv)
{
    struct bpf_object *object = NULL;
    struct bpf_map *targets;
    struct bpf_cpumap_val target = { .qsize = 2048 };
    __u64 *values = NULL;
    __u32 key = 0, cpu;
    int interface, possible, selected, fd = -1, stats, result = 1;
    bool attached = false;

    if (argc != 4) {
        fprintf(stderr, "usage: %s INPUT CPU|auto OBJECT\n", argv[0]);
        return 2;
    }
    possible = libbpf_num_possible_cpus();
    if (possible < 1 || (selected = select_cpu(argv[2], possible)) < 0) {
        fprintf(stderr, "CPU must be online and allowed by the current CPU affinity\n");
        return 2;
    }
    interface = initialize_lab(argv[1]);
    if (interface < 0)
        return 2;
    cpu = selected;
    object = open_programs(argv[3], "cpu_ingress", "cpu_remote");
    if (!object)
        goto cleanup;
    targets = bpf_object__find_map_by_name(object, "cpu_targets");
    if (!targets || bpf_map__set_max_entries(targets, possible)
        || bpf_object__load(object))
        goto cleanup;
    fd = bpf_program__fd(bpf_object__find_program_by_name(object, "cpu_ingress"));
    target.bpf_prog.fd = bpf_program__fd(bpf_object__find_program_by_name(object, "cpu_remote"));
    if (bpf_map_update_elem(bpf_object__find_map_fd_by_name(object, "cpu_targets"),
                            &cpu, &target, BPF_ANY)
        || bpf_map_update_elem(bpf_object__find_map_fd_by_name(object, "target_cpu"),
                               &key, &cpu, BPF_ANY)) {
        perror("CPUMAP update");
        goto cleanup;
    }
    values = calloc(possible, sizeof(*values));
    stats = bpf_object__find_map_fd_by_name(object, "cpu_stats");
    if (!values || stats < 0 || stopping || attach_xdp(interface, fd, XDP_FLAGS_DRV_MODE))
        goto cleanup;
    attached = true;
    printf("attached cpumap native on %s target_cpu=%u qsize=2048\n", argv[1], cpu);
    fflush(stdout);
    result = 0;
    while (!stopping) {
        if (show_stats(stats, possible, values)) {
            result = 1;
            break;
        }
        sleep(1);
    }
    if (show_stats(stats, possible, values))
        result = 1;
cleanup:
    if (attached && detach_xdp(interface, fd, XDP_FLAGS_DRV_MODE))
        result = 1;
    bpf_object__close(object);
    free(values);
    return result;
}
