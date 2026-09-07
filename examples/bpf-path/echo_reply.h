#ifndef NSLAB_ECHO_REPLY_H
#define NSLAB_ECHO_REPLY_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

static inline uint16_t checksum(const unsigned char *data, size_t length)
{
    uint32_t sum = 0;
    for (; length > 1; length -= 2, data += 2)
        sum += ((uint16_t)data[0] << 8) | data[1];
    if (length)
        sum += (uint16_t)data[0] << 8;
    while (sum >> 16)
        sum = (sum & 0xffff) + (sum >> 16);
    return (uint16_t)~sum;
}

static inline void set_checksum(unsigned char *data, size_t length, size_t offset)
{
    uint16_t sum;
    data[offset] = data[offset + 1] = 0;
    sum = checksum(data, length);
    data[offset] = sum >> 8;
    data[offset + 1] = sum & 0xff;
}

/* Return zero without modifying malformed/unsupported frames. */
static inline size_t reply_echo(unsigned char *frame, size_t length, uint32_t local)
{
    unsigned char *ip, *icmp;
    unsigned char saved[6];
    size_t ip_length;

    if (length < 42)
        return 0;
    ip = frame + 14;
    icmp = frame + 34;
    if (frame[12] != 8 || frame[13] != 0 || ip[0] != 0x45)
        return 0;
    ip_length = ((size_t)ip[2] << 8) | ip[3];
    if (ip_length < 28 || ip_length > length - 14 || ip[9] != 1 || !ip[8]
        || (ip[6] & 0x3f) || ip[7] || memcmp(ip + 16, &local, 4)
        || icmp[0] != 8 || icmp[1] || checksum(ip, 20) || checksum(icmp, ip_length - 20))
        return 0;
    memcpy(saved, frame, 6);
    memcpy(frame, frame + 6, 6);
    memcpy(frame + 6, saved, 6);
    memcpy(saved, ip + 12, 4);
    memcpy(ip + 12, ip + 16, 4);
    memcpy(ip + 16, saved, 4);
    ip[8] = 64;
    set_checksum(ip, 20, 10);
    icmp[0] = 0;
    set_checksum(icmp, ip_length - 20, 2);
    return ip_length + 14;
}
#endif
