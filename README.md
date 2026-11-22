# F&O Cockpit v2 — Live Options Signal Engine + Android App

Nee personal intraday **options-buying signal engine**: NSE F&O stocks anni continuous ga scan chestundi.
Entry trigger valid ayinappude phone ki alert vastundi, exact CE/PE contract, entry, SL, T1, T2,
quantity, and full reasoning tho.

* **Paper trading only.** App, server rendu orders place cheyavu. Nuvve Kite lo manual ga trade cheyali.
* **No quota.** Fixed time ledu, "2 BUY + 2 SELL" ledu. Valid setup lekapothe **NO TRADE**.
* **Missing data unte signal radu.** Option OI, futures OI, IV unavailable aithe adi "unavailable" ani chupistundi. Fake values pettadu.
* Research tool matrame, advice kaadu. Real money pettadaniki mundu 2–3 weeks paper results chudu.

---

## ⚡ Simple setup (VPS, one command)

1. **Kite:** developers.kite.trade → your app → regenerate the API secret (old one was shared). Keep key + secret ready.
2. **VPS:** AWS Lightsail → Mumbai → Ubuntu 24.04 → 1 GB plan → create. Networking tab: attach a **static IP**,
   and add firewall rule **HTTPS 443**.
3. **One command** (Lightsail → *Connect using SSH* opens a terminal in the browser; paste this):
   ```bash
   curl -fsSL https://raw.githubusercontent.com/nagarajunagaraju/fno-cockpit/main/deploy/bootstrap.sh | sudo bash
   ```
   It asks for the Kite key/secret, installs everything with HTTPS, and prints your **Server URL**, **App token**
   and the **Redirect URL** to put in Kite. Run the same command again any time to update.
4. **Phone:** install `dist/FnO-Cockpit-v2.0.0.apk` → enter Server URL + App token → allow notifications and background running.
5. **Every trading day ~9:00:** app → Login with Kite.

Detailed / manual steps are below.

---

## 1. Folder layout

```
app/                    FastAPI backend (old dashboard modules kept as-is)
app/engine/             NEW signal engine
  config.py             every threshold (editable from app / web)
  feed_kite.py          Zerodha WebSocket (KiteTicker) + REST fallback + 1-min candles + RVOL baseline
  feed_sim.py           SIMULATED feed for demo mode and tests
  candles.py            tick -> 1-min bars, VWAP, opening range, relative volume
  layers.py             4-layer evaluation + 10-point score + mandatory filters + entry trigger
  contracts.py          automatic CE/PE strike selection with explanation
  planner.py            premium SL / targets / sizing / explanation
  signals.py            lifecycle, duplicate prevention, risk locks, paper fills, P&L, events
  scanner.py            continuous scanner loop + option OI tracker + single-instance lock
  backtest.py           replay of 1-min history through the SAME evaluate()
  api.py                /api/engine/* endpoints
  ratelimit.py          throttle for every Kite REST call
static/signals.html     NEW web page (all 13 dashboard sections)
static/index.html       old dashboard (still works, "Signals" link added)
android/                Android app source (Kotlin + Compose)
deploy/                 VPS installer, systemd units, Caddy (HTTPS) config
tests/                  30 automated tests
backup_original/        your original files before this upgrade
```

## 2. Engine logic (what decides a signal)

**Layer 1 – Market & sector:** NIFTY 50, BANK NIFTY, stock's sector index (sector vs NIFTY relative strength), F&O breadth (info).
NIFTY *and* BANKNIFTY both > 0.5% against the trade → reject.

**Layer 2 – Price & volume:** relative volume (cumulative volume vs same minute, last 10 sessions), Open=Low / Open=High (0.10%),
change 1.0–2.8% preferred, > 3.5% = chasing → reject, VWAP side (mandatory), PDH breakout / PDL breakdown, circuit-limit distance.

