# Zerodha Intraday P&L Monitor

Polls your Zerodha positions every N minutes during market hours and sends a
Telegram alert when total intraday P&L crosses your configured thresholds.
Runs as a hardened systemd service on a DigitalOcean Ubuntu droplet.

---

## Project layout

```
zerodha-pnl-monitor/
├── pnl_monitor.py        # main polling loop (run as a service)
├── generate_token.py     # daily token refresh — run manually each morning
├── requirements.txt
├── .env.example          # copy to .env and fill in secrets
├── pnl-monitor.service   # systemd unit file
└── README.md
```

---

## 1. Prerequisites

### 1a. Kite Connect API app

1. Go to <https://developers.kite.trade/> and log in with your Zerodha credentials.
2. Create a new app. For the **Redirect URL** set `https://127.0.0.1` (used only
   for the manual token flow).
3. Note your **API Key** and **API Secret**.

### 1b. Telegram bot

1. Open Telegram and message **@BotFather**: `/newbot`
2. Follow the prompts — you'll receive a **Bot Token** like
   `7123456789:AAF...`.
3. Start a conversation with your new bot (send any message).
4. Find your **Chat ID**:
   - Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser.
   - Look for `"chat":{"id": 123456789}` in the JSON — that number is your
     Chat ID.

---

## 2. DigitalOcean droplet setup

### 2a. Create the droplet

- **Image:** Ubuntu 24.04 LTS
- **Size:** Basic — $4/mo (1 vCPU, 512 MB RAM) is enough
- **Region:** Bangalore (BLR) reduces latency to Zerodha's servers
- **Authentication:** SSH key (recommended) or password
- **Hostname:** e.g. `pnl-monitor`

### 2b. SSH in and update the system

```bash
ssh root@<DROPLET_IP>
apt update && apt upgrade -y
```

### 2c. Create a dedicated non-root user

```bash
adduser pnlmon --disabled-password --gecos ""
mkdir -p /opt/pnl-monitor
chown pnlmon:pnlmon /opt/pnl-monitor
```

### 2d. Install Python 3 and git

Ubuntu 24.04 ships Python 3.12. Verify it, then install pip and venv:

```bash
python3 --version          # should print 3.12.x
apt install -y python3-pip python3-venv git
```

---

## 3. Deploy the project

### 3a. Copy files to the droplet

**Option A — git clone** (recommended if you push to a private repo):

```bash
# On the droplet, as root or pnlmon:
cd /opt/pnl-monitor
git clone https://github.com/<you>/zerodha-pnl-monitor.git .
```

**Option B — scp from your local machine**:

```bash
# Run on your local machine:
scp -r /path/to/zerodha-pnl-monitor/* root@<DROPLET_IP>:/opt/pnl-monitor/
```

### 3b. Create a virtualenv and install dependencies

```bash
cd /opt/pnl-monitor
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
```

To enable the optional automated TOTP login later:

```bash
venv/bin/pip install pyotp
```

### 3c. Set up the .env file

```bash
cp .env.example .env
nano .env          # fill in all secrets (see .env.example for descriptions)
chmod 600 .env     # only the owner can read it
chown pnlmon:pnlmon .env
```

Key values to set:

| Variable | Where to get it |
|---|---|
| `KITE_API_KEY` | Kite developer console |
| `KITE_API_SECRET` | Kite developer console |
| `TELEGRAM_BOT_TOKEN` | @BotFather |
| `TELEGRAM_CHAT_ID` | getUpdates endpoint |
| `LOSS_THRESHOLD` | e.g. `-5000` |
| `PROFIT_THRESHOLD` | e.g. `10000` |
| `POLL_INTERVAL_MINUTES` | e.g. `5` |

---

## 4. Daily token refresh

Kite access tokens expire at midnight every day. Run this each morning
**before 9:15 AM IST**:

```bash
cd /opt/pnl-monitor
venv/bin/python generate_token.py
```

The script will:
1. Print a Kite login URL — open it in your browser.
2. Log in with your Zerodha ID + password + TOTP (Google Authenticator / any
   TOTP app).
3. You'll be redirected to your redirect URL with `?request_token=...` in the
   URL — copy just the token value.
4. Paste it when prompted.

The token is saved to `.access_token`. The monitor picks it up on the next
poll without needing a restart.

### First run

Run the above before installing the systemd service, so the monitor starts
with a valid token:

