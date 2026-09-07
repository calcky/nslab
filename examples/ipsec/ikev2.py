#!/usr/bin/env python3
"""Foreground, namespace-isolated strongSwan lab; no host service/config changes."""

import argparse
import contextlib
import os
import secrets
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path


def connection(node: str, secret: str) -> str:
    local, remote = (1, 2) if node == "r1" else (2, 1)
    return f"""connections {{
    site {{
        version = 2
        local_addrs = 192.0.2.{local}
        remote_addrs = 192.0.2.{remote}
        proposals = aes128-sha256-modp2048
        mobike = no
        rekey_time = 1h
        local {{
            auth = psk
            id = r{local}
        }}
        remote {{
            auth = psk
            id = r{remote}
        }}
        children {{
            lan {{
                local_ts = 10.90.{local}.0/24
                remote_ts = 10.90.{remote}.0/24
                mode = tunnel
                esp_proposals = aes128gcm16
                rekey_time = 5m
                life_time = 6m
                start_action = none
            }}
        }}
    }}
}}
secrets {{
    ike-lab {{
        id-1 = r1
        id-2 = r2
        secret = {secret}
    }}
}}
"""


DAEMON_CONFIG = """charon {
    load = random nonce openssl kernel-netlink socket-default vici
    install_routes = no
    filelog {
        stdout {
            default = 1
            flush_line = yes
        }
    }
}
swanctl {
    load = random nonce openssl
}
"""


