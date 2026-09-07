#include <arpa/inet.h>
#include <ifaddrs.h>
#include <poll.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <time.h>
#include <xdp/xsk.h>
#include "xdp_common.h"
#include "echo_reply.h"

#define FRAMES 256
#define FRAME_SIZE 4096
#define BATCH 32

struct rings {
    struct xsk_ring_prod fill, tx;
    struct xsk_ring_cons completion, rx;
    __u64 available[FRAMES];
    unsigned int free_count;
    __u64 received, submitted, completed, rejected;
};

static bool has_address(const char *interface, __be32 address)
{
    struct ifaddrs *list, *entry;
    bool found = false;
    if (getifaddrs(&list))
        return false;
    for (entry = list; entry; entry = entry->ifa_next) {
        if (entry->ifa_addr && entry->ifa_addr->sa_family == AF_INET
            && !strcmp(entry->ifa_name, interface)
            && ((struct sockaddr_in *)entry->ifa_addr)->sin_addr.s_addr == address) {
            found = true;
            break;
        }
    }
    freeifaddrs(list);
    return found;
}

static void refill(struct rings *rings)
{
    __u32 index;
    /* A frame belongs to exactly one of FILL, RX, TX, CQ or this free list. */
    while (rings->free_count && xsk_ring_prod__reserve(&rings->fill, 1, &index) == 1) {
        *xsk_ring_prod__fill_addr(&rings->fill, index) = rings->available[--rings->free_count];
        xsk_ring_prod__submit(&rings->fill, 1);
    }
}

static void reap(struct rings *rings)
{
    __u32 index;
    unsigned int count = xsk_ring_cons__peek(&rings->completion, BATCH, &index);
    for (unsigned int i = 0; i < count; i++) {
        __u64 address = *xsk_ring_cons__comp_addr(&rings->completion, index + i);
        rings->available[rings->free_count++] = address & ~((__u64)FRAME_SIZE - 1);
        rings->completed++;
    }
    xsk_ring_cons__release(&rings->completion, count);
}

static int serve(struct rings *rings, struct xsk_socket *socket, void *area, __be32 local)
{
    struct pollfd pollfd = { .fd = xsk_socket__fd(socket), .events = POLLIN };
    time_t last = 0;

    while (!stopping) {
        __u32 index;
        unsigned int count;

        reap(rings);
        refill(rings);
        count = xsk_ring_cons__peek(&rings->rx, BATCH, &index);
        for (unsigned int i = 0; i < count; i++) {
            const struct xdp_desc *packet = xsk_ring_cons__rx_desc(&rings->rx, index + i);
            __u64 base = packet->addr & ~((__u64)FRAME_SIZE - 1);
            __u32 tx_index;
            size_t length;

            if (packet->addr >= FRAMES * FRAME_SIZE || packet->len > FRAME_SIZE
                || packet->addr + packet->len > base + FRAME_SIZE) {
                fprintf(stderr, "invalid RX descriptor\n");
                return -1;
            }
            rings->received++;
            length = reply_echo(xsk_umem__get_data(area, packet->addr), packet->len, local);
            if (!length || xsk_ring_prod__reserve(&rings->tx, 1, &tx_index) != 1) {
                rings->rejected++;
                rings->available[rings->free_count++] = base;
                continue;
            }
            struct xdp_desc *reply = xsk_ring_prod__tx_desc(&rings->tx, tx_index);
            reply->addr = packet->addr;
            reply->len = length;
            reply->options = 0;
            xsk_ring_prod__submit(&rings->tx, 1);
            rings->submitted++;
        }
        xsk_ring_cons__release(&rings->rx, count);
        if (rings->submitted != rings->completed) {
            if (sendto(pollfd.fd, NULL, 0, MSG_DONTWAIT, NULL, 0) < 0
                && errno != EAGAIN && errno != ENOBUFS && errno != EBUSY && errno != EINTR) {
                perror("AF_XDP TX kick");
                return -1;
            }
        }
        if (time(NULL) != last) {
            last = time(NULL);
            printf("rx=%llu tx=%llu completed=%llu rejected=%llu\n",
                   (unsigned long long)rings->received, (unsigned long long)rings->submitted,
                   (unsigned long long)rings->completed, (unsigned long long)rings->rejected);
            fflush(stdout);
        }
        if (!count && poll(&pollfd, 1, 100) < 0 && errno != EINTR) {
            perror("AF_XDP poll");
            return -1;
        }
    }
    reap(rings);
    printf("rx=%llu tx=%llu completed=%llu rejected=%llu\n",
           (unsigned long long)rings->received, (unsigned long long)rings->submitted,
           (unsigned long long)rings->completed, (unsigned long long)rings->rejected);
    return 0;
}

