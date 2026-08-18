# AGENTS.md

Project: `kimi-remote` — a single-file Python launcher that exposes
`kimi web` (kimi code's web UI) through Tailscale serve or a Cloudflare
quick tunnel, printing a token-authenticated mobile URL + QR code.

## Layout

- `kimi_remote.py` — the whole tool. Keep it single-file.
- `.plan` — working design notes (gitignored).

## Conventions

- **Python stdlib only.** No pip dependencies. `qrcode` is optional and
  imported lazily with a graceful fallback.
- Windows-first (Git Bash + native Python); keep subprocess calls
  cross-platform anyway (use `shutil.which`, no shell=True).
- Never add `--dangerous-bypass-auth`; bearer-token auth must stay on.

## Test

No test suite. Validate manually:

```sh
python kimi_remote.py --help
python kimi_remote.py --cloudflare   # e2e: curl the printed host, expect
                                     # 401 without token / 200 with
```

Tailscale mode needs `tailscale up` (logged-in daemon) to run end-to-end.
