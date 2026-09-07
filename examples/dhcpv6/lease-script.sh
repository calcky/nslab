#!/bin/sh
set -eu

# This dedicated lab client never modifies shared DNS or host configuration.
test "${interface:-}" = eth0
case "${1:-}" in
    bound|renew)
        test -n "${ipv6:-}"
        # dnsmasq.conf advertises equal preferred and valid lifetimes.
        ip -6 addr replace "$ipv6/128" dev "$interface" \
            valid_lft "${lease:-300}" preferred_lft "${lease:-300}"
        ;;
    deconfig)
        ip -6 addr flush dev "$interface" to 2001:db8:60::/64
        ;;
esac
