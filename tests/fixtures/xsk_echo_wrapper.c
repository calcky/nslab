#include "echo_reply.h"

size_t make_reply(unsigned char *frame, size_t length, uint32_t address)
{
    return reply_echo(frame, length, address);
}
