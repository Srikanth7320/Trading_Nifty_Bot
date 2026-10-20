# NiftyBot - NSE paper-trading signal bot (rebuilt)

Scans the NSE EQ universe, scores each stock 0-100, opens **paper** positions on clean breakouts, manages exits, and tells you
(Telegram + dashboard + log files) exactly what happened in every scan. No real orders are ever sent.

## What changed vs. the old version
| Area | Before | Now |
|---|---|---|
| Signals | ~15 hard gates incl. a manual CSV that was empty -> nothing could ever pass | **Scored (0-100)** with 5 must-have gates; manual CSV is optional and only vetoes known-bad stocks |
| Visibility | `print()` only | `data/scan_runs.csv`, `data/scan_details.csv`, `data/scan_journal.txt`, dashboard **Scans** + **Journal** pages, Telegram summaries |
| Holidays/after hours | silent | 08:00 morning message (holiday notice), "Scan now" = analysis-only when closed, end-of-day summary |
| Access | localhost + SSH tunnel only, no login | nginx on port 80/443, **password login**, CSRF, lockout, works on the VM's external IP |
| Data | old trades included | **clean ledgers** (headers only) |
| Help | - | Help page, `/help` `/why SYMBOL` `/watchlist` `/today` ... in Telegram, plain-English "what is the bot doing" banner |

## Deploy on your GCP VM (Ubuntu/Debian)
1. Upload and unzip (example): `gcloud compute scp NiftyBot.zip INSTANCE:~ --zone ZONE`, then on the VM `unzip NiftyBot.zip -d ~/ && cd ~/NiftyBot`
2. `bash deploy/setup_gcp.sh` - installs everything, asks for a dashboard password, starts `niftybot-web` + `niftybot-worker` + nginx.
   (It stops/replaces the old services of the same names. Your old folder is untouched.)
3. From **Cloud Shell or your PC** (not the VM): `bash deploy/gcp_static_ip_and_firewall.sh INSTANCE ZONE [YOUR.IP/32]`
   - opens tcp:80/443 for this VM only and **reserves the VM's current external IP** so it never changes.
4. Open `http://EXTERNAL_IP/` (or `https://EXTERNAL_IP/` - self-signed certificate, the browser warns once but the password is encrypted).
5. Edit `.env` (Upstox token, Telegram token/chat id...) then `sudo systemctl restart niftybot-web niftybot-worker`.

### Keeping the same IP if you change the VM
A normal external IP changes whenever the VM is stopped/recreated - only a **reserved static address** stays. Step 3 does that
(a reserved address attached to a running VM costs the same as an ordinary external IP; an unattached one is billed higher - see GCP pricing).
To move it to another VM **in the same region**: `bash deploy/gcp_move_ip_to_new_vm.sh NEW_INSTANCE NEW_ZONE`,
then run `deploy/setup_gcp.sh` on the new VM and copy the old `.env` and `data/` folder across.

## Daily behaviour (IST)
- 08:00 morning check (data source health + holiday notice) -> Telegram. 09:15-15:30 scan every 15 min (a rotating batch of 400 stocks; whole list ~every 2 hours), positions checked every 5 min.
- New entries only 10:00-14:30. After 15:45 an end-of-day summary. Weekends/holidays: nothing runs, except analysis-only on request.

## Where to see what happened
- Dashboard **Scans** -> click a scan -> every stock, result and reason. **Journal** -> narrative. **Watchlist** -> near-misses. `/symbol/TCS` -> history of one stock.
- Files in `data/`: `scan_runs.csv` (one row per scan), `scan_details.csv` (one row per stock per scan), `scan_journal.txt`, ledgers. See `data/README.txt`.
- Telegram: `/status /positions /watchlist /today /why SYMBOL /scan /pause /resume /performance /health /help`.

## Strategy in one paragraph
Score = trend 15 + EMA momentum 10 + long-term trend 10 + RSI 55-68 10 + MACD 10 + breakout above 20-day high 15 + volume 15 + strength near 52-week high 10 + tight base 5.
SIGNAL needs score >= 70 **and** breakout, volume >= 1.2x average, price > EMA20, RSI 50-75. Stop = 1.5 x ATR (max 8%), size so the loss at the stop is 1% of capital (max 25% of capital per stock),
Target 1 = 2R (sell half, stop to break-even, then trail), Target 2 = 3.5R, trend-break and 15-session time exits, 48h cooldown, daily loss limit 2%. All tunable in `.env`.

## Important honesty notes
- This is a **paper** system. The strategy has not been validated: run it for weeks, read `History`, and backtest before any real-money decision. Zero-signal days are normal.
- Market data comes from Upstox (needs your Analytics token) with Yahoo fallback (unofficial, may be delayed). Without a token or fallback you will see `NO_DATA` rows.
- The built-in holiday list covers 2026; add later dates in `NSE_HOLIDAYS_EXTRA` (the bot warns you).
- No login brute-force protection beyond a 5-try lockout; restrict the firewall rule to your own IP if you can.

## Tests
`python -m unittest tests.test_bot` (synthetic data; no network needed).
