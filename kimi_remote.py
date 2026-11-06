#!/usr/bin/env python3
"""One-click mobile web UI for kimi code.

Starts `kimi web`, exposes it through an encrypted tunnel (Tailscale serve
by default, Cloudflare quick tunnel with --cloudflare), and prints a
mobile-ready URL plus a QR code. A tiny loopback auth gate sits between the
tunnel and kimi web: the phone logs in with a memorable password
(KIMI_REMOTE_PASSWORD or a startup prompt) and gets the kimi bearer token
bootstrap only after that - the URL/QR carry no credential.

Stdlib only. The `qrcode` package is used for the QR if importable,
otherwise just the URL is printed.
"""

import argparse
import getpass
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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


def get_password():
    """The phone-side password: env var first, else an interactive prompt.
    Never printed, never in the URL/QR."""
    pw = os.environ.get("KIMI_REMOTE_PASSWORD")
    if pw:
        return pw
    if not sys.stdin.isatty():
        die("set KIMI_REMOTE_PASSWORD (no terminal to prompt for a password)")
    pw = getpass.getpass("choose a password for the phone login: ")
    if not pw:
        die("empty password refused - set KIMI_REMOTE_PASSWORD or type one")
    if getpass.getpass("repeat it: ") != pw:
        die("passwords don't match")
    return pw


# --- auth gate ------------------------------------------------------------------

LOGIN_PATH = b"/__login"
HEAD_CAP = 65536  # bytes; no legit HTTP request head gets near this
BODY_CAP = 8192  # the login form posts one password field

FORM = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>kimi-remote</title>
<body style="font-family:sans-serif;display:grid;place-items:center;min-height:70vh;margin:0">
<form method=post action=/__login style="text-align:center">
<h2>kimi-remote</h2>
<p style="color:#c00;min-height:1.2em">{}</p>
<input type=password name=password placeholder=password autofocus
 autocomplete=current-password style="font-size:1.2em;padding:.4em">
<button style="font-size:1.2em;padding:.4em">unlock</button>
</form>"""


def open_gate(port):
    """Bind in the main thread so a bind failure surfaces immediately,
    not inside a daemon thread."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen()
    return srv


def gate_loop(srv, upstream_port, password, token):
    secret = secrets.token_urlsafe(32)  # per-run cookie value; restart = relogin
    while True:
        conn, _ = srv.accept()
        threading.Thread(
            target=gate_conn,
            args=(conn, upstream_port, password, token, secret),
            daemon=True,
        ).start()


def gate_header(head, name):
    """First value of a request header (name matched case-insensitively)."""
    for line in head.split(b"\r\n")[1:]:
        k, _, v = line.partition(b":")
        if k.strip().lower() == name:
            return v.strip()
    return b""


def gate_send(conn, status, body=b"", headers=()):
    head = (
        f"HTTP/1.1 {status}\r\ncontent-length: {len(body)}\r\nconnection: close\r\n"
        + "".join(f"{k}: {v}\r\n" for k, v in headers)
        + "\r\n"
    )
    conn.sendall(head.encode() + body)


def splice(a, b):
    """Copy a -> b until EOF, then half-close b so the other direction can
    finish draining (WebSocket connections stay open for hours)."""
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def gate_conn(conn, upstream_port, password, token, secret):
    """One gate connection: authenticate, then splice raw bytes to kimi web.

    Auth is a cookie, not HTTP Basic: kimi web's own bearer token lives in
    the Authorization header, and JS cannot set headers on a WebSocket
    handshake - a cookie rides along on both, untouched.
    """
    try:
        conn.settimeout(30)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = conn.recv(4096)
            if not chunk:
                return
            head += chunk
            if len(head) > HEAD_CAP:
                return
        parts = head.split(b"\r\n", 1)[0].split(b" ")
        if len(parts) < 2:
            return
        method, path = parts[0], parts[1].split(b"?", 1)[0]

        authed = False
        for part in gate_header(head, b"cookie").split(b";"):
            k, _, v = part.strip().partition(b"=")
            if k == b"kr_auth":
                authed = bool(v) and hmac.compare_digest(v, secret.encode())
                break

        if method == b"POST" and path == LOGIN_PATH:
            length = int(gate_header(head, b"content-length") or 0)
            if length > BODY_CAP:
                return
            body = head.split(b"\r\n\r\n", 1)[1]
            while len(body) < length:
                chunk = conn.recv(min(4096, length - len(body)))
                if not chunk:
                    return
                body += chunk
            pw = urllib.parse.parse_qs(body.decode("utf-8", "replace")).get(
                "password", [""]
            )[0]
            if hmac.compare_digest(pw.encode(), password.encode()):
                # the only place the token ever leaves the gate: after a
                # correct password, as a client-side fragment the tunnel
                # servers never see
                gate_send(
                    conn,
                    "302 Found",
                    headers=(
                        ("location", f"/#token={token}"),
                        ("set-cookie", f"kr_auth={secret}; Path=/; HttpOnly; Secure; SameSite=Strict"),
                    ),
                )
            else:
                gate_send(
                    conn,
                    "401 Unauthorized",
                    FORM.format("wrong password, try again").encode(),
                    (("content-type", "text/html"),),
                )
            return

        if not authed:
            gate_send(
                conn,
                "401 Unauthorized",
                FORM.format("").encode(),
                (("content-type", "text/html"),),
            )
            return

        up = socket.create_connection(("127.0.0.1", upstream_port))
        up.sendall(head)
        conn.settimeout(None)
        up.settimeout(None)
        t = threading.Thread(target=splice, args=(up, conn), daemon=True)
        t.start()
        splice(conn, up)
        t.join()  # upstream may still be draining its last response
        up.close()
    except (OSError, ValueError):
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


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


