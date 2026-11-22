# Options Cockpit

Stock **options buying** kosam nee personal dashboard. Nee own Zerodha Kite account tho, nee laptop lo run avuthundi.

Flow: **Stock direction → correct CE/PE strike → premium SL & size → discipline check → journal.**

| Problem | Cockpit lo em chestundi |
| --- | --- |
| Direction confusion | 180+ F&O stocks ki live **score (-100 to +100)**: daily trend (EMA20/50), futures OI build-up, strength vs NIFTY, RSI. Result: **BUY CE / BUY PE / NO TRADE**. |
| Strike selection | Stock click chesthe **option chain**: OI, OI change, IV, delta per strike. PCR, max pain, support (highest PE OI), resistance (highest CE OI). ★ = suggested strike (delta ~0.55, liquid). |
| Entry/exit timing | Stock ATR SL ni delta tho **premium SL** ga convert chestundi (15%–40% of premium). T1 = 1.5R, T2 = 2.5R. "Exit if stock crosses X" level kuda. Premium SL/T1/T2 leda stock SL hit aithe beep + desktop alert. |
| Risk & position size | Lots = min(risk budget ÷ premium risk per lot, premium cap ÷ premium per lot). 1 lot kuda ekkuva aithe "skip". |
| Option traps | Warnings: expiry < 5 days (theta), IV > 1.4× stock HV20 (costly premium, IV crush), wide bid-ask spread, low OI, far OTM (delta < 0.3), high theta/day. |
| Discipline / overtrading | **Discipline Guard**: max trades/day, daily loss limit, loss taruvata cooldown, max open positions. Limit cross aithe "Log Trade" lock. |

Plus **Journal**: close chesetappudu mistakes tag cheyi. Rules follow chesina vs break chesina trades P&L pakka pakkana.

> Personal use matrame. Kite data ni vere vallaki share/sell cheyakudadu.
> Tool orders place cheyadu; order nuvve Kite lo pettali. Idi advice kaadu, nee rules follow avvadaniki tool.

---

## 1. Demo first (no login)

**Windows:** `run_demo.bat` double-click. **Mac/Linux:** `./run.sh demo`
Browser lo http://127.0.0.1:8000. Data fake, features anni pani chestayi.

## 2. Live with Kite

1. https://developers.kite.trade lo app create cheyi (Kite Connect + historical data access).
2. Redirect URL = `http://127.0.0.1:8000/callback`
3. `config.example.json` → `config.json` copy chesi `api_key`, `api_secret` fill cheyi.
4. `run.bat` / `./run.sh` → **Login with Kite** (roju okasari).

First run of the day: ~180 stocks history load 2–3 min (rate limit), taruvata cached. Option chain lo OI chg% first time "…" ga untundi, background lo prev-day OI load ayyaka vastundi.

## 3. Daily routine

1. **9:00** Rules tab lo capital, risk check. Market open ayyaka marchaku.
2. **9:30+** Scanner lo BUY CE / BUY PE stocks matrame (|score| ≥ 45).
3. Stock click → chain lo support/resistance, IV chudu. ★ strike leda ATM/1 ITM.
4. Expiry < 5 days aithe next month expiry select cheyi.
5. Planner warnings unte skip. Checklist 6 tick → **Log Trade**. Kite lo entry + SL order okesari.
6. Alerts vasthe follow avvu: SL hit = exit. T1 = partial book, SL cost ki.
7. Close lo honest ga mistakes tag. **Weekend:** Journal → costliest mistake ki okka rule.

## Next-session prediction research (new)

- `/api/predictions` creates a next-session research ranking with up to 5 bullish and 5 bearish candidates. It includes a transparent score, reasons, spot reference, ATR-based reference stop/targets and a **score-strength estimate**.
- `/api/backtest` runs a chronological one-step directional diagnostic over the historical daily candles currently available to the source.
- A background scheduler saves a JSON report under `reports/YYYY-MM-DD.json` at **09:28 Asia/Kolkata** on weekdays only when a fresh feed snapshot exists. Exchange holidays are not automatically known; check the feed timestamp.
- The technical indicators now use cash/spot candles with a live cash/spot quote. Futures LTP and futures previous close are kept separately for futures price/OI build-up classification. This fixes the previous cash-candle + futures-LTP mixing issue.

**Important limitations:** The score-strength number is not a calibrated probability. The current backtest uses the history available from the configured data source (currently about 180 calendar days by default), skips transaction costs/slippage, and is not a full multi-year validation. Demo mode is synthetic and must never be used to judge model accuracy or trade. No trade is a valid output; Top 5 lists are capped and can be empty. The report is research support, not a recommendation or guarantee.

## Android client starter (new)

Open the `android/` directory in Android Studio. Run the backend first, then run the app on an Android emulator (default backend address `10.0.2.2:8000`). For a physical phone, change `API_BASE` in `MainActivity.kt` to your Windows PC LAN IP and follow `android/README.md`. This is a Compose starter client, not yet a signed/release APK; it has no push notifications, authentication, offline mode or Play Store configuration.

## Score

| Part | Points |
| --- | --- |
| Trend: price > EMA20 > EMA50 (or reverse) | ±40 (±15 only vs EMA20) |
| Futures OI: Long / Short Buildup | ±30 |
| Short Covering / Long Unwinding | ±12 |
| Relative strength vs NIFTY | up to ±15 |
| RSI ≥ 60 / ≤ 40 | ±15 |

## Default rules (Rules tab lo change cheyi)

| Rule | Default |
| --- | --- |
| Capital | ₹3,00,000 |
| Risk per trade | 1% |
| Max premium per trade | 10% of capital |
| Max trades/day | 3 |
| Daily loss limit | 2% |
| Cooldown after loss | 30 min |
| Max open positions | 2 |
| Stock SL = ATR × | 1.5 |

Stock option lots pedda (1 lot premium ₹10k–40k). 1% risk tho konni stocks "skip" chupistayi: adi nee risk reality, bug kaadu.

## Files

```
app/analytics.py    indicators, build-up, score
app/options.py      Black-Scholes IV, Greeks, PCR, max pain, strike suggestion
app/risk.py         option position sizing, premium SL/targets, discipline guard
app/journal.py      SQLite journal (cockpit.db)
app/data_source.py  DemoSource + KiteSource (futures, option chain)
app/main.py         FastAPI server
static/             dashboard
```

## Safety and production readiness

- Never share Kite API key, API secret, request token or access token in chat. Keep `config.json` and `.kite_token.json` private (both are git-ignored).
- Keep the backend bound to `127.0.0.1` for desktop use. Do not expose this development API to the public internet; authentication, HTTPS, rate limiting and secure secret storage are still required before remote deployment.
- The app does not place orders. User reviews signals and places any trade manually in Zerodha.
- The 09:28 scheduler uses weekdays, not an official NSE holiday calendar. It only saves when the live feed was refreshed recently; verify the timestamp.
