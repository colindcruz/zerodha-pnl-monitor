# Zerodha Intraday P&L Monitor

Watches your Zerodha positions in real time, sends Telegram alerts on P&L
thresholds and trailing-stop/green-day/loss-limit events, runs an automated
NIFTY short-strangle strategy with its own risk controls, and offers a set of
Telegram commands (`/status`, `/set`, `/strangle_status`, etc.) for day-to-day
control. Runs as a hardened `systemd` service on a DigitalOcean Ubuntu droplet.

**The live, deployed script is `with-websockets/pnl_monitor.py`** (confirmed
by `pnl-monitor.service`'s `ExecStart`). The root-level `pnl_monitor.py` is an
older, simpler REST-polling-only predecessor kept for reference — it is not
what runs in production.

**→ See [`MANUAL.md`](MANUAL.md) for the full setup, configuration, operation,
and troubleshooting guide.** That's the maintained, authoritative reference —
this file is intentionally just a pointer, so there's only one document to
keep in sync with the code as it changes.

## Project layout (top level)

```
zerodha-pnl-monitor/
├── with-websockets/pnl_monitor.py   # the live monitor (see MANUAL.md)
├── pnl_monitor.py                   # older predecessor, not deployed
├── generate_token.py                # manual/on-demand daily token refresh
├── auto_token.py                    # unattended daily token refresh (runs via cron on the droplet)
├── requirements.txt
├── .env.example                     # copy to .env and fill in secrets
├── pnl-monitor.service              # systemd unit file
├── MANUAL.md                        # full user manual — start here
└── README.md                        # this file
```
