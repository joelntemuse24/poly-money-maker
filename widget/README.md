# Polydesk

A tiny **always-on-top** glance for your Polymarket proxy: position value and
whether you are **HOLDING** or **FLAT**. Same shape as a Pomodoro timer widget
(small square, circular plate, corner of the screen) — no tomato art, no orders.

Runs on **your laptop**, not the trading VM. Public Data API only (the same
`/value` + `/positions` the site uses). No private key.

This is **not** idle CLOB cash. `/value` is mark-to-market of open (and
claimable) positions. Cash sitting in the exchange needs CLOB auth; this widget
does not touch that.

## Desktop (recommended)

Needs Python 3 + `requests` (repo `.venv` already has it) + tkinter.

```bash
cd poly-money-maker
.venv/bin/python widget/polydesk.py
# or
.venv/bin/python widget/polydesk.py --address 0xYourProxyWallet
```

Address resolution, in order: `--address`, `FUNDER_ADDRESS` in the environment
or repo `.env`, then `widget/.address` (gitignored).

- Drag anywhere to a corner
- Stays on top
- Esc or right-click → Quit
- Double-click / right-click → set address
- `--once` prints one snapshot (no window; good on a headless box)

Debian/Ubuntu if tkinter is missing: `sudo apt install python3-tk`

## Browser

If you would rather pin a tiny Chrome/Edge window:

```bash
open widget/index.html
# or: google-chrome --app=file:///…/widget/index.html --window-size=240,300
```

Paste the proxy address once (saved in localStorage). Same 6s poll.

## Chrome app-mode (closest to a Pomodoro window)

```bash
google-chrome --app="file://$PWD/widget/index.html" --window-size=240,300
```

Then: Window → (your OS) keep on top, if the browser supports it. The Python
widget does always-on-top itself.
