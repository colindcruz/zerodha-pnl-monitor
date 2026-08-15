# Your P&L Monitor — Plain-English Guide

This is a quick-reference guide for day-to-day use. For setup, technical details, or troubleshooting, see `MANUAL.md` instead — this one skips all of that.

---

## 1. What it does

It watches your Zerodha account continuously, about once a second, and sends you a Telegram message whenever something worth knowing happens — a profit or loss milestone, a stop-loss getting placed, or a threshold being crossed. On top of watching, it also runs two things automatically on your behalf: it sells a small NIFTY options trade every trading day around 9:23am and closes it by 3pm, and once a week it buys a cheap "insurance" option that reduces how much margin (money set aside by your broker) the daily trade needs. If your **manual** trading loses or gains more than the limits you've set, it will automatically close those positions for you rather than wait for you to act. Everything else you trade yourself — it just protects those trades with automatic stop-losses and watches your overall P&L.

**Important:** the daily NIFTY trade and the weekly hedge run in a completely separate bucket. The ₹ thresholds in section 2 below only look at your manual trading — the automated trade's own profit/loss never counts toward them, and none of the automatic closes described below ever touch the automated trade or its hedge. It's protected by its own, separate stop-loss instead (see the "Daily NIFTY trade" row).

---

## 2. Current default settings

| Setting (plain words) | Currently set to | What it means in practice |
|---|---|---|
| First loss warning | ₹20,000 down (manual trading only) | You get a warning message. Nothing is closed automatically. |
| Second loss warning | ₹30,000 down (manual trading only) | A stronger warning, suggesting you cut your position size in half. Still nothing closed automatically. |
| Hard loss stop | ₹40,000 down (manual trading only) | All your manual/other positions get closed automatically, right away. The automated NIFTY trade and its hedge are a separate bucket and are never touched by this. |
| Profit target | ₹80,000 up (manual trading only) | All your manual/other positions get closed automatically to lock in the win. The automated NIFTY trade and its hedge are never touched by this. |
| Profit check-in step | Every ₹5,000 | You get an update every time your manual-trading running total crosses another ₹5,000, in either direction, until the "trailing lock" below takes over. |
| Trailing lock starts | Once your manual trading is ₹40,000 up | From here on, it tracks your highest profit point of the day and protects a chunk of it — see section 3 for how much. |
| Early "green day" floor | Arms at ₹20,000 up, floor at ₹5,000 (manual trading only) | A gentler safety net that only applies *before* the trailing lock above has kicked in. If profit reaches ₹20,000 then falls back to ₹5,000, your manual positions close — the automated trade is untouched. |
| Cool-off after a forced exit | 15 minutes | After any of the automatic closes above, if you open a new **manual** position in the next 15 minutes, it gets automatically closed again too. |
| What counts as a "hedge" among your manual trades | Priced under ₹5 | Among your own manual positions, cheap ones are assumed to be insurance, not real bets, so they're never touched by any of the auto-close rules above. (The automated trade's weekly hedge is separate again — see below.) |
| Big-position alert | 1,950 units (~30 lots) in one position | You get a warning if any single position gets this large. Nothing closes automatically — it's just a heads-up. |
| "Still alive" check-in | Every 60 minutes | Even on a totally quiet day, you get a message confirming the monitor is still running. |
| Daily NIFTY trade | On, 5 lots/day (2 lots on expiry day) | Sells a near-the-money call and put automatically each morning around 9:23am, protected by its own stop-loss, closed by 3pm. |
| Weekly protective hedge | On, 5 lots/week | Buys a cheap far-out option once a week (Wednesday) that lowers the margin needed for the daily trade above. Held all week, not touched daily. |

*(There are also a handful of behind-the-scenes technical knobs — retry timing, order buffer percentages, file locations — that don't change what you see or how much is at risk day-to-day, so they're left out of this table on purpose.)*

---

## 3. What happens if you change a setting

