#!/usr/bin/env bash
set -euo pipefail

if (( EUID != 0 )); then
    echo "run this script as root after nslab deploy" >&2
    exit 1
fi
topo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/nslab.yaml"
nslab_bin="${NSLAB_BIN:-nslab}"
name="${NSLAB_NAME:-wireguard}"
command -v wg >/dev/null
ns() {
    local node="$1"
    shift
    "$nslab_bin" exec --topo "$topo" --name "$name" --node "$node" -- "$@"
}
for node in r1 r2; do
    ns "$node" ip link show dev eth0 >/dev/null
    if ns "$node" ip link show dev wg0 >/dev/null 2>&1; then
        echo "$node already has wg0; redeploy before running setup again" >&2
        exit 1
    fi
done

umask 077
key_dir=$(mktemp -d /tmp/nslab-wireguard.XXXXXX)
created=()
complete=0
cleanup() {
    local node
    if (( ! complete )); then
        for node in "${created[@]}"; do
            ns "$node" ip link delete wg0 || true
        done
    fi
    rm -f -- "$key_dir/r1.key" "$key_dir/r2.key"
    rmdir -- "$key_dir"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
wg genkey > "$key_dir/r1.key"
wg genkey > "$key_dir/r2.key"
r1_public=$(wg pubkey < "$key_dir/r1.key")
r2_public=$(wg pubkey < "$key_dir/r2.key")

for node in r1 r2; do
    ns "$node" ip link add wg0 type wireguard
    created+=("$node")
    ns "$node" wg set wg0 listen-port 51820 private-key "$key_dir/$node.key"
    ns "$node" ip link set dev wg0 mtu 1420 up
done
ns r1 wg set wg0 peer "$r2_public" allowed-ips 10.80.0.2/32,fd80::2/128 endpoint 192.0.2.2:51820
ns r2 wg set wg0 peer "$r1_public" allowed-ips 10.80.0.1/32,fd80::1/128 endpoint 192.0.2.1:51820
ns r1 ip addr add 10.80.0.1/32 dev wg0
ns r2 ip addr add 10.80.0.2/32 dev wg0
ns r1 ip -6 addr add fd80::1/128 dev wg0
ns r2 ip -6 addr add fd80::2/128 dev wg0
ns r1 ip route add 10.80.0.2/32 dev wg0
ns r2 ip route add 10.80.0.1/32 dev wg0
ns r1 ip -6 route add fd80::2/128 dev wg0
ns r2 ip -6 route add fd80::1/128 dev wg0
complete=1
echo "WireGuard configured: r1 <-> r2 (IPv4 + IPv6)"