int main(int argc, char **argv)
{
    struct bpf_object *object = NULL;
    struct xsk_umem *umem = NULL;
    struct xsk_socket *socket = NULL;
    struct rings rings = {};
    struct xsk_umem_config uc = {
        .fill_size = FRAMES, .comp_size = FRAMES, .frame_size = FRAME_SIZE,
    };
    struct xsk_socket_config sc = {
        .rx_size = FRAMES, .tx_size = FRAMES,
        .libbpf_flags = XSK_LIBBPF_FLAGS__INHIBIT_PROG_LOAD,
        .xdp_flags = XDP_FLAGS_SKB_MODE,
        .bind_flags = XDP_COPY | XDP_USE_NEED_WAKEUP,
    };
    struct xdp_options options = {};
    socklen_t options_length = sizeof(options);
    __be32 address;
    __u32 key = 0;
    void *area = MAP_FAILED;
    int interface, fd = -1, socket_fd, error, result = 1;
    bool attached = false;

    if (argc != 4 || inet_pton(AF_INET, argv[2], &address) != 1) {
        fprintf(stderr, "usage: %s INPUT LOCAL_IPV4 OBJECT\n", argv[0]);
        return 2;
    }
    interface = initialize_lab(argv[1]);
    if (interface < 0)
        return 2;
    if (!has_address(argv[1], address)) {
        fprintf(stderr, "LOCAL_IPV4 must be assigned to INPUT in this namespace\n");
        return 2;
    }
    object = open_programs(argv[3], "xsk_ingress", NULL);
    if (!object || bpf_object__load(object))
        goto cleanup;
    fd = bpf_program__fd(bpf_object__find_program_by_name(object, "xsk_ingress"));
    area = mmap(NULL, FRAMES * FRAME_SIZE, PROT_READ | PROT_WRITE,
                MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (area == MAP_FAILED)
        goto cleanup;
    error = xsk_umem__create(&umem, area, FRAMES * FRAME_SIZE,
                             &rings.fill, &rings.completion, &uc);
    if (!error)
        error = xsk_socket__create(&socket, argv[1], 0, umem, &rings.rx, &rings.tx, &sc);
    if (error) {
        fprintf(stderr, "AF_XDP setup: %s\n", strerror(-error));
        goto cleanup;
    }
    socket_fd = xsk_socket__fd(socket);
    if (getsockopt(socket_fd, SOL_XDP, XDP_OPTIONS, &options, &options_length)
        || (options.flags & XDP_OPTIONS_ZEROCOPY)) {
        fprintf(stderr, "could not confirm AF_XDP copy mode\n");
        goto cleanup;
    }
    for (unsigned int i = 0; i < FRAMES; i++)
        rings.available[rings.free_count++] = (__u64)i * FRAME_SIZE;
    refill(&rings);
    if (bpf_map_update_elem(bpf_object__find_map_fd_by_name(object, "sockets"),
                            &key, &socket_fd, BPF_ANY)
        || bpf_map_update_elem(bpf_object__find_map_fd_by_name(object, "local_address"),
                               &key, &address, BPF_ANY)) {
        perror("XSK map update");
        goto cleanup;
    }
    if (stopping || attach_xdp(interface, fd, XDP_FLAGS_SKB_MODE))
        goto cleanup;
    attached = true;
    printf("attached af_xdp generic on %s queue=0 mode=copy zero_copy=no address=%s\n",
           argv[1], argv[2]);
    fflush(stdout);
    result = serve(&rings, socket, area, address) ? 1 : 0;
cleanup:
    if (attached && detach_xdp(interface, fd, XDP_FLAGS_SKB_MODE))
        result = 1;
    xsk_socket__delete(socket);
    if (umem && xsk_umem__delete(umem)) {
        fprintf(stderr, "UMEM cleanup failed\n");
        result = 1;
    }
    bpf_object__close(object);
    if (area != MAP_FAILED)
        munmap(area, FRAMES * FRAME_SIZE);
    return result;
}
