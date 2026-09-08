# Connection Tracking

```console
deployed topology: conntrack
status: deployed
destroyed topology: conntrack
status: absent
```

Use `examples/conntrack/nslab.yaml` to inspect conntrack state for forwarded traffic on r1.

```bash
sudo nslab graph --format mermaid
```

```mermaid
flowchart LR
    n0["h1\nlinux"]
    n1["r1\nlinux"]
    n2["h2\nlinux"]
    n0 -- "eth0 <-> eth0" --- n1
    n1 -- "eth1 <-> eth0" --- n2
```

```bash
sudo nslab deploy
deployed topology: conntrack
sudo nslab inspect
status: deployed
sudo nslab exec --node r1 -- sysctl -w net.ipv4.conf.all.rp_filter=0
sudo nslab exec --node r1 -- conntrack -F
sudo nslab exec --node h1 -- ping -n -c 1 10.73.2.2
sudo nslab exec --node r1 -- conntrack -L -o extended
sudo nslab exec --node h2 -- sh -c 'nc -u -l 9000 >/dev/null'
sudo nslab exec --node h1 -- sh -c 'printf hello | nc -u -w 1 10.73.2.2 9000'
sudo nslab exec --node r1 -- conntrack -L -p udp -o extended
sudo nslab exec --node h2 -- sh -c 'nc -l 9000 >/dev/null'
sudo nslab exec --node h1 -- sh -c 'printf hello | nc -w 2 10.73.2.2 9000'
sudo nslab exec --node r1 -- conntrack -L -p tcp -o extended
sudo nslab exec --node r1 -- conntrack -E
sudo nslab exec --node r1 -- conntrack -F
sudo nslab destroy
destroyed topology: conntrack
sudo nslab inspect
status: absent
```

ICMP has no ports, so correlate protocol, addresses, ICMP id, and `ASSURED`. One-way UDP commonly remains `UNREPLIED`; bidirectional traffic adds the reply tuple. Repeat `conntrack -L -p tcp -o extended` during a TCP handshake and close to observe `SYN_SENT`, `SYN_RECV`, `ESTABLISHED`, and expiry. Run `conntrack -E` for an event stream. Clean up with `sudo nslab destroy`.