def wait_ready(port, proc, timeout=90):
    """Poll kimi web until its HTTP layer answers. A bare TCP accept is not
    enough: the port can open seconds before the routes are mounted, and a
    cold first boot (bundle self-extraction, one-time init) blows past 20s.
    The probe request itself is the warm-up - it drives lazy init to
    completion before the QR is printed."""
    url = f"http://127.0.0.1:{port}/api/v1/meta"
    deadline = time.time() + timeout
    notice_at = time.time() + 3
    while time.time() < deadline:
        if proc.poll() is not None:
            die(f"kimi web exited early (code {proc.returncode})")
        try:
            urllib.request.urlopen(url, timeout=2)
            return  # 2xx: up
        except urllib.error.HTTPError:
            return  # 401: HTTP layer + auth middleware answering = warm
        except Exception:
            if time.time() > notice_at:
                print("kimi web warming up (first run can take a while)...")
                notice_at = float("inf")  # print once
            time.sleep(0.5)
    die(
        f"kimi web not serving on port {port} after {timeout}s"
        " (a cold first boot can be slow - just run again)"
    )


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
    """Spawn kimi web without waiting; call wait_ready() afterwards."""
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
    threading.Thread(
        target=lambda: [None for _ in proc.stderr], daemon=True
    ).start()
    return url, host, proc


# --- self-test ------------------------------------------------------------------


def self_test():
    """Exercise the auth gate against a dummy upstream: no tunnel, no kimi."""
    import http.client
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Up(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"ok"
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    up = HTTPServer(("127.0.0.1", 0), Up)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    gate = open_gate(0)
    threading.Thread(
        target=gate_loop, args=(gate, up.server_port, "pw", "TOK"), daemon=True
    ).start()
    port = gate.getsockname()[1]

    def req(method, path, body=None, headers=()):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, path, body, dict(headers))
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        c.close()
        return out

    status, _, _ = req("GET", "/")
    assert status == 401, status
    status, _, _ = req("GET", "/", headers=(("cookie", "kr_auth=bogus"),))
    assert status == 401, status
    status, _, _ = req("POST", "/api/v1/sessions", "{}")
    assert status == 401, status  # bearer-only API access stays blocked
    status, _, _ = req("POST", "/__login", "password=nope")
    assert status == 401, status
    status, hdrs, _ = req("POST", "/__login", "password=pw")
    assert status == 302, status
    assert hdrs["location"] == "/#token=TOK", hdrs
    cookie = hdrs["set-cookie"].split(";")[0]
    assert cookie.startswith("kr_auth="), cookie
    status, _, body = req("GET", "/", headers=(("cookie", cookie),))
    assert status == 200 and body == b"ok", (status, body)

    up.shutdown()
    print("self-test OK: 401 without/with wrong cookie, API posts blocked,")
    print("login 302s to /#token with a cookie, and requests relay with it.")


# --- main ---------------------------------------------------------------------


def wait_edge(base, timeout=120):
    """Poll the public URL until the tunnel edge actually routes it.

    cloudflared prints the quick-tunnel URL seconds before the edge can
    serve it (observed: up to ~60s), so announcing earlier hands the phone
    a dead link. Any HTTP response - even 401 - means the edge is live.
    Fail-open after timeout: print the URL anyway with a warning.
    """
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
    ap.add_argument("--port", type=int, default=58627, help="gate port (tunnel target)")
    ap.add_argument(
        "--self-test",
        action="store_true",
        help="run the auth-gate self-test (no tunnel, no kimi) and exit",
    )
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return

    kimi = require("kimi")
    token = get_token(kimi)
    password = get_password()
    gate_port = free_port(args.port)
    if gate_port != args.port:
        print(f"port {args.port} is busy, using {gate_port}")
    kimi_port = free_port(gate_port + 1)
    gate = open_gate(gate_port)

    procs = []
    try:
        threading.Thread(
            target=gate_loop,
            args=(gate, kimi_port, password, token),
            daemon=True,
        ).start()
        if args.cloudflare:
            # parallel: kimi web boots (~10s) while cloudflared (~15s)
            # fetches a URL. ".trycloudflare.com" is a suffix-match
            # allowed-host, so kimi web can start before the URL is known.
            web = start_kimi_web(kimi, kimi_port, ".trycloudflare.com")
            procs.append(web)
            base, host, cf_proc = setup_cloudflare(gate_port)
            procs.append(cf_proc)
            wait_ready(kimi_port, web)
        else:
            # parallel: tailscale serve registration does not need the
            # kimi port to be up yet, so both happen at once.
            ts, host = tailscale_dns()
            web = start_kimi_web(kimi, kimi_port, host)
            procs.append(web)
            tailscale_serve(ts, gate_port)
            wait_ready(kimi_port, web)
            base = f"https://{host}"

        if args.cloudflare:
            print("\nNOTE: this URL is PUBLIC. Your password is the only thing")
            print("between it and your shell. Do not share the URL.")
        else:
            print("\ntailnet-only URL. Phone must be on the same tailnet")
            print("(Tailscale app installed and connected).")
        wait_edge(base)
        print_url(base)
        print("  The page asks for the password you set at startup / via")
        print("  KIMI_REMOTE_PASSWORD. The link and QR carry no secret.\n")

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
