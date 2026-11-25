# Smart Money Footprint (SMF) scanner

EOD research tool inside F&O Cockpit. Finds stocks where delivery, volume, price behaviour and (for F&O)
futures OI suggest quiet accumulation or distribution. It never places orders.

## Data (NSE public archives, no Kite login needed)
- `sec_bhavdata_full_DDMMYYYY.csv`: OHLC, volume, turnover, delivery qty/%
- `BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip`: stock futures OI (all expiries summed), near-month close, lot
- `ind_close_all_DDMMYYYY.csv`: Nifty 50 close; `ind_nifty500list.csv`; `fo_secban.csv`
Stored in `data/smf.db`. Incremental: only missing dates are downloaded. Holidays (404 for a past date) are skipped.

## Lists
- **FNO**: stocks in that day's F&O bhavcopy, 20-day turnover >= Rs 50 Cr, not in ban. Bull and bear.
- **INV**: Nifty 500, turnover >= Rs 10 Cr. Bull only (cash investing).
Weights and rules: `app/smf/config.yaml` (app Settings writes `data/smf/config_override.yaml`).

## Jobs (run as subprocesses: `python -m app.smf.run update|daily|backtest|all`)
- Weekdays 18:30-22:30 IST: update + list + tracker + paper log + Excel + Telegram, retry every 15 min.
- Backtest: train/test split (last 120 days = test), de-duplicated events, excess vs same-day average,
  monthly consistency, weight sensitivity, honest verdict.

## API
`/api/smf/today`, `/status`, `/backtest`, `/watch`, `/paper`, `/excel`, `POST /run/{job}`, `POST /settings`,
`POST /telegram` (token stored on the server only, never returned).