```bash
cd /opt/pnl-monitor
venv/bin/python generate_token.py
```

---

## 5. Install the systemd service

```bash
# Copy the unit file
cp /opt/pnl-monitor/pnl-monitor.service /etc/systemd/system/pnl-monitor.service

# Reload systemd, enable at boot, and start now
systemctl daemon-reload
systemctl enable pnl-monitor
systemctl start pnl-monitor

# Verify it's running
systemctl status pnl-monitor
```

### View live logs

```bash
journalctl -u pnl-monitor -f
```

### Other useful commands

```bash
systemctl stop pnl-monitor       # stop the service
systemctl restart pnl-monitor    # restart (e.g. after changing .env)
systemctl disable pnl-monitor    # don't start on reboot
```

---

## 6. Automate the daily token reminder

### Option A — cron reminder (simplest)

This sends you a Telegram message at 8:50 AM IST reminding you to refresh the
token. Add it to root's crontab:

```bash
crontab -e
```

Add this line (adjust the curl to your bot token and chat ID):

```
# Weekdays at 8:50 AM UTC+5:30 = 3:20 AM UTC
20 3 * * 1-5 curl -s "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  -d chat_id=<CHAT_ID> \
  -d text="⏰ Reminder: run generate_token.py before market open"
```

### Option B — systemd timer (cleaner)

Create `/etc/systemd/system/token-reminder.service`:

```ini
[Unit]
Description=Kite token refresh reminder

[Service]
Type=oneshot
User=pnlmon
WorkingDirectory=/opt/pnl-monitor
ExecStart=/usr/bin/curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -d "chat_id=${TELEGRAM_CHAT_ID}" \
  -d "text=⏰ Run generate_token.py before market open"
EnvironmentFile=/opt/pnl-monitor/.env
```

Create `/etc/systemd/system/token-reminder.timer`:

```ini
[Unit]
Description=Daily Kite token reminder (8:50 AM IST = 03:20 UTC)

[Timer]
OnCalendar=Mon..Fri 03:20:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

Enable it:

```bash
systemctl daemon-reload
systemctl enable --now token-reminder.timer
systemctl list-timers token-reminder.timer
```

---

## 7. Optional: fully automated TOTP login

If you'd rather not paste the request_token manually each morning:

1. Install pyotp: `venv/bin/pip install pyotp`
2. Add these to your `.env`:
   ```
   KITE_USER_ID=your_zerodha_id
   KITE_PASSWORD=your_zerodha_password
   KITE_TOTP_SECRET=your_totp_base32_secret
   ```
   The TOTP secret is the base32 string you used to set up Google Authenticator
   (usually shown as a QR code — use a QR decoder to get the text).
3. `generate_token.py` will automatically attempt a headless login before
   falling back to the manual flow.

> **Security note:** storing your password in a file on a server carries risk.
> Keep the `.env` file at `chmod 600` and consider using a secrets manager
> (e.g. HashiCorp Vault, DO Secrets) if this is a concern.

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| Service won't start | `journalctl -u pnl-monitor -n 50` — check for missing `.env` keys |
| "No access token found" | Run `generate_token.py` and restart the service |
| "Token exchange failed" | Your `KITE_API_SECRET` is wrong, or the request_token is >60 s old — try again |
| No Telegram messages | Check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; make sure you messaged the bot first |
| P&L always 0 | You have no open positions today |
| Alerts fire repeatedly | Delete `alert_state.json` — it may have a stale date |

---

## 9. Quick reference — all terminal commands

```bash
# --- First-time setup (run once) ---
ssh root@<DROPLET_IP>
apt update && apt upgrade -y && apt install -y python3-pip python3-venv git
adduser pnlmon --disabled-password --gecos ""
mkdir -p /opt/pnl-monitor && chown pnlmon:pnlmon /opt/pnl-monitor

# Copy project files
cd /opt/pnl-monitor
# (use git clone or scp here)

python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env && chmod 600 .env

# Generate first token
venv/bin/python generate_token.py

# Install and start service
cp pnl-monitor.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable pnl-monitor && systemctl start pnl-monitor

# --- Daily routine ---
cd /opt/pnl-monitor && venv/bin/python generate_token.py
# (no restart needed — monitor picks up the new token on next poll)

# --- Monitoring ---
systemctl status pnl-monitor
journalctl -u pnl-monitor -f
tail -f /opt/pnl-monitor/pnl_monitor.log
```