**Layer 3 – Derivatives:** futures price + OI build-up, near-ATM option OI (put addition + call unwinding for bulls; reverse for bears),
IV vs reference (stored ATM-IV history, or HV20 until 5 days of IV are stored), Greeks, contract liquidity.

**Layer 4 – Score (min 7/10) + mandatory filters + trigger**

| Parameter | Points |
|---|---:|
| Relative volume | 2 (1 if ≥ 1.5x, 2 if ≥ 2x) |
| Futures price + OI | 2 (half for short covering / long unwinding) |
| Option chain confirmation | 2 (half if only one side confirms) |
| Open = Low / High | 1 |
| VWAP + PDH / PDL | 1 |
| Sector alignment | 1 |
| Price momentum (1.0–2.8%) | 1 |

Mandatory (any fail = no signal, whatever the score): fresh data, clear direction, VWAP side, not chasing, no index conflict,
not near circuit, futures OI available, option-chain OI available, IV not abnormal, score ≥ minimum, plus every risk rule.

**Entry trigger:** after the 10-minute opening range (09:25), a **completed** 1-min candle closes above the day's high
(bull) / below the day's low (bear). No new entries after 14:45. Only completed candles are used: no look-ahead.

**Contract selection:** nearest expiry with ≥ 5 days left; ATM ± 4 strikes checked for two-sided quote, spread ≤ 3%,
OI ≥ 50 lots, volume ≥ 20 lots, |delta| 0.35–0.80, IV not > 15% above ATM IV, 1 lot within premium cap.
Eligible strikes are ranked (delta fit 40, spread 25, liquidity 20, IV 10, theta 5) and the winner is explained against the runner-up.

**Trade plan:** underlying stop = tighter of VWAP / trigger-candle extreme (0.35%–1.5% band). Premium SL and targets are the
*actual contract repriced* (its IV, strike, expiry, time decay) at those spot levels, not the stock stop copied onto the premium.
T1 = 1.5R, T2 = 2.5R. Lots = min(risk budget ÷ (per-lot risk + charges), premium cap). Reward:risk to T1 must be ≥ 1.3.

**Lifecycle:** TRIGGERED → ACTIVE (paper fill) → TARGET_HIT / STOP_LOSS_HIT / EXPIRED (15:15 square-off);
TRIGGERED → INVALIDATED (spot broke stop before fill) / EXPIRED (missed fill in 3 min). Max 2 signals per stock per side per day,
30-min cooldown, a new signal needs a new extreme. Risk: 1% per trade, 2% daily loss lock, max 2 open, 25% exposure.

## 3. Demo first (no login, simulated data)

Windows: double-click `run_demo.bat` → browser opens `http://127.0.0.1:8000/static/signals.html`.
Data is **SIMULATED** (a fake fast-forward market) and only proves every screen works.

## 4. Mumbai VPS setup (recommended, runs 24x7)

1. Buy a small Ubuntu 22.04/24.04 VPS in Mumbai (1 vCPU, 1–2 GB RAM is enough).
2. **Cloudflare DNS** (your SMN domain): add an `A` record, e.g. `fno` → VPS IP, *DNS only (grey cloud)*.
   Only a DNS record is added: your SMN Worker and its routes are untouched.
3. Copy this folder to the VPS (`scp -r fno-cockpit root@VPS_IP:/root/`), then:
   ```bash
   cd /root/fno-cockpit && sudo bash deploy/install_vps.sh fno.yourdomain.in
   ```
4. **Regenerate your Kite API secret** (the old one was shared in a zip). Then:
   `sudo nano /opt/fno-cockpit/config.json` → new `api_key`, `api_secret`.
5. developers.kite.trade → your app → **Redirect URL** = `https://fno.yourdomain.in/callback`.
6. `sudo systemctl restart fno-cockpit` and get the app token: `sudo cat /opt/fno-cockpit/data/app_token.txt`.
7. Web check: `https://fno.yourdomain.in/static/signals.html?token=YOUR_TOKEN`.

