# Polydesk

A tiny **always-on-top** glance for your Polymarket proxy: position value and
whether you are **HOLDING** or **FLAT**. Same shape as a Pomodoro timer widget
(small square, circular plate, corner of the screen) — no tomato art, no orders.

Runs on **your laptop**, not the trading VM. Public Data API only. No private key.

This is **not** idle CLOB cash. `/value` is mark-to-market of holdings.

Replace `0xYourProxyWallet` with your real Polymarket **proxy** address
(`FUNDER_ADDRESS` on the VM — the `0x` in your Polymarket profile URL, not a
private key).

---

## Windows (PowerShell)

Do **not** start in `C:\WINDOWS\system32`. The repo is not there, and
`.venv/bin/python` is a Linux path.

**Easiest — no Python:** copy `widget\index.html` to the PC (or clone the repo),
then double-click it. Click **set address**, paste your `0x…` proxy. To make it
feel like an app:

```powershell
cd $HOME\Downloads   # or wherever index.html lives
start msedge --app="$PWD\index.html" --window-size=240,300
```

**Desktop widget (always-on-top):** clone the repo somewhere you own, then:

```powershell
cd $HOME\Documents
git clone https://github.com/joelntemuse24/poly-money-maker.git
cd poly-money-maker
git checkout cursor/balance-widget-4f20   # until this is on main

python -m pip install requests python-dotenv
python widget\polydesk.py --address 0xYOUR_REAL_PROXY
```

If you already have a venv on Windows it is `.\.venv\Scripts\python.exe`, not
`.venv/bin/python`.

```powershell
.\.venv\Scripts\python.exe widget\polydesk.py --address 0xYOUR_REAL_PROXY
```

Need [Python 3 from python.org](https://www.python.org/downloads/) with **tcl/tk**
checked. Drag the window to a corner; Esc quits; double-click to set address.

---

## macOS / Linux

```bash
cd /path/to/poly-money-maker
python3 widget/polydesk.py --address 0xYOUR_REAL_PROXY
# repo venv on Linux/mac:
.venv/bin/python widget/polydesk.py --address 0xYOUR_REAL_PROXY
```

Debian/Ubuntu if tkinter is missing: `sudo apt install python3-tk`

---

## Flags

- `--once` — print one snapshot, no window
- `--address 0x…` — proxy / `FUNDER_ADDRESS`
- Also reads `FUNDER_ADDRESS` from the environment or repo `.env`, then
  `widget/.address` (gitignored)
