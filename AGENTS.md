# AGENTS.md

Project: `kimi-remote` — a single-file Python launcher that exposes
`kimi web` (kimi code's web UI) through Tailscale serve or a Cloudflare
quick tunnel, printing a token-authenticated mobile URL + QR code.

## Layout

- `kimi_remote.py` — the whole tool. Keep it single-file.
- `setup.ps1` — one-time env installer (uv + tunnel CLIs via winget).
- `start.ps1` — interactive launcher; runs `kimi_remote.py` via `uv run`.
- `pyproject.toml` — only so `uv run` provides `qrcode` for the QR.
- `.plan` — working design notes (gitignored).

## Conventions

- **`kimi_remote.py` stays Python stdlib only.** `qrcode` is optional and
  imported lazily with a graceful fallback (also handles non-UTF-8
  consoles where block glyphs can't encode).
- Windows-first (PowerShell entry points + Git Bash + native Python);
  keep subprocess calls cross-platform anyway (`shutil.which`, no
  shell=True).
- Never add `--dangerous-bypass-auth`; bearer-token auth must stay on.

## Test

No test suite. Validate manually:

```sh
uv run kimi_remote.py --help
uv run kimi_remote.py --cloudflare   # e2e: curl the printed host, expect
                                     # 401 without token / 200 with
```

Tailscale mode needs `tailscale up` (logged-in daemon) to run end-to-end.
Windows curl needs `--ssl-no-revoke` against trycloudflare.com (schannel).