The service restarts itself every weekday at 08:30 IST (fresh day), runs with one worker (one scanner),
restarts on crash, and Caddy gives automatic HTTPS. Update later: `sudo bash deploy/update_vps.sh`.
Logs: `journalctl -u fno-cockpit -f` or `/opt/fno-cockpit/data/logs/cockpit.log` (secrets are masked).

## 5. Phone app

1. Install `FnO-Cockpit-v2.0.0.apk` (allow "install unknown apps" for your file manager / browser).
2. Open → Server URL `https://fno.yourdomain.in` + App token → **Test connection & save**.
3. Allow notifications. Settings → **Allow background running** (battery optimisation off) so alerts keep coming.
4. **Every trading day ~9:00 AM:** app → Login with Kite (Kite rules: access token expires daily; this cannot be automated).
5. 9:15 nunchi scanner run avutundi; 9:25 tarvata valid trigger vasthe notification vastundi. Tap → full plan + reasoning.

Alerts: a small "F&O Cockpit watching" notification keeps the watcher alive; it checks your server every 10 s in market
hours and every 5 min otherwise (no Firebase).

## 6. Live on your Windows PC (optional)

`config.json` (from `config.example.json`) → `run.bat` → **Login with Kite** (redirect URL `http://127.0.0.1:8000/callback`).
`run_lan_for_phone.bat` lets the phone use the PC on home Wi-Fi (`http://PC-IP:8000` + token).

## 7. Backtest — read the limits

Run from the app (More → Backtesting) or web page. It downloads 1-min cash, futures (with OI) and index history and replays
it through the same `evaluate()`. Entries fill at the next bar open; stop is assumed before target in the same bar.

* Kite has **no data for expired option contracts** → the option-chain layer is excluded and the min score is scaled (7/10 → 6/8).
  Results are in **underlying R-multiples**, not option premium P&L.
* Futures minute OI exists only for the current contract → earlier days show futures OI as unavailable.
* Universe = today's F&O list (survivorship bias). Report says **INSUFFICIENT** under 20 days / 30 trades.
* Real option P&L (premium, spread, slippage, charges, missed fills) is recorded **going forward by paper trading** — that is the number to trust.

## 8. Tests

`run_tests.bat` (or `python -m pytest -q tests`) → 30 tests: layers, mandatory filters, missing-data rejection, no-look-ahead
(evaluate and backtest), candle builder, RVOL, contract selection, plan levels, lifecycle, missed fill, invalidation, risk locks,
daily loss lock, backtest stats, simulated end-to-end scanner day, API auth, old dashboard endpoints, rate limiter.

## 9. API (all need header `X-App-Token`)

`/api/engine/health · scanner · stock/{symbol} · signals · signals/{id} · events?after=N · events/last · risk · paper · config (GET/POST) · backtest (GET/POST)`.
Old endpoints (`/api/dashboard`, `/api/chain`, `/api/plan`, `/api/trades`, `/api/journal`, `/api/settings`, `/api/predictions`) unchanged.

## 10. Security

* `config.json`, `.kite_token.json`, `data/` stay on the server only (git-ignored). Never put Kite keys in the app.
* Every `/api/*` call needs the app token (constant-time check). Local PC browser is exempt only with `run.bat` (`COCKPIT_LOCAL=1`).
* Uvicorn listens on 127.0.0.1; only Caddy (HTTPS) is public. Firewall allows 22/80/443 only.
* The APK signing key is in `android/keystore/` — keep a private backup; future updates must be signed with the same key.

## 11. Not verified by the builder

The live Zerodha path (KiteTicker streaming, historical downloads, option quotes) was written against the Kite Connect API
but could **not** be run here (no Kite session). Verify on the first live morning: Data/API health screen should show
WebSocket "connected", baselines loaded = universe size, last tick age < 5 s. If anything is off, the health screen lists errors.