- **Loss warnings / hard loss stop** — Lower the ₹ amount and it reacts sooner (safer, but you'll get stopped out of a wobble more easily). Raise it and you're giving yourself more room to be wrong before your manual positions get forced closed. Remember this is measured on manual trading only — it has no effect on the automated NIFTY trade.
- **Profit target** — Lower it and you'll lock in wins earlier but leave more on the table on a good day. Raise it and you're letting winners run longer, at the risk of giving more of it back. Same scope note — manual trading only.
- **Trailing lock start point** — This is the point where it stops sending "every ₹5,000" updates and switches to actively protecting your peak profit. Lowering it means the protection (and the reduced spam) kicks in sooner on a smaller win; raising it means you need a bigger win before it starts guarding your peak.
- **Green day floor** — This only matters early in the day, before the trailing lock has armed. Raising the floor protects more of an early gain; lowering it gives a small early profit more room to develop before anything closes.
- **Cool-off minutes** — Longer means more of a "forced timeout" after a big stop-loss or target hit, before you're allowed to trade freely again. Shorter gets you back to normal trading sooner.
- **What counts as a hedge (₹5 threshold)** — Raise it and more of your cheap positions get ignored by the safety nets (both protected from auto-close, and not counted toward the big-position alert). Lower it and fewer positions get that pass — only genuinely near-worthless ones.
- **Daily NIFTY trade lots** — More lots means more premium collected on a good day, and a proportionally bigger loss on a bad one. This isn't a small tweak — it directly scales your risk on every single trading day.
- **Weekly hedge lots/distance** — More lots on the hedge costs more upfront but caps risk further out; moving it closer to your daily trade's strikes is cheaper margin but a tighter (and pricier) hedge. This one currently isn't fine-tuned against real numbers yet — treat any change here as experimental.

---

## 4. What messages you'll actually receive

**Normal day, nothing unusual:**
```
⏰ Strangle entry in ~2 min
Selling today's 1-OTM CE+PE at 9:23 IST.
```
```
🦅 Strangle entry orders placed
SOLD NIFTY2582624650CE x325 & NIFTY2582624550PE x325
Spot: Rs 24,612.30 | Expiry: 2026-08-26
Awaiting fill confirmation...
```
```
💓 Heartbeat
Monitor alive. Positions: 2 | P&L: Rs 1,240.50
```
```
📅 EOD SUMMARY — 2026-08-15
Final P&L: Rs 3,180.00
Peak: Rs 4,050.00  |  Trough: Rs -620.00
Gave back Rs 870.00 from the day's peak
```

**Loss threshold hit:**
```
⚠️ Loss warning — Rs 20k down
P&L is Rs -21,340.00. Stay cautious.
```
```
🛑 Loss limit Rs -40,000 hit — exited
Exited: BANKNIFTY2582651000PE
Cool-off: new positions auto-squared-off until 11:47:03
```
*(This only ever lists your own manual positions — never the automated NIFTY trade or its hedge, which aren't included in this ₹ total at all.)*

**Profit threshold hit:**
```
🎯 Profit target Rs 80,000 hit — exited
Exited: BANKNIFTY2582651000PE
Cool-off: new positions auto-squared-off until 13:02:15
```

**Something going wrong:**
```
🔑 Access token error
Incorrect `api_key` or `access_token`.
Run generate_token.py and restart the service.
```

One honest gap worth knowing: that token-error message only fires if the connection to your broker fails right when the monitor starts up. If the connection drops in the *middle* of the day instead, it does **not** proactively message you — it just quietly keeps showing you the last numbers it had until the connection comes back. In practice this is rare, since the token gets refreshed fresh every weekday morning automatically.

---

## 5. What you need to do each day

**Nothing, normally.** The daily login/token refresh happens automatically every weekday morning before the market opens, and you'll get a Telegram message either way:
- ✅ success message once it's refreshed and the monitor's restarted with it, or
- ❌ a failure message if it didn't work — this is the one case where you'd need to step in and sort out the login yourself.

Everything else — the daily trade, the weekly hedge, the stop-losses, the safety nets — runs on its own. Your only *optional* daily habit is glancing at Telegram in the morning to confirm you got the ✅, and checking in occasionally during the day if you want a live read (see the questions below for how).

---

## 6. Quick answers to obvious questions

**Does it place trades for me, or just tell me?**
Both, depending on what you mean. The daily NIFTY trade and the weekly hedge are fully automatic — it decides the strikes, places the orders, and manages the stop-loss without you doing anything, and these run completely separately from everything below. For everything *else* you trade yourself, it never opens a new position on its own — it only protects what you've already opened (automatic stop-loss, and closing those manual positions if one of the ₹ thresholds is hit).

**What happens if the morning login step fails — does it just stop silently, or tell me?**
It tells you. You'll get a Telegram message saying the automatic login failed, and at that point you'd need to log in manually yourself before the monitor can do anything that day.

**Does it keep alerting me every few minutes once a threshold is hit, or just once?**
Depends which one. The hard stops (loss limit, profit target, green-day floor) each fire once and take action immediately. The trailing-lock breach is the one exception — if your peak profit gives back too much, it repeats an urgent "still below the floor" alert every 15 seconds until it either recovers or auto-closes, so it can't be missed.

**What happens over the weekend or on a market holiday?**
Nothing trades. No new positions get opened, and you won't get milestone or heartbeat messages either — the monitor just sits quietly until the next real trading day. It knows about specific NSE holidays as well as weekends, so it won't try to trade on those either.

**Can I temporarily switch things off without asking you to touch the server?**
Yes — send `/pause` (stops auto-closing), `/pause_sl` (stops new stop-losses), `/pause_strangle` or `/pause_hedge` (stops tomorrow's/next week's automatic trade), or `/stop` (mutes all messages). Each has a matching `/resume_*` or `/start` to switch it back on. Send `/help` any time to see the full list, or `/status` for a live snapshot right now.
