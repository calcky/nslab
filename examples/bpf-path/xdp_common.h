#ifndef NSLAB_XDP_COMMON_H
#define NSLAB_XDP_COMMON_H

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

static volatile sig_atomic_t stopping;

static void stop_lab(int signum)
{
    (void)signum;
    stopping = 1;
}

static inline int initialize_lab(const char *interface)
{
    struct rlimit limit = { RLIM_INFINITY, RLIM_INFINITY };
    unsigned int index = if_nametoindex(interface);

    if (geteuid() || !index) {
        fprintf(stderr, "requires root and an existing interface in the lab namespace\n");
        return -1;
    }
    signal(SIGINT, stop_lab);
    signal(SIGTERM, stop_lab);
    (void)setrlimit(RLIMIT_MEMLOCK, &limit);
    return index;
}

static inline struct bpf_object *open_programs(const char *path, const char *first,
                                               const char *second)
{
    struct bpf_object *object = bpf_object__open_file(path, NULL);
    struct bpf_program *program;

    if (libbpf_get_error(object))
        return NULL;
    if (!bpf_object__find_program_by_name(object, first)
        || (second && !bpf_object__find_program_by_name(object, second))) {
        fprintf(stderr, "object does not contain the expected lab programs\n");
        bpf_object__close(object);
        return NULL;
    }
    bpf_object__for_each_program(program, object) {
        const char *name = bpf_program__name(program);
        bpf_program__set_autoload(program,
            !strcmp(name, first) || (second && !strcmp(name, second)));
    }
    return object;
}

static inline int attach_xdp(int interface, int fd, __u32 mode)
{
    int error = bpf_xdp_attach(interface, fd, mode | XDP_FLAGS_UPDATE_IF_NOEXIST, NULL);
    if (error)
        fprintf(stderr, "XDP attach refused/unsupported: %s\n", strerror(-error));
    return error;
}

static inline int detach_xdp(int interface, int fd, __u32 mode)
{
    struct bpf_xdp_attach_opts opts = { .sz = sizeof(opts), .old_prog_fd = fd };
    int error = bpf_xdp_detach(interface, mode, &opts);
    if (error)
        fprintf(stderr, "XDP cleanup failed: %s\n", strerror(-error));
    return error;
}
#endif
