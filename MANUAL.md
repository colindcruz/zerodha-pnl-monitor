# P&L Monitor — User Manual

This manual documents the system as it actually runs today: **`with-websockets/pnl_monitor.py`**, deployed on a DigitalOcean server and controlled by Telegram commands. See the [important note in section 10](#10-file-reference) about an older, simpler script that lives in the same project folder but isn't what's deployed. `README.md` now just points here rather than duplicating setup instructions.

A few terms used throughout, defined once here:

- **API key / access token**: credentials that let a program (this script) act on your Zerodha account on your behalf, the same way a password lets you log into the website. Zerodha's access tokens **expire every night at midnight** and must be regenerated each morning.
- **systemd**: the program built into Linux servers that keeps a background program (a "service") running — starting it on boot, restarting it if it crashes, and giving you commands to start/stop/check it.
- **cron**: a Linux scheduler that runs a command automatically at a fixed time every day (like an alarm clock for scripts).
- **Droplet**: DigitalOcean's name for a small cloud server you rent by the month.
- **F&O / lot / strike / expiry / MIS**: standard NSE options-trading terms — a *lot* is the fixed minimum quantity you can trade for a contract, a *strike* is the price level of an option, *expiry* is the date it lapses, and *MIS* is Zerodha's intraday-only product type (positions must close same day).

---

## 1. Overview

### What it does

The script logs into your Zerodha account and watches your open F&O (futures & options) positions continuously during market hours (9:15 AM – 3:40 PM IST). It calculates your running profit-and-loss (P&L) in real time using a live price feed (a "WebSocket" — a permanently-open connection that pushes price ticks to the script the instant they happen, instead of the script having to repeatedly ask "any change yet?").

It does three broad jobs at once:

1. **Watches and protects your manual trades.** Any position you open yourself gets an automatic stop-loss order placed behind it (based on recent price volatility), profit/loss milestones are announced as they're crossed, and the whole account can be automatically flattened if P&L hits a trailing profit-lock floor, a hard loss limit, or a profit target.
2. **Runs its own automated trade.** Every trading day at 9:23 AM it can automatically sell a NIFTY "short strangle" (explained in section 6) with its own independent stop-loss and a fixed 3:00 PM square-off — entirely separate from your manual positions.
3. **Talks to you.** All of this is reported through a Telegram bot: alerts when something happens, and commands you can send back (`/status`, `/pause`, `/set`, etc.) to check in or intervene without touching the server.

Alerts go to **two channels**: your Telegram chat, and (optionally) [ntfy.sh](https://ntfy.sh) for a phone push notification outside of Telegram. Both can be silenced at once with `/stop`.

### Architecture — what each piece does

| Piece | Role |
|---|---|
| `with-websockets/pnl_monitor.py` | **The live system.** Runs forever as a background service. Connects to the price feed, computes P&L, places/manages stop-loss orders, runs the auto-strangle, sends alerts, and listens for your Telegram commands. |
| `generate_token.py` | A script *you* run each morning (or `auto_token.py` runs for you, see below) to get a fresh Zerodha access token — without one, the monitor can't read your positions. |
| `auto_token.py` | The automation that actually generates the token in production: logs in with your Zerodha ID/password/TOTP, saves the token, and **restarts the monitor service** so it picks it up. Runs every weekday morning at 8:45 AM IST via a system cron entry outside this project (`/etc/cron.d/pnl-token` on the live droplet — see section 5.1). |
| `pnl-monitor.service` | A **systemd unit file** — the instructions that tell the server "run this script as a background service, under this user account, restart it if it crashes." |
| `.env` (you create this from `.env.example`) | Holds every secret and setting: API keys, Telegram credentials, and all the thresholds described in section 4. |

The monitor and the token generator are separate programs on purpose: the monitor keeps running all day reading whatever token is currently saved to disk (`.access_token`), while the token generator's only job is to refresh that file each morning. Restarting the monitor after a fresh token is what actually gets it re-authenticated — that's why `auto_token.py`'s last step is a service restart.

---

## 2. Prerequisites

Confirmed directly from the code's imports and API calls:

| Requirement | Why it's needed |
|---|---|
| **Zerodha Kite Connect API subscription** (paid, via [developers.kite.trade](https://developers.kite.trade/)) | The script uses the `kiteconnect` Python library to read positions, place orders, and stream live prices — this requires a Kite Connect "app" with an API key and secret, separate from your normal Zerodha login. |
| **An active Zerodha trading account** | The API only works against a real account with trading permissions for F&O. |
| **A Telegram account + bot** | All alerts and commands go through a Telegram bot you create via `@BotFather`. |
| **(Optional) An [ntfy.sh](https://ntfy.sh) topic** | For phone push notifications outside Telegram. Free, no account needed — just a topic name. |
| **A Linux server reachable from the internet** | The project's own `pnl-monitor.service` assumes a DigitalOcean "droplet" running Ubuntu, but any always-on Linux machine works the same way. It must stay on and connected 24/7 during market hours. |
| **Python 3.10 or newer** | The code uses modern syntax (e.g. `int | None` type hints) that requires Python 3.10+. |
| **(For fully automated token refresh) Your Zerodha login ID, password, and TOTP secret** | `auto_token.py` logs in on your behalf every morning — see the security note in section 4. |

---

## 3. Installation & setup

These steps assume a fresh Linux server, matching how the live system was actually set up.

1. **Create a Kite Connect app.** Go to [developers.kite.trade](https://developers.kite.trade/), log in, and create a new app. Set the **Redirect URL** to `https://127.0.0.1` (only used for the manual token flow). Note the **API Key** and **API Secret** it gives you.

2. **Create a Telegram bot.** In Telegram, message **@BotFather** with `/newbot` and follow the prompts. You'll get a **bot token** (looks like `7123456789:AAF...`). Then send your new bot any message (so it knows who you are), and visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser — look for `"chat":{"id": 123456789}` to get your **chat ID**.

3. **(Optional) Get an ntfy.sh topic.** Pick any unique word (e.g. `yourname-pnl-alerts`) — no signup required, just remember it.

4. **Provision the server.** Create an Ubuntu server (a $4–6/mo DigitalOcean droplet is enough), SSH in, and install prerequisites:
   ```bash
   apt update && apt upgrade -y
   apt install -y python3-pip python3-venv git
   ```

5. **Create a dedicated, non-root user to run the service** (matches how the live system runs it — as `pnlmon`, not `root`):
   ```bash
   adduser pnlmon --disabled-password --gecos ""
   mkdir -p /opt/pnl-monitor
   chown pnlmon:pnlmon /opt/pnl-monitor
   ```

6. **Copy the project files** to `/opt/pnl-monitor` on the server (via `git clone` or `scp`).

7. **Create a virtual environment and install dependencies:**
   ```bash
   cd /opt/pnl-monitor
   python3 -m venv venv
   venv/bin/pip install --upgrade pip
   venv/bin/pip install -r requirements.txt
   venv/bin/pip install pyotp   # see the flag in section 4 — needed for automated token refresh
   ```

8. **Create your `.env` file:**
   ```bash
   cp .env.example .env
   nano .env          # fill in every value — see the full table in section 4
   chmod 600 .env     # only the file owner can read it (it holds secrets)
   chown pnlmon:pnlmon .env
   ```

9. **Generate your first access token manually** (before starting the service, so it doesn't start with no valid token):
   ```bash
   cd /opt/pnl-monitor
   venv/bin/python generate_token.py
   ```
   Follow the printed login URL, log in, and paste the `request_token` from the redirected URL when prompted (full walkthrough in section 5).

10. **Copy the live script into place.** The systemd service (next step) runs a file literally named `pnl_monitor.py` inside `/opt/pnl-monitor` — you need to put the **`with-websockets`** version there under that name, not the root-level one (they are different programs — see section 10):
    ```bash
    cp with-websockets/pnl_monitor.py /opt/pnl-monitor/pnl_monitor.py
    ```

11. **Install and start the systemd service:**
    ```bash
    cp pnl-monitor.service /etc/systemd/system/pnl-monitor.service
    systemctl daemon-reload
    systemctl enable pnl-monitor   # start automatically on server reboot
    systemctl start pnl-monitor
    systemctl status pnl-monitor   # confirm it's running
    ```

12. **Set up daily token automation** (strongly recommended — see section 7 for why this matters). Either run `auto_token.py` yourself as a manual daily habit, or schedule it so it runs unattended every weekday morning before 9:15 AM. This repo does not include a ready-made cron entry for `auto_token.py` — you'll need to add one yourself. On the live droplet this is done via a system-wide `/etc/cron.d/` entry (root-owned, so it doesn't depend on any one user's crontab), confirmed present and working:
    ```bash
    # /etc/cron.d/pnl-token — requires the droplet's system timezone to actually be
    # Asia/Kolkata (confirmed true on the live server; check yours with `timedatectl`
    # before copying this, or convert to UTC otherwise)
    45 8 * * 1-5 root /opt/pnl-monitor/venv/bin/python /opt/pnl-monitor/auto_token.py >> /opt/pnl-monitor/auto_token.log 2>&1
    ```
    Verified against the live server: this entry exists, and `auto_token.log`'s most recent run completed successfully end to end (login, TOTP, token saved, service restarted).

---

## 4. Configuration reference

Every value below is read directly from `.env` by `with-websockets/pnl_monitor.py` (the live script), and `.env.example` in this repo has been checked to match this table exactly — no dead entries from the older script, nothing missing.

### Secrets & credentials

| Variable | Purpose | Required? | Example |
|---|---|---|---|
| `KITE_API_KEY` | Your Kite Connect app's API key | **Required** | `a1b2c3d4e5f6g7h8` |
| `KITE_API_SECRET` | Your Kite Connect app's API secret (used by `generate_token.py` to exchange a login token for an access token) | **Required** (for token generation) | `x1y2z3...` |
| `KITE_ACCESS_TOKEN` | Fallback access token read from `.env` directly if the `.access_token` file is missing. In normal operation the file always wins — this exists as a backup path. | Optional | *(leave blank)* |
| `ACCESS_TOKEN_PATH` | Where the daily access token is saved/read | Optional | `.access_token` (default) |
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot's token from BotFather | **Required** | `7123456789:AAF...` |
| `TELEGRAM_CHAT_ID` | Your personal Telegram chat ID (only messages from this chat are treated as commands) | **Required** | `123456789` |
| `NTFY_TOPIC` | Your ntfy.sh topic name, for push notifications | Optional (blank disables ntfy) | `yourname-pnl-alerts` |

### Automated token login (`generate_token.py`'s optional headless mode, and `auto_token.py`'s unattended daily run)

| Variable | Purpose | Required? | Example |
|---|---|---|---|
| `ZERODHA_USER_ID` | Zerodha login ID | Optional for `generate_token.py` (falls back to manual paste-the-token flow); **required** for `auto_token.py` (crashes with `KeyError` if unset) | `AB1234` |
| `ZERODHA_PASSWORD` | Zerodha login password | Same as above | — |
| `ZERODHA_TOTP_SECRET` | The base32 secret behind your TOTP (e.g. Google Authenticator) code | Same as above | — |
| `KITE_REDIRECT_URL` | Redirect URL registered on your Kite app | Optional | `https://127.0.0.1` (default) |

Both scripts now read the same three `ZERODHA_*` variable names (this was fixed after an earlier version of this manual flagged a naming mismatch between them) — set them once and both the manual `generate_token.py` shortcut and the unattended daily `auto_token.py` cron job pick them up.

### P&L milestones, trailing lock, green day

| Variable | Purpose | Default | Example |
|---|---|---|---|
| `MILESTONE_STEP` | Send a P&L update every time this many rupees is crossed (e.g. every ₹5,000), until the trailing lock takes over | `5000` | `5000` |
| `TRAIL_ACTIVATION_THRESHOLD` | Once P&L reaches this, milestone alerts stop and a trailing profit-lock arms instead | `40000` | `40000` |
| `GREEN_DAY_ACTIVATION` | Once P&L reaches this (and the trailing lock hasn't armed), a separate, lower "green day" floor arms | `20000` | `20000` |
| `GREEN_DAY_FLOOR` | If armed and P&L falls back to this level, everything is auto-exited | `5000` | `5000` |
| `EXIT_ALERT_REPEAT_SECONDS` | How often to repeat the "still below exit floor" alert while breached | `15` | `15` |
| `TRAIL_CHECK_INTERVAL` | Seconds between each pass of the main monitoring loop | `1` | `1` |
| `HEARTBEAT_INTERVAL_SECONDS` | How often to send a "still alive" heartbeat message, even with no alerts | `3600` (1 hour) | `3600` |

The trailing-lock drawdown itself (how far P&L can fall from its peak before triggering an exit) is **not** a `.env` setting — it's a fixed table in the code (`TRAIL_TIERS`): ₹15k give-back above a ₹1L peak, ₹12k between ₹80k–1L, ₹10k between ₹60k–80k, ₹8k below that. Changing this requires editing the code, not `.env`.

### Auto-exit & protective stop-losses

| Variable | Purpose | Default | Example |
|---|---|---|---|
| `AUTO_EXIT` | Master switch: `true` lets the trailing-lock breach, green-day floor, profit target, and loss limit actually place exit orders; `false` means they only alert you | `true` | `true` |
| `HEDGE_PRICE_THRESHOLD` | Positions priced below this (e.g. cheap far-OTM hedge options) are never auto-exited and don't count toward the position-size limit | `5.0` | `5.0` |
| `EXIT_BUFFER_SECONDS` | Seconds P&L must stay below the trailing floor before auto-exit actually fires (avoids exiting on a one-tick flicker) | `30` | `30` |
| `PROFIT_TARGET` | Exit everything immediately once P&L reaches this | `80000` | `80000` |
| `LOSS_WARNING_1` | Alert-only warning level | `-20000` | `-20000` |
| `LOSS_WARNING_2` | Alert-only "cut size" warning level | `-30000` | `-30000` |
| `LOSS_LIMIT` | Hard shutdown — exit everything immediately once P&L falls to this | `-40000` | `-40000` |
| `COOLOFF_MINUTES` | After a loss-limit, profit-target, or trailing-lock auto-exit, block new manual trades for this many minutes (see section 6) | `15` | `15` |
| `MAX_POSITION_QTY` | Alert (not auto-exit) if any single non-hedge position's quantity exceeds this | `1950` | `1950` |

### VIX-adaptive ATR stop-loss (manual trades only)

Protects every open **manual** position (never the strangle, which has its own separate premium-multiple SL). On every fill, an SL-LIMIT order is (re)placed at `multiplier × 14-period 5-min ATR` from the entry/pyramid-anchor price — the multiplier itself scales with India VIX instead of being fixed:

```
multiplier = ATR_BASE_MULTIPLIER + (current India VIX - ATR_REFERENCE_VIX) * ATR_VIX_SENSITIVITY
```
clamped to `[ATR_MIN_MULTIPLIER, ATR_MAX_MULTIPLIER]`. If the India VIX quote fails to fetch, it falls back to `ATR_BASE_MULTIPLIER` rather than skipping SL placement.

| Variable | Purpose | Default | Example |
|---|---|---|---|
| `ATR_BASE_MULTIPLIER` | Multiplier when VIX equals the reference level | `1.5` | `1.5` |
| `ATR_REFERENCE_VIX` | The VIX level the base multiplier is anchored to | `15` | `15` |
| `ATR_VIX_SENSITIVITY` | How much the multiplier moves per 1-point VIX move away from the reference | `0.1` | `0.1` |
| `ATR_MIN_MULTIPLIER` | Floor on the multiplier, however low VIX gets | `1.5` | `1.5` |
| `ATR_MAX_MULTIPLIER` | Ceiling on the multiplier, however high VIX spikes | `4.0` | `4.0` |

> ⚠️ These five defaults are a reasoning-based starting point, not backtested against real fills — validate against historical data before trusting this with meaningfully larger position size. `test_vix_atr.py` covers the pure formula/clamping logic offline; `test_atr_sl.py` is the live dry-run (no orders placed) that shows the actual VIX/multiplier/ATR numbers for your current positions.

### NIFTY auto-strangle

| Variable | Purpose | Default | Example |
|---|---|---|---|
| `STRANGLE_ENABLED` | Master switch for the whole feature | `true` | `true` |
| `STRANGLE_STRIKE_OFFSET` | NIFTY's strike step in points — used for ATM rounding and as the delta-search ladder's step size, not "how far OTM to sell" (that's now delta-driven, see below) | `50` | `50` |
| `STRANGLE_TARGET_DELTA` | Each side's strike is chosen so its live delta magnitude is close to this, independently per side — recomputed from real option prices every entry, so it self-corrects for that day's actual skew (a fixed points offset does not) | `0.25` | `0.25` |
| `STRANGLE_DELTA_SEARCH_STRIKES` | How many strikes out from ATM to search for the target-delta strike before giving up | `20` | `20` |
| `STRANGLE_SL_MULTIPLIER` | Stop-loss trigger = entry premium × this. E.g. `2.0` means the SL fires if the option's price doubles from what you sold it for | `2.0` | `2.0` |
| `STRANGLE_LOTS` | Number of lots per leg on a normal day | `5` | `5` |
| `STRANGLE_0DTE_LOT_FRACTION` | On a 0DTE day (sold contract expires that same day, margin runs higher), lots = `STRANGLE_LOTS × this`, rounded down, floor of 1 lot | `0.5` | `0.5` |
| `STRANGLE_ENTRY_BUFFER_PCT` | How aggressive the entry/exit limit order price is, as a % off the live price | `0.01` (1%) | `0.01` |
| `STRANGLE_ENTRY_RETRY_SECONDS` | Seconds between entry-order retry attempts if a leg hasn't filled | `20` | `20` |
| `STRANGLE_ENTRY_MAX_RETRIES` | How many times to retry an unfilled leg before giving up | `3` | `3` |
| `STRANGLE_STATE_FILE` | Where today's strangle progress is saved (so a restart doesn't lose track) | `strangle_state.json` | `strangle_state.json` |

The strangle's entry time (9:23 AM), entry cutoff (9:35 AM), and square-off time (3:00 PM) are **fixed in the code**, not configurable via `.env`.

### Weekly NIFTY hedge

Buys a far-OTM long CE+PE pair once a week (the first trading day on/after Wednesday),
held without being touched daily, through that week's Tuesday expiry (cash-settles
automatically — nothing to close). Completely separate from, and doesn't change, the
daily strangle above — see the [Alert behavior](#6-alert-behavior) section for how the
two interact.

| Variable | Purpose | Default | Example |
|---|---|---|---|
| `HEDGE_ENABLED` | Master switch for the whole feature | `true` | `true` |
| `HEDGE_STRIKE_OFFSET` | How far out-of-the-money to buy, in NIFTY strike points — much wider than the strangle's own offset | `1000` | `1000` |
| `HEDGE_LOTS` | Number of lots per leg | `5` | `5` |
| `HEDGE_ENTRY_BUFFER_PCT` | How aggressive the entry/exit limit order price is, as a % off the live price | `0.01` (1%) | `0.01` |
| `HEDGE_ENTRY_RETRY_SECONDS` | Seconds between entry-order retry attempts if a leg hasn't filled | `20` | `20` |
| `HEDGE_ENTRY_MAX_RETRIES` | How many times to retry an unfilled leg before giving up | `3` | `3` |
| `HEDGE_STATE_FILE` | Where the current week's hedge progress is saved (so a restart doesn't lose track) | `hedge_state.json` | `hedge_state.json` |

> ⚠️ `HEDGE_STRIKE_OFFSET`'s default (1000 points) is a reasoning-based starting guess at "far enough to be cheap," not tuned against real fills — actual premium depends on IV and time-to-expiry. Check `/hedge_status` after the first live entry and adjust via `/set hedge_strike_offset` if the premium paid isn't where you want it.

The hedge's entry time (9:18 AM, 5 minutes before the strangle's own entry so the hedge
is already in place when Kite prices that day's short-leg margin) and entry cutoff
(9:30 AM) are **fixed in the code**, not configurable via `.env`.

### Greeks

| Variable | Purpose | Default | Example |
|---|---|---|---|
| `RISK_FREE_RATE` | Used in the Black-Scholes formula behind the `/greeks` command | `0.065` (6.5%) | `0.065` |

### File paths

| Variable | Purpose | Default |
|---|---|---|
| `LOG_FILE` | Where the script writes its log | `pnl_monitor.log` |
| `HISTORY_FILE` | Where daily end-of-day P&L history is saved (read by `/history`) | `pnl_history.json` |

### Pause/mute switches (not `.env` variables — see section 9)

Five behaviors can be toggled by creating or deleting an empty file in the project directory, with no restart needed: `pause_auto_exit`, `pause_sl_orders`, `pause_strangle`, `pause_hedge`, `mute_notifications`. These are normally controlled via Telegram commands (`/pause`, `/pause_sl`, `/pause_strangle`, `/pause_hedge`, `/stop`) rather than touched directly — see section 9.

---

## 5. Running the system

### 5.1 Generating/refreshing the access token

Zerodha access tokens **expire at midnight every night**, so this must happen before market open (9:15 AM) every trading day. There are two ways:

**Manual (`generate_token.py`)** — run this yourself each morning:
1. `cd /opt/pnl-monitor && venv/bin/python generate_token.py`
2. If `ZERODHA_USER_ID`/`ZERODHA_PASSWORD`/`ZERODHA_TOTP_SECRET` are set and `pyotp` is installed, the script tries a fully automated login first and skips straight to step 5 if it works.
3. Otherwise, it prints a login URL. Open it in a browser and log in with your Zerodha credentials + TOTP.
4. After login, you're redirected to your configured Redirect URL with `?request_token=...` in the address bar — copy just that token value.
5. Paste it when the script prompts `Paste the request_token here:`.
6. The new token is saved to `.access_token`. **The running monitor does not need a restart** — it re-reads this file the next time it needs to authenticate.

**Automated (`auto_token.py`)** — runs unattended every weekday at 8:45 AM IST, confirmed live via `/etc/cron.d/pnl-token` on the droplet (`45 8 * * 1-5 root ... auto_token.py`, logging to `/opt/pnl-monitor/auto_token.log`):
1. Logs into Zerodha directly with `ZERODHA_USER_ID` / `ZERODHA_PASSWORD` and a TOTP code generated from `ZERODHA_TOTP_SECRET`.
2. Saves the new token to `.access_token`.
3. **Restarts the `pnl-monitor` service** (`systemctl restart pnl-monitor`) so it comes up fresh with the new token — unlike the manual path, this one does restart the service.
4. Sends a Telegram message: `✅ Token generated & monitor restarted. Ready for 9:15 AM.` on success, or `❌ Auto token generation FAILED: <error>` if anything goes wrong.

> ⚠️ **Security note (from the code's own design):** storing your Zerodha password and TOTP secret in a file on a server is a real risk if that server is ever compromised. Keep `.env` at `chmod 600` and treat the server itself as sensitive.

### 5.2 Start / stop / restart / check status

The service is managed by **systemd**, so standard commands apply:

| Action | Command |
|---|---|
| Start | `systemctl start pnl-monitor` |
| Stop | `systemctl stop pnl-monitor` |
| Restart (e.g. after a config or code change) | `systemctl restart pnl-monitor` |
| Check current status | `systemctl status pnl-monitor` |
| Enable on boot | `systemctl enable pnl-monitor` |
| Disable on boot | `systemctl disable pnl-monitor` |

The service is set to restart itself automatically if it crashes (`Restart=on-failure`, waiting 30 seconds first), and gives itself up to 90 seconds to shut down cleanly when stopped.

### 5.3 Viewing and reading logs

Live tail of the service's logs (via systemd's own log system, `journalctl`):
```bash
journalctl -u pnl-monitor -f
```
Last 50 lines (useful right after a restart):
```bash
journalctl -u pnl-monitor -n 50
```
The script also writes its own log file (path set by `LOG_FILE`, default `pnl_monitor.log`):
```bash
tail -f /opt/pnl-monitor/pnl_monitor.log
```

**Normal startup and idle output looks like this:**
```
2026-08-15 12:08:43,456 [INFO] P&L monitor (WebSocket) started.
2026-08-15 12:08:43,590 [INFO] Telegram command listener started.
2026-08-15 12:08:43,591 [INFO] Market closed. Sleeping 60 s.
```
Once the market opens and it connects to the live price feed, you'd see something like:
```
2026-08-15 09:15:02,101 [INFO] WebSocket connected. Subscribed to 4 instruments.
2026-08-15 09:15:32,204 [INFO] Refreshed positions: 2 open, realized P&L: 0.00
```

**An alert firing looks like this in the logs** (a trailing-lock breach, for example):
```
2026-08-15 13:42:10,553 [INFO] Auto-exit triggered after 30s breach.
2026-08-15 13:42:11,102 [INFO] Exit order: BUY NIFTY24850CE qty=325 @ 145.2 order_id=250815...
```
— and the corresponding Telegram message would read:
```
🚨 Trailing floor breached
P&L Rs 42,350.00 hit floor Rs 40,000.00
Exiting in 30s if not recovered.
```
followed a few seconds later by:
```
🔴 Auto-exit executed
Exited: NIFTY24850CE, NIFTY24700PE
Cool-off: new positions auto-squared-off until 14:12:11
```

---

## 6. Alert behavior

### Manual-position safety net

- Any position you open (that isn't part of the auto-strangle) gets a **stop-loss order automatically placed** behind it, sized at 2× the 14-period Average True Range (a volatility measure, calculated from the last two days of 5-minute candles). This SL is recalculated and resized every time the position changes (new fill, added quantity, partial exit) — both instantly, when the fill notification arrives, and again every 2 minutes as a slower backup check in case the instant path is ever missed.
- **Milestone alerts**: while total P&L is below `TRAIL_ACTIVATION_THRESHOLD`, you get a P&L update every time a `MILESTONE_STEP` boundary (default every ₹5,000) is crossed, no more than once per 60 seconds.
- **Trailing profit-lock**: once P&L reaches `TRAIL_ACTIVATION_THRESHOLD`, milestone alerts stop. The system now tracks the day's P&L *peak* and locks a floor below it (the give-back amount depends on how high the peak is — see the `TRAIL_TIERS` table in section 4). Every new peak raises the floor; if P&L drops back to the floor, a breach alert fires, repeating every `EXIT_ALERT_REPEAT_SECONDS`, and — if `AUTO_EXIT` is on and the breach persists for `EXIT_BUFFER_SECONDS` — everything is exited automatically.
- **Green day floor**: a gentler, earlier safety net that only applies *before* the trailing lock has armed. Arms at `GREEN_DAY_ACTIVATION`, exits everything if P&L falls back to `GREEN_DAY_FLOOR`.
- **Profit target / loss limit**: hard, one-shot exits — `PROFIT_TARGET` and `LOSS_LIMIT` each fire exactly once per day, immediately flattening everything (independent of whether the trailing lock has armed).
- **Loss warnings** (`LOSS_WARNING_1`, `LOSS_WARNING_2`): alert-only, no action taken.
- **Position size limit** (`MAX_POSITION_QTY`): alert-only (repeated every 60s while breaching), no auto-exit.
- **Cool-off** (new): after any of the three "hard" exits above (loss limit, profit target, or a trailing-lock breach exit), a `COOLOFF_MINUTES` window starts. During it, any new manual order that *increases* your exposure (a fresh position, or adding to one) is detected and automatically closed again — both the instant it fills, and via a 2-minute backup check. Reducing an existing position is left alone. This is enforcement *after the fact*, not prevention — Zerodha's API has no way to block you from placing an order in the app itself; the bot can only react once the order is filled.

**All of these one-shot flags (breach/exit "already fired today") reset automatically at the start of each new trading day** — the script re-derives state fresh rather than persisting these particular flags across days.

### Auto-strangle

- At **9:21 AM** (2 minutes before entry), a heads-up alert fires if entry is actually going to be attempted today (same gating as the real entry below — you won't get a heads-up on a paused/holiday/already-done day).
- Between **9:23 AM and 9:35 AM**, if enabled and not already attempted today, the script resolves the nearest NIFTY expiry and, on each side independently, the strike whose live delta magnitude is closest to `STRANGLE_TARGET_DELTA` (falling back to a fixed `STRANGLE_STRIKE_OFFSET`-point offset on 0DTE days, where delta-targeting isn't meaningful at same-day expiry), and sells one call and one put.
- **0DTE days** (the resolved expiry is today's date) write fewer lots — `STRANGLE_LOTS × STRANGLE_0DTE_LOT_FRACTION`, rounded down, floor of 1 — since margin required runs considerably higher that close to expiry. You get an alert when this kicks in. `STRANGLE_0DTE_LOT_FRACTION`'s default (0.5) is a rough starting estimate — check actual margin on the next few 0DTE days and tune it via `/set strangle_0dte_lot_fraction`.
- If a leg's order doesn't fill, it's retried up to `STRANGLE_ENTRY_MAX_RETRIES` times (every `STRANGLE_ENTRY_RETRY_SECONDS`) with a progressively wider limit price. If the 9:35 AM cutoff passes with a leg still unfilled, the system **gives up** and sends an urgent alert — it does **not** fall back to a market order, and does **not** try to fix an imbalance if only one leg filled (e.g. from insufficient margin on the other). That's a deliberate design choice, not a bug — it means asymmetric or incomplete entries need a manual look via `/strangle_status`.
- Each filled leg gets its own stop-loss at `entry price × STRANGLE_SL_MULTIPLIER`.
- At **3:00 PM**, any strangle leg still open is squared off automatically.
- The strangle's P&L, positions, and safety nets are **fully separate** from the manual-trading system above — a strangle leg is never counted in your manual P&L, never swept by the loss-limit/trailing-lock/profit-target exits, and vice versa.

### Weekly hedge

- At **9:16 AM** (2 minutes before entry), a heads-up alert fires if entry is actually going to be attempted this week (same gating as the real entry below).
- On the first trading day **on or after Wednesday** each week (rolling forward automatically if Wednesday's an NSE holiday), if enabled and not already entered this week, the script resolves the nearest NIFTY expiry (naturally the coming Tuesday) and the strikes `HEDGE_STRIKE_OFFSET` points away from spot on each side, and **buys** one call and one put — 5 minutes before the strangle's own daily entry, so the hedge is already in place when Kite prices that day's short-leg margin.
- Same retry/give-up discipline as the strangle: up to `HEDGE_ENTRY_MAX_RETRIES` retries with a widening limit price, no market-order fallback, no auto-correction of an asymmetric fill.
- **No stop-loss and no scheduled exit** — it's a bought option, so the maximum loss is already capped at the premium paid. It's simply held until that week's Tuesday expiry and cash-settles automatically; the bot does nothing to it at expiry. Use `/close_hedge` if you ever want to exit it early.
- Fully separate state from the strangle (`hedge_state.json`, not date-keyed — it deliberately survives every day's restart until the held expiry passes, unlike the strangle's state which resets every morning).
- **Note on terminology**: this literal, bot-bought weekly hedge is a different thing from `HEDGE_PRICE_THRESHOLD` (any position priced below that threshold, historically meant for a hedge you bought yourself outside the bot). The weekly hedge is additionally excluded from every exit mechanism by *symbol*, not just by price — so it stays protected even if its premium later rises above `HEDGE_PRICE_THRESHOLD` during a sharp move (which the price-only mechanism alone would not have caught).

### Final safety-net (3:01–3:06 PM)

A last-resort backstop, independent of everything above — covers **both** manual and strangle positions, unlike every other auto-exit mechanism in this system (which deliberately keep the two separate). The idea is to catch anything that should already be closed by 3 PM (a manual trailing/loss-limit exit, or the strangle's own 3:00 PM square-off) but somehow wasn't, rather than to be a normal part of the daily routine. The weekly hedge is excluded here too (by symbol) — this sweep must never touch it, or the whole point of holding it through the week would break.

- From **3:01 PM**, if any non-hedge position (manual or strangle) is still open, an urgent warning repeats every 30 seconds telling you to close it.
- At **3:06 PM**, everything non-hedge still open — manual or strangle — is squared off automatically, whether or not it should have been closed already.
- This one **ignores `AUTO_EXIT` and the pause files** — it fires regardless, since its entire purpose is to be the backstop for when something else has already failed or been paused. If you deliberately want a position open past 3 PM, this will still close it (except the weekly hedge, which is never touched by this).

### Restart mid-day (a real scenario, not hypothetical)

Systemd restarts the service automatically if it crashes. On every startup, before doing anything else, the script cross-checks its saved strangle state *and* hedge state against your *actual live* Zerodha positions and orders — it never blindly trusts what was last written to disk. This matters even more for the hedge than the strangle, since the hedge state must be correctly recognized across every one of the week's daily restarts, not just a same-day one. If anything is ambiguous after a restart, it alerts you rather than guessing. Note, however, that a restart **does wipe all other in-memory tracking** (the trailing-lock peak/floor, which flags have already fired today, etc., for the manual-trading side) — those are not saved to disk and start over on the next check after restart.

### Edge cases

- **Token expires or is missing mid-day**: the script can't refresh positions and logs a `Failed to refresh positions: Incorrect api_key or access_token` error repeatedly; it does not crash, but it also can't compute P&L or manage risk until a fresh token is in place — see section 8.
- **Weekend / market holiday**: `is_market_open()` checks Saturday/Sunday, the clock (9:15 AM–3:40 PM IST), and now also a hardcoded `NSE_HOLIDAYS` set of 2026 trading holidays (Republic Day, Holi, Diwali-Balipratipada, etc. — 16 dates, cross-checked against two independent sources). The strangle auto-entry check applies the same holiday check directly rather than going through `is_market_open()`. **This list needs a manual update every year** — nothing in the code fetches it automatically, so it will silently stop being accurate once 2027 arrives unless someone updates `NSE_HOLIDAYS` in `with-websockets/pnl_monitor.py`. It also doesn't know about one-off exchange-announced closures (e.g. a special settlement holiday) beyond the fixed list.

---

## 7. Daily operating routine

**What you need to do every trading day:**
1. Confirm the access token was refreshed before 9:15 AM — either by running `generate_token.py` yourself, or by confirming `auto_token.py`'s automated run succeeded (you'll get a Telegram message either way — success or failure).
2. That's it, if everything is running normally. The rest is automatic.

**What runs automatically, with no input from you:**
- The service is already running in the background (systemd keeps it alive, restarting on crash).
- The strangle entry (9:23–9:35 AM) and 3:00 PM square-off.
- All P&L monitoring, milestone/trailing/green-day/loss-limit/profit-target alerts and auto-exits.
- Protective stop-loss placement for manual positions.
- The end-of-day summary, sent automatically the first loop cycle after market close.
- The cool-off enforcement described in section 6.

**Optional, as-needed:**
- Send `/status` any time for a full snapshot.
- Send `/strangle_status` to check today's strangle specifically, or `/hedge_status` for this week's hedge.
- Use `/pause`, `/pause_sl`, `/pause_strangle`, `/pause_hedge`, or `/stop` if you need to temporarily disable something without touching the server (see section 9).

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Service won't start (`systemctl status` shows failed) | Missing required `.env` key, or a Python import error | `journalctl -u pnl-monitor -n 50` — the script raises a clear `KeyError` naming the missing variable if a required one (`KITE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) is absent |
| Log repeats `Failed to refresh positions: Incorrect api_key or access_token` | Access token expired (happens every night at midnight) or was never generated today | Run `generate_token.py` (or check why `auto_token.py`'s scheduled run didn't happen) |
| `auto_token.py` crashes with a `KeyError` for `ZERODHA_USER_ID` (or similar) | `.env` doesn't have the `ZERODHA_*` variables set — a real gap, see section 4 | Add `ZERODHA_USER_ID`, `ZERODHA_PASSWORD`, `ZERODHA_TOTP_SECRET` to `.env` |
| `auto_token.py` crashes with `ModuleNotFoundError: No module named 'pyotp'` | `pyotp` is listed as commented-out/optional in `requirements.txt`, but `auto_token.py` imports it unconditionally — it's actually required for the automated path | `venv/bin/pip install pyotp` |
| No Telegram messages at all | Wrong `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`, or you never messaged the bot first (Telegram won't let a bot message a chat that hasn't messaged it) | Double-check both values; send the bot any message once, then re-check your chat ID via `getUpdates` |
| Telegram commands (e.g. `/status`) get no reply | The command listener only responds to messages from the exact `TELEGRAM_CHAT_ID` configured | Confirm you're messaging from the right Telegram account/chat |
| "Token exchange failed" while running `generate_token.py` | `KITE_API_SECRET` is wrong, or the `request_token` you pasted is more than ~60 seconds old | Re-run the login flow and paste the token immediately |
| P&L always shows 0 | No open positions today (this is expected, not an error) | Open a position — the script re-checks periodically and will pick it up |
| Strangle didn't enter today | `STRANGLE_ENABLED=false`, the `pause_strangle` file exists, `/skip_strangle` was sent, or both legs failed (check for an urgent Telegram alert) | Check `/strangle_status`; remove `pause_strangle` or send `/resume_strangle` if it was paused |
| Hedge didn't enter this week | `HEDGE_ENABLED=false`, the `pause_hedge` file exists, `/skip_hedge` was sent, or both legs failed (check for an urgent Telegram alert) | Check `/hedge_status`; remove `pause_hedge` or send `/resume_hedge` if it was paused |
| Notifications suddenly stopped | `/stop` was sent (or the `mute_notifications` file exists) | Send `/start` to resume |
| SL orders no longer being placed for new positions | `/pause_sl` was sent (or `pause_sl_orders` file exists) | Send `/resume_sl` |

---

## 9. Customization

**Safe to change any time, via Telegram — no restart needed:**

Send `/set` alone to list every live-tunable value with its current setting, or `/set <name> <value>` to change one on the fly, e.g. `/set loss_limit -50000`. The full list of tunable names: `loss_warning_1`, `loss_warning_2`, `loss_limit`, `profit_target`, `trail_activation`, `green_day_activation`, `green_day_floor`, `milestone_step`, `max_position_qty`, `hedge_price_threshold`, `exit_buffer_seconds`, `strangle_sl_multiplier`, `strangle_lots`, `strangle_target_delta`, `strangle_delta_search_strikes`, `cooloff_minutes`.

**Safe to change in `.env`, but requires a `systemctl restart pnl-monitor` to take effect:** everything else listed in section 4's tables (e.g. `TRAIL_CHECK_INTERVAL`, `STRANGLE_STRIKE_OFFSET`, `NTFY_TOPIC`, `MILESTONE_STEP` as a starting default before your first `/set` override, etc.).

**Safe to toggle without any restart, via Telegram commands** (these create/remove a small marker file in the project folder rather than changing `.env`): `/pause` ↔ `/resume` (auto-exit), `/pause_sl` ↔ `/resume_sl` (protective SL placement), `/pause_strangle` ↔ `/resume_strangle` (strangle auto-entry, future days only), `/pause_hedge` ↔ `/resume_hedge` (hedge auto-entry, future weeks only), `/stop` ↔ `/start` (all notifications).

**Do NOT touch without understanding the code first:**
- The strangle's entry/cutoff/square-off **times** (9:23, 9:35, 3:00 PM) and the manual-trading market hours (9:15 AM–3:40 PM) are hardcoded, not `.env` settings — changing them means editing `with-websockets/pnl_monitor.py` directly.
- The `TRAIL_TIERS` drawdown table and the ATR stop-loss multiplier (currently 2×) are also hardcoded — these came out of specific backtesting decisions (see the project's own memory notes) and shouldn't be casually adjusted without re-validating against real trade data.
- `FREEZE_QTY_LIMIT` (1800) reflects NSE's own exchange-level order-size limit for F&O — this is a real exchange rule, not a tunable preference.
- Never edit `strangle_state.json`, `hedge_state.json`, `.access_token`, or `pnl_history.json` by hand while the service is running — they're read and rewritten by the script itself and a manual edit mid-cycle risks state corruption.

---

## 10. File reference

> ⚠️ **Read this first.** This project folder actually contains **two different versions** of the monitor:

| File | What it actually is |
|---|---|
| **`with-websockets/pnl_monitor.py`** | **The real, live system** — everything in this manual describes this file. Uses a live WebSocket price feed, has the trailing lock, green-day floor, cool-off, and the full auto-strangle feature. |
| `pnl_monitor.py` (root folder) | An **older, much simpler predecessor** — polls positions every few minutes over plain REST calls (no live feed), only has a basic loss/profit threshold alert, no strangle, no trailing lock, no Telegram commands. Not what's deployed. |
| `README.md` | Now just a short project intro pointing here — no longer documents the outdated root-level script's setup steps. |

Other files:

| File | Purpose |
|---|---|
| `generate_token.py` | Manual (or semi-automated) daily access-token generator — see section 5.1 |
| `auto_token.py` | Fully automated daily token generator + service restart, meant to run on a schedule — see section 5.1 |
| `pnl-monitor.service` | systemd unit file that runs the live monitor as a background service |
| `requirements.txt` | Python package dependencies |
| `.env.example` | Template for your `.env` file — matches the live script's actual config exactly (see section 4) |
| `.env` | *(you create this)* — your real secrets and settings, never committed to git |
| `.access_token` | *(created automatically)* — today's Zerodha access token |
| `strangle_state.json` | *(created automatically)* — today's auto-strangle progress, survives restarts |
| `hedge_state.json` | *(created automatically)* — the current week's hedge progress, survives every restart through the held expiry |
| `pnl_history.json` | *(created automatically)* — daily end-of-day P&L history, read by `/history` |
| `pnl_monitor.log` | *(created automatically)* — the script's own log file |
| `check_positions.py`, `debug_pnl.py`, `exit_position.py`, `snapshot.py` | Small standalone developer/debugging utilities — not part of the live monitor's runtime, safe to ignore for normal operation |
| `bhavcopy_fetcher.py`, `bhavcopy_cache/` | Historical options-data tooling used for backtesting new strategies — unrelated to live monitoring |
| `test_*.py` | Developer test/dry-run scripts, not run automatically — useful if you're modifying the code yourself |
| `live-dashboard/` | A **separate, independent** read-only live market-data viewer (own process, own venv, own `.env`) — see section 11. Not part of the trading bot. |
| `live-dashboard.service`, `Caddyfile` | systemd unit + reverse-proxy config for deploying the live dashboard — see section 11.3 |

---

## 11. Live dashboard

`live-dashboard/` is a **separate, independent process** — not part of the trading bot,
does not touch `strangle_state.json`/`hedge_state.json`, and cannot place orders. It's a
read-only viewer: live CE/PE option prices with a candle-timeframe selector (1/2/3/5
min) and strike dropdowns, reachable from any browser.

> ⚠️ It reads the **same** `.access_token` the trading bot uses (via
> `ACCESS_TOKEN_PATH`), so it needs `auto_token.py`'s daily refresh to also restart it —
> already wired in (see 11.1). If you deploy this on a box that doesn't run the bot,
> point `ACCESS_TOKEN_PATH` at a token file kept fresh some other way.

### 11.1 What it is

- `live-dashboard/server.py` — an `aiohttp` server: password-protected login (signed
  session cookie), a cached NIFTY-options instrument lookup, a thin proxy to Kite's
  historical-candle API for one-time backfill, and a WebSocket that relays live ticks
  for whichever strikes the browser currently has selected (with real subscribe/
  unsubscribe on strike change — unlike the bot's own ticker, which never unsubscribes).
- `live-dashboard/dashboard.html` — the browser page. All candle bucketing happens
  **client-side**, from 1-minute base candles (backfilled once via `/api/historical`,
  extended live from ticks), merged into the selected timeframe. Bucket boundaries are
  anchored to 09:15 IST using a fixed-offset clock calculation — correct regardless of
  the viewer's own timezone, not just "whatever timezone the browser happens to be in."
- `live-dashboard/login.html` — the login page.
- Binds to `127.0.0.1:8765` only. It is **never** directly reachable from outside the
  droplet — see 11.3 for how it's exposed.

### 11.2 First-time setup

1. `mkdir -p /opt/live-dashboard && cd /opt/live-dashboard`
2. Copy `live-dashboard/server.py`, `dashboard.html`, `login.html`, and
   `requirements.txt` from this repo into that directory (same deploy method you already
   use for `/opt/pnl-monitor` — see section 3).
3. `python3 -m venv venv && venv/bin/pip install -r requirements.txt`
4. Copy `live-dashboard/.env.example` to `/opt/live-dashboard/.env` and fill in:
   - `KITE_API_KEY` — same value as the bot's `.env`.
   - `ACCESS_TOKEN_PATH` — path to the bot's `.access_token` (default assumes
     `/opt/live-dashboard/../.access_token` won't resolve across separate deploy
     directories — set an explicit absolute path, e.g. `/opt/pnl-monitor/.access_token`).
   - `DASHBOARD_PASSWORD` — the login password.
   - `DASHBOARD_SESSION_SECRET` — generate with
     `python3 -c "import secrets; print(secrets.token_hex(32))"`.
5. Install the systemd unit: copy `live-dashboard.service` to
   `/etc/systemd/system/live-dashboard.service`, then
   `systemctl daemon-reload && systemctl enable --now live-dashboard`.
6. Confirm `auto_token.py`'s daily run restarts it too — it already calls
   `systemctl restart live-dashboard` (non-fatal if that service isn't installed on a
   given box) after refreshing the token, right after restarting `pnl-monitor`.

### 11.3 Exposing it over HTTPS (no domain needed)

Since `server.py` only binds `127.0.0.1`, something has to terminate TLS and proxy to
it. This project uses **Caddy** with a free `nip.io` hostname — `nip.io` resolves
`157-245-102-152.nip.io` straight to `157.245.102.152` with zero DNS setup, which is
enough for Caddy to get a real Let's Encrypt certificate (no browser warning, unlike a
self-signed cert):

1. Install Caddy on the droplet (see caddyserver.com's install docs for your distro).
2. Copy `Caddyfile` (repo root) to `/etc/caddy/Caddyfile`.
3. Make sure ports 80 and 443 are open (`ufw allow 80,443/tcp` if using `ufw`) — 80 is
   needed for the ACME HTTP-01 challenge, Caddy handles the HTTP→HTTPS redirect itself.
4. `systemctl restart caddy`.
5. Visit `https://157-245-102-152.nip.io` — you should land on the login page with a
   valid padlock.

If the droplet's IP ever changes, update the hostname in `Caddyfile` to match, and
rotate `DASHBOARD_PASSWORD`/`DASHBOARD_SESSION_SECRET` while you're at it.

### 11.4 Using it

- **Strike dropdowns** default to 7-strikes-OTM (350 points) on each side and re-follow
  the ATM strike automatically as it drifts — until you manually pick a strike, at which
  point your pick sticks (for that browser session) until you change it again or reload
  the page.
- **Candle timeframe** (1/2/3/5 min) is remembered in the browser's `localStorage`
  across reloads.
- The connection badge in the header reads **Live** / **Reconnecting…** — the dashboard
  auto-reconnects with backoff and resubscribes to whatever's currently selected.
- Outside 09:15–15:40 IST, a banner explains the market's closed and prices shown are
  the last available.

### 11.5 Explicitly out of scope

No P&L, no alerting, no order placement — this dashboard only ever reads market data.
If you want live P&L on it too, that's a separate feature to design (it would need to
know your actual entry fills, which this process currently has no access to).

---

*This manual was generated by reading the actual code as of the current commit. If the script changes, re-generate or manually update the sections affected — especially section 4's config table, since that's the part most likely to drift silently.*
