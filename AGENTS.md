# AGENTS.md

Project: `kimi-remote` - a single-file Python launcher that exposes
`kimi web` (kimi code's web UI) through Tailscale serve or a Cloudflare
quick tunnel, printing a mobile URL + QR code. A loopback password gate
sits in front of kimi web, so the URL/QR carry no credential.

## Layout

- `kimi_remote.py` - the whole tool. Keep it single-file.
- `setup.ps1` - one-time env installer (uv + tunnel CLIs via winget).
- `start.ps1` - interactive launcher; runs `kimi_remote.py` via `uv run`.
- `pyproject.toml` - only so `uv run` provides `qrcode` for the QR.
- `.plan` - working design notes (gitignored).

## Conventions

- **`kimi_remote.py` stays Python stdlib only.** `qrcode` is optional and
  imported lazily with a graceful fallback (also handles non-UTF-8
  consoles where block glyphs can't encode).
- Windows-first (PowerShell entry points + Git Bash + native Python);
  keep subprocess calls cross-platform anyway (`shutil.which`, no
  shell=True).
- Never add `--dangerous-bypass-auth`; bearer-token auth must stay on.
- The printed URL/QR must never carry the token. Auth lives in the
  loopback gate (`gate_conn`): cookie login with a memorable password
  (`KIMI_REMOTE_PASSWORD` or a startup prompt); the token reaches the
  phone only as a `#token=` fragment in the 302 after a correct password.
  Cookie, not HTTP Basic - kimi's bearer token owns the Authorization
  header and JS can't set headers on WebSocket handshakes.

## Test

No test suite. Validate:

```sh
uv run kimi_remote.py --self-test  # gate flow: 401s, login 302, relay
uv run kimi_remote.py --help
uv run kimi_remote.py --cloudflare   # e2e: curl the printed host: 401
                                     # without cookie, 302 + cookie after
                                     # POST /__login, 200 with cookie
```

Tailscale mode needs `tailscale up` (logged-in daemon) to run end-to-end.
Windows curl needs `--ssl-no-revoke` against trycloudflare.com (schannel).
