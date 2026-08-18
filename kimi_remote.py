#!/usr/bin/env python3
"""One-click mobile web UI for kimi code.

Starts `kimi web`, exposes it through an encrypted tunnel (Tailscale serve
by default, Cloudflare quick tunnel with --cloudflare), and prints a
mobile-ready URL with the bearer token embedded plus a QR code.

Stdlib only. The `qrcode` package is used for the QR if importable,
otherwise just the URL is printed.
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

KIMI_HOME = Path(os.environ.get("KIMI_CODE_HOME", Path.home() / ".kimi-code"))
TOKEN_FILE = KIMI_HOME / "server.token"
CF_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def require(name):
    path = shutil.which(name)
    if not path:
        die(f"'{name}' not found on PATH")
    return path


def get_token(kimi):
    if not TOKEN_FILE.exists():
        print("no server token yet, generating one via `kimi web rotate-token` ...")
        subprocess.run([kimi, "web", "rotate-token"], check=True)
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        die(f"token file {TOKEN_FILE} is empty")
    return token


def free_port(start):
    """First free loopback port >= start. kimi web silently bumps a busy
    port, which would break the tunnel mapping - so we pick upfront."""
    port = start
    while True:
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", port))
            return port
        except OSError:
            port += 1
            if port > start + 20:
                die(f"no free port in {start}-{start + 20}")


def wait_port(port, proc, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            die(f"kimi web exited early (code {proc.returncode})")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.3)
    die(f"kimi web did not open port {port} within {timeout}s")


def print_url(url):
    print("\n  Open this on your phone:\n")
    print(f"    {url}\n")
    try:
        import qrcode
    except ImportError:
        print("  (install `qrcode` for a scannable QR code; URL above works too)")
        print()
        return
    try:
        # block glyphs need a utf-8 console (GBK codepages would crash)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    try:
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except UnicodeEncodeError:
        print("  (console can't render the QR code; use the URL above)")
    print()


def start_kimi_web(kimi, port, allowed_host):
    """Spawn kimi web without waiting; call wait_port() afterwards."""
    cmd = [kimi, "web", "--no-open", "--port", str(port)]
    if allowed_host:
        cmd += ["--allowed-host", allowed_host]
    print("+", " ".join(cmd))
    return subprocess.Popen(cmd)


# --- transports ---------------------------------------------------------------


def tailscale_dns():
    """Return this node's ts.net DNS name; dies if tailscale is not up."""
    ts = require("tailscale")
    out = subprocess.run(
        [ts, "status", "--json"], capture_output=True, text=True
    )
    if out.returncode != 0:
        die(f"tailscale status failed: {out.stderr.strip()}")
    status = json.loads(out.stdout)
    if status.get("BackendState") != "Running":
        die(
            "tailscale is not running. Run `tailscale up` once (browser login),\n"
            "then re-run this script."
        )
    return ts, status["Self"]["DNSName"].rstrip(".")


def tailscale_serve(ts, port):
    subprocess.run(
        [ts, "serve", "--bg", "--https=443", f"http://127.0.0.1:{port}"],
        check=True,
    )


def teardown_tailscale():
    ts = shutil.which("tailscale")
    if ts:
        subprocess.run([ts, "serve", "--https=443", "off"], capture_output=True)


def setup_cloudflare(port):
    """Return (base_url, host, proc). URL is parsed from cloudflared output."""
    cf = require("cloudflared")
    proc = subprocess.Popen(
        [cf, "tunnel", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 30
    url = None
    assert proc.stderr is not None
    while time.time() < deadline:
        line = proc.stderr.readline()
        if not line:
            break
        m = CF_URL_RE.search(line)
        if m:
            # api.trycloudflare.com is cloudflared's control plane, not
            # the tunnel URL - keep reading
            if m.group(0) == "https://api.trycloudflare.com":
                continue
            url = m.group(0)
            break
    if not url:
        proc.kill()
        die("could not get a trycloudflare URL from cloudflared")
    host = url.removeprefix("https://")
    # keep draining stderr so the pipe never fills up
    import threading

    threading.Thread(
        target=lambda: [None for _ in proc.stderr], daemon=True
    ).start()
    return url, host, proc


# --- main ---------------------------------------------------------------------


def wait_edge(base, timeout=120):
    """Poll the public URL until the tunnel edge actually routes it.

    cloudflared prints the quick-tunnel URL seconds before the edge can
    serve it (observed: up to ~60s), so announcing earlier hands the phone
    a dead link. Any HTTP response - even 401 - means the edge is live.
    Fail-open after timeout: print the URL anyway with a warning.
    """
    import urllib.error
    import urllib.request

    print("waiting for the tunnel edge to come online", end="", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/", timeout=10)
            break  # 2xx: live
        except urllib.error.HTTPError:
            break  # 401/403: edge reachable, auth/host layer answered
        except Exception:
            print(".", end="", flush=True)
            time.sleep(3)
    else:
        print("\nwarning: tunnel not responding yet - the link may need")
        print("another minute before it works on your phone.")
        return
    print(" live!")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--cloudflare",
        action="store_true",
        help="use a cloudflare quick tunnel (public URL) instead of tailscale",
    )
    ap.add_argument("--port", type=int, default=58627, help="kimi web bind port")
    args = ap.parse_args()

    kimi = require("kimi")
    token = get_token(kimi)
    port = free_port(args.port)
    if port != args.port:
        print(f"port {args.port} is busy, using {port}")

    procs = []
    try:
        if args.cloudflare:
            # parallel: kimi web boots (~10s) while cloudflared (~15s)
            # fetches a URL. ".trycloudflare.com" is a suffix-match
            # allowed-host, so kimi web can start before the URL is known.
            web = start_kimi_web(kimi, port, ".trycloudflare.com")
            procs.append(web)
            base, host, cf_proc = setup_cloudflare(port)
            procs.append(cf_proc)
            wait_port(port, web)
        else:
            # parallel: tailscale serve registration does not need the
            # kimi port to be up yet, so both happen at once.
            ts, host = tailscale_dns()
            web = start_kimi_web(kimi, port, host)
            procs.append(web)
            tailscale_serve(ts, port)
            wait_port(port, web)
            base = f"https://{host}"

        if args.cloudflare:
            print("\nNOTE: this URL is PUBLIC. Anyone with it + the token gets")
            print("full access to your sessions and shell. Do not share it.")
        else:
            print("\ntailnet-only URL. Phone must be on the same tailnet")
            print("(Tailscale app installed and connected).")
        wait_edge(base)
        print_url(f"{base}/#token={token}")
        # some QR scanners strip the #fragment - offer a manual fallback
        print(f"  If the scanned link misses '#token=...', paste this")
        print(f"  token on the login page instead:\n\n    {token}\n")

        print("serving... Ctrl-C to stop.")
        while True:
            time.sleep(1)
            for p in procs:
                if p.poll() is not None:
                    die(f"a subprocess exited unexpectedly (code {p.returncode})")
    except KeyboardInterrupt:
        print("\nshutting down...")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        if not args.cloudflare:
            teardown_tailscale()
        print("stopped.")


if __name__ == "__main__":
    main()
