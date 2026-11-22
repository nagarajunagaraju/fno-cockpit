"""Offline demo smoke test; uses synthetic data only and never places orders."""
from app.data_source import DemoSource
from app import analytics, predictor

source = DemoSource()
rows = source.snapshot()
index = source.index()
index_change = analytics.pct(index["ltp"], index["prev_close"])
scan = [analytics.analyse(row, index_change) for row in rows]
report = predictor.build_report(rows, index_change)
backtest = predictor.walk_forward_backtest(rows)
assert rows and len(scan) == len(rows)
assert report["universe_count"] == len(rows)
assert len(report["top_bullish"]) <= 5 and len(report["top_bearish"]) <= 5
assert backtest.get("samples", 0) >= 0
print(f"PASS: {len(rows)} demo symbols; {len(report['top_bullish'])} bullish; "
      f"{len(report['top_bearish'])} bearish; {backtest.get('samples', 0)} backtest samples")
print("NOTE: synthetic demo results are not evidence of live-market accuracy.")