class Lab:
    def __init__(self, directory: Path):
        self.directory = directory
        self.nslab = os.environ.get("NSLAB_BIN", "nslab")
        self.name = os.environ.get("NSLAB_NAME", "ipsec")
        self.charon = os.environ.get("CHARON_BIN", "/usr/lib/ipsec/charon")
        self.swanctl = os.environ.get("SWANCTL_BIN", "swanctl")
        self.processes = []
        self.logs = []
        self.pidfds = []
        self.started_nodes = []

    def ns(self, node, *args):
        return [
            self.nslab,
            "exec",
            "--topo",
            str(Path(__file__).with_name("nslab.yaml")),
            "--name",
            self.name,
            "--node",
            node,
            "--",
            *args,
        ]

    def command(self, args, **kwargs):
        return subprocess.run(
            args, check=True, text=True, capture_output=True, timeout=30, **kwargs
        ).stdout

    def uri(self, node):
        return f"unix://{self.directory}/{node}/run/charon.vici"

    def control(self, node, *args):
        env = dict(
            os.environ,
            STRONGSWAN_CONF=str(self.directory / "strongswan.conf"),
            SWANCTL_DIR=str(self.directory / node),
        )
        return self.command([self.swanctl, *args, "--uri", self.uri(node)], env=env)

    def start(self):
        for binary in (self.nslab, self.charon, self.swanctl, "unshare", "mount", "ip"):
            if not shutil.which(binary):
                raise RuntimeError(f"missing executable: {binary}")
        if Path("/var/run").resolve() != Path("/run"):
            raise RuntimeError("this lab requires /var/run to resolve to /run")
        for node in ("r1", "r2"):
            for args in (("state", "list", "nokeys"), ("policy", "list")):
                if self.command(self.ns(node, "ip", "xfrm", *args)).strip():
                    raise RuntimeError(f"{node}: XFRM is not empty; redeploy first")
        config = self.directory / "strongswan.conf"
        config.write_text(DAEMON_CONFIG)
        secret = secrets.token_hex(32)
        for node in ("r1", "r2"):
            root = self.directory / node
            (root / "run").mkdir(parents=True, mode=0o700)
            (root / "swanctl.conf").write_text(connection(node, secret))
            log = (root / "charon.log").open("w")
            self.logs.append(log)
            # Private /run isolates the compiled-in PID and VICI paths. Mounts
            # disappear with the child; host strongSwan configuration is unused.
            command = self.ns(
                node,
                "unshare",
                "--mount",
                "--propagation",
                "private",
                "/bin/sh",
                "-ec",
                'mount --bind "$1" /run; unset LD_LIBRARY_PATH; exec "$2"',
                "sh",
                str(root / "run"),
                self.charon,
            )
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                env=dict(os.environ, STRONGSWAN_CONF=str(config)),
            )
            self.processes.append(process)
            self.started_nodes.append(node)
            deadline = time.monotonic() + 15
            while not all((root / "run" / name).exists() for name in ("charon.vici", "charon.pid")):
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError(f"{node}: charon did not become ready")
                time.sleep(0.1)
            pid = int((root / "run/charon.pid").read_text().strip())
            self.pidfds.append(os.pidfd_open(pid))
            print(
                self.control(node, "--load-conns", "--file", str(root / "swanctl.conf")),
                end="",
                flush=True,
            )
            print(
                self.control(node, "--load-creds", "--file", str(root / "swanctl.conf")),
                end="",
                flush=True,
            )
        print(
            self.control("r1", "--initiate", "--child", "lan", "--timeout", "20"),
            end="",
            flush=True,
        )
        print("IKEv2 ready: r1 <-> r2 (PSK, ESP AES-128-GCM)", flush=True)
        print(f"R1_URI={self.uri('r1')}", flush=True)

    def check(self):
        before = self.command(self.ns("r1", "ip", "xfrm", "state", "list", "nokeys"))
        for node, destination in (("h1", "10.90.2.2"), ("h2", "10.90.1.2")):
            print(self.command(self.ns(node, "ping", "-c", "3", "-W", "2", destination)), end="")
        print(self.control("r1", "--rekey", "--child", "lan"), end="")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            after = self.command(self.ns("r1", "ip", "xfrm", "state", "list", "nokeys"))
            if spi_set(after) - spi_set(before):
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("CHILD_SA rekey did not produce a new SPI")
        print(self.command(self.ns("h1", "ping", "-c", "3", "-W", "2", "10.90.2.2")), end="")
        print("IKEv2 check passed: bidirectional ping and CHILD_SA rekey")

    def close(self):
        # Signal the actual daemons first so they remove their SAs and policies.
        # pidfds avoid signaling a reused PID. The wrapper group is a fallback
        # for failures before a daemon created its pidfile/socket.
        for fd in self.pidfds:
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(fd, signal.SIGTERM)
        for process in self.processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
        for fd in self.pidfds:
            os.close(fd)
        for log in self.logs:
            log.close()
        errors = []
        for node in self.started_nodes:
            try:
                for args in (("state", "list", "nokeys"), ("policy", "list")):
                    if self.command(self.ns(node, "ip", "xfrm", *args)).strip():
                        errors.append(
                            f"{node}: XFRM {args[0]} remains; stop processes and redeploy"
                        )
            except (OSError, subprocess.SubprocessError) as error:
                errors.append(f"{node}: could not verify XFRM cleanup: {error}")
        return errors


def spi_set(output):
    words = output.split()
    return {words[index + 1] for index, word in enumerate(words[:-1]) if word == "spi"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="ping, rekey, then stop")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("run as root after nslab deploy")
    os.umask(0o077)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    with tempfile.TemporaryDirectory(prefix="nslab-ikev2-", dir="/tmp") as directory:
        lab = Lab(Path(directory))
        result = 0
        try:
            lab.start()
            if args.check:
                lab.check()
            else:
                print("Keep this terminal open. Ctrl+C stops both daemons.", flush=True)
                while True:
                    if any(process.poll() is not None for process in lab.processes):
                        raise RuntimeError("charon exited unexpectedly")
                    time.sleep(0.5)
        except KeyboardInterrupt:
            print("Stopping IKEv2 lab")
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            print(f"IKEv2 error: {error}")
            if isinstance(error, subprocess.CalledProcessError):
                print(error.stdout, error.stderr)
            result = 1
        finally:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            errors = lab.close()
            if errors:
                print("IKEv2 cleanup incomplete:\n" + "\n".join(errors))
                result = 1
            if result:
                for path in lab.directory.glob("*/charon.log"):
                    print(f"{path.parent.name}:\n{path.read_text()}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
