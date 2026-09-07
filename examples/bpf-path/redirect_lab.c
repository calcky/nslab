#include <errno.h>
#include <net/if.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <unistd.h>
#include <linux/if_link.h>
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <bpf/libbpf_version.h>

#if LIBBPF_MAJOR_VERSION == 0 && LIBBPF_MINOR_VERSION < 7
#error "redirect_lab requires libbpf >= 0.7 (for example Ubuntu 24.04 libbpf-dev)"
#endif

static volatile sig_atomic_t stopping;

static void stop(int signum)
{
    (void)signum;
    stopping = 1;
}

static int show_counters(int fd)
{
    const char *names[] = { "seen", "redirected", "map_miss", "fib_fallback", "passed" };
    int cpus = libbpf_num_possible_cpus();
    __u64 *values;

    if (cpus < 1)
        return -1;
    values = calloc(cpus, sizeof(*values));
    if (!values)
        return -1;
    for (__u32 key = 0; key < 5; key++) {
        __u64 total = 0;
        if (bpf_map_lookup_elem(fd, &key, values)) {
            free(values);
            return -1;
        }
        for (int cpu = 0; cpu < cpus; cpu++)
            total += values[cpu];
        printf("%s=%llu%s", names[key], (unsigned long long)total, key == 4 ? "\n" : " ");
    }
    free(values);
    fflush(stdout);
    return 0;
}

int main(int argc, char **argv)
{
    struct bpf_object *object = NULL;
    struct bpf_program *program;
    struct bpf_tc_hook hook = { .sz = sizeof(hook), .attach_point = BPF_TC_INGRESS };
    struct bpf_tc_opts tc = { .sz = sizeof(tc), .handle = 1, .priority = 1 };
    struct bpf_xdp_attach_opts xdp = { .sz = sizeof(xdp) };
    struct rlimit limit = { RLIM_INFINITY, RLIM_INFINITY };
    bool is_tc, empty, attached = false, hook_created = false;
    unsigned int input, output, key = 0;
    int prog_fd = -1, stats_fd, map_fd, settings_fd, result = 1, error;

    if (argc != 5 || (strcmp(argv[1], "devmap") && strcmp(argv[1], "empty") &&
                     strcmp(argv[1], "tc"))) {
        fprintf(stderr, "usage: %s devmap|empty|tc INPUT OUTPUT OBJECT\n", argv[0]);
        return 2;
    }
    is_tc = !strcmp(argv[1], "tc");
    empty = !strcmp(argv[1], "empty");
    input = if_nametoindex(argv[2]);
    output = if_nametoindex(argv[3]);
    if (!input || !output || input == output) {
        fprintf(stderr, "INPUT and OUTPUT must be distinct existing interfaces\n");
        return 2;
    }
    if (geteuid() != 0) {
        fprintf(stderr, "run via sudo nslab exec in the router namespace\n");
        return 2;
    }
    signal(SIGINT, stop);
    signal(SIGTERM, stop);
    /* Older kernels charge BPF memory against RLIMIT_MEMLOCK. */
    (void)setrlimit(RLIMIT_MEMLOCK, &limit);
    object = bpf_object__open_file(argv[4], NULL);
    if (libbpf_get_error(object)) {
        object = NULL;
        goto cleanup;
    }
    bpf_object__for_each_program(program, object) {
        bool selected = !strcmp(bpf_program__name(program),
                                is_tc ? "redirect_tc" : "redirect_devmap");
        bpf_program__set_autoload(program, selected);
    }
    if (bpf_object__load(object))
        goto cleanup;
    program = bpf_object__find_program_by_name(object,
                                               is_tc ? "redirect_tc" : "redirect_devmap");
    if (!program)
        goto cleanup;
    prog_fd = bpf_program__fd(program);
    stats_fd = bpf_object__find_map_fd_by_name(object, "counters");
    map_fd = bpf_object__find_map_fd_by_name(object, "devices");
    settings_fd = bpf_object__find_map_fd_by_name(object, "settings");
    if (stats_fd < 0 || map_fd < 0 || settings_fd < 0)
        goto cleanup;
    if (bpf_map_update_elem(settings_fd, &key, &output, BPF_ANY)) {
        perror("settings update");
        goto cleanup;
    }
    if (!is_tc && !empty && bpf_map_update_elem(map_fd, &output, &output, BPF_ANY)) {
        perror("DEVMAP update");
        goto cleanup;
    }
    if (stopping)
        goto cleanup;
    if (is_tc) {
        hook.ifindex = input;
        error = bpf_tc_hook_create(&hook);
        if (error) {
            fprintf(stderr, "refusing existing/unavailable clsact: %s\n", strerror(-error));
            goto cleanup;
        }
        hook_created = true;
        tc.prog_fd = prog_fd;
        error = bpf_tc_attach(&hook, &tc);
    } else {
        error = bpf_xdp_attach(input, prog_fd,
                               XDP_FLAGS_SKB_MODE | XDP_FLAGS_UPDATE_IF_NOEXIST, NULL);
    }
    if (error) {
        fprintf(stderr, "attach failed (existing hook or unsupported kernel): %s\n",
                strerror(-error));
        goto cleanup;
    }
    attached = true;
    printf("attached %s on %s; output %s; Ctrl+C detaches\n", argv[1], argv[2], argv[3]);
    fflush(stdout);
    result = 0;
    while (!stopping) {
        if (show_counters(stats_fd)) {
            perror("read counters");
            result = 1;
            break;
        }
        sleep(1);
    }
    if (show_counters(stats_fd))
        result = 1;
cleanup:
    if (attached && !is_tc) {
        xdp.old_prog_fd = prog_fd;
        error = bpf_xdp_detach(input, XDP_FLAGS_SKB_MODE, &xdp);
        if (error) {
            fprintf(stderr, "XDP cleanup failed: %s\n", strerror(-error));
            result = 1;
        }
    }
    if (hook_created) {
        hook.attach_point = BPF_TC_INGRESS | BPF_TC_EGRESS;
        error = bpf_tc_hook_destroy(&hook);
        if (error) {
            fprintf(stderr, "clsact cleanup failed: %s\n", strerror(-error));
            result = 1;
        }
    }
    bpf_object__close(object);
    return result;
}
