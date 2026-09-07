#!/usr/bin/env bash
set -euo pipefail

if (( EUID != 0 )); then
    echo "run this script as root after nslab deploy" >&2
    exit 1
fi
topo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/nslab.yaml"
nslab_bin="${NSLAB_BIN:-nslab}"
name="${NSLAB_NAME:-ipsec}"
command -v openssl >/dev/null
ns() {
    local node="$1"
    shift
    "$nslab_bin" exec --topo "$topo" --name "$name" --node "$node" -- "$@"
}
for node in r1 r2; do
    ns "$node" ip link show dev eth0 >/dev/null
    states=$(ns "$node" ip xfrm state list nokeys)
    policies=$(ns "$node" ip xfrm policy list)
    if [[ -n "$states" || -n "$policies" ]]; then
        echo "$node already has XFRM state/policy; redeploy before running setup again" >&2
        exit 1
    fi
done

umask 077
config_dir=$(mktemp -d /tmp/nslab-ipsec.XXXXXX)
started=()
complete=0
cleanup() {
    local node
    if (( ! complete )); then
        # Only remove the selectors/SPIs owned by this lab in the checked namespaces.
        for node in "${started[@]}"; do
            for direction in in out fwd; do
                ns "$node" ip xfrm policy delete dir "$direction" src 10.90.1.0/24 dst 10.90.2.0/24 >/dev/null 2>&1 || true
                ns "$node" ip xfrm policy delete dir "$direction" src 10.90.2.0/24 dst 10.90.1.0/24 >/dev/null 2>&1 || true
            done
            ns "$node" ip xfrm state delete src 192.0.2.1 dst 192.0.2.2 proto esp spi 0x100 >/dev/null 2>&1 || true
            ns "$node" ip xfrm state delete src 192.0.2.2 dst 192.0.2.1 proto esp spi 0x200 >/dev/null 2>&1 || true
        done
    fi
    unset forward_key reverse_key
    rm -f -- "$config_dir/r1.batch" "$config_dir/r2.batch"
    rmdir -- "$config_dir"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# RFC 4106 AES-128-GCM uses a 16-byte key followed by a 4-byte salt.
forward_key="0x$(openssl rand -hex 20)"
reverse_key="0x$(openssl rand -hex 20)"
for node in r1 r2; do
    {
        printf 'xfrm state add src 192.0.2.1 dst 192.0.2.2 proto esp spi 0x100 reqid 42 mode tunnel replay-window 32 aead rfc4106(gcm(aes)) %s 128\n' "$forward_key"
        printf 'xfrm state add src 192.0.2.2 dst 192.0.2.1 proto esp spi 0x200 reqid 42 mode tunnel replay-window 32 aead rfc4106(gcm(aes)) %s 128\n' "$reverse_key"
        if [[ "$node" == r1 ]]; then
            local_net=10.90.1.0/24
            remote_net=10.90.2.0/24
            local_ip=192.0.2.1
            remote_ip=192.0.2.2
        else
            local_net=10.90.2.0/24
            remote_net=10.90.1.0/24
            local_ip=192.0.2.2
            remote_ip=192.0.2.1
        fi
        printf 'xfrm policy add dir out src %s dst %s tmpl src %s dst %s proto esp reqid 42 mode tunnel level required\n' "$local_net" "$remote_net" "$local_ip" "$remote_ip"
        for direction in in fwd; do
            printf 'xfrm policy add dir %s src %s dst %s tmpl src %s dst %s proto esp reqid 42 mode tunnel level required\n' "$direction" "$remote_net" "$local_net" "$remote_ip" "$local_ip"
        done
    } > "$config_dir/$node.batch"
done
unset forward_key reverse_key
for node in r1 r2; do
    started+=("$node")
    ns "$node" ip -batch "$config_dir/$node.batch"
done
complete=1
echo "IPsec configured: r1 <-> r2 (ESP tunnel, AES-128-GCM)"
