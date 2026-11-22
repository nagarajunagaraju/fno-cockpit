"""API tests in demo (simulated) mode: auth, engine endpoints, and the existing dashboard endpoints."""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("COCKPIT_DATA_DIR", tempfile.mkdtemp(prefix="cockpit_api_"))
os.environ["COCKPIT_DEMO"] = "1"
os.environ["COCKPIT_SIM_SPEED"] = "600"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

client = TestClient(main.app)
H = {"X-App-Token": main.APP_TOKEN}


def test_token_required():
    assert client.get("/api/engine/health").status_code == 401
    assert client.get("/api/dashboard").status_code == 401
    assert client.get("/api/engine/health", headers={"X-App-Token": "wrong"}).status_code == 401
    assert client.get("/api/engine/health", headers={"Authorization": f"Bearer {main.APP_TOKEN}"}).status_code == 200


def test_engine_endpoints():
    time.sleep(8)
    h = client.get("/api/engine/health", headers=H).json()
    assert h["execution"].startswith("PAPER") and h["feed"]["mode"] == "SIMULATED"
    sc = client.get("/api/engine/scanner", headers=H).json()
    assert sc["status"] == "running" and sc["universe"] == 40 and "full" not in sc
    for p in ("risk", "paper", "signals", "events?after=0", "events/last", "config", "backtest"):
        r = client.get(f"/api/engine/{p}", headers=H)
        assert r.status_code == 200, (p, r.text)
    assert client.post("/api/engine/config", headers=H, json={"nope": 1}).status_code == 400
    cfg = client.post("/api/engine/config", headers=H, json={"min_score": 8}).json()
    assert cfg["min_score"] == 8
    client.post("/api/engine/config", headers=H, json={"min_score": 7})


def test_backtest_endpoint():
    r = client.post("/api/engine/backtest", headers=H, json={"days": 12})
    assert r.status_code == 200
    for _ in range(60):
        b = client.get("/api/engine/backtest", headers=H).json()
        if not b["running"] and b["last"]:
            break
        time.sleep(1)
    assert b["last"] and b["last"]["source"] == "SIMULATED" and "stats" in b["last"]


def test_existing_dashboard_still_works():
    assert client.get("/").status_code == 200
    d = client.get("/api/dashboard", headers=H).json()
    assert d["mode"] == "DEMO" and d["rows"]
    sym = d["rows"][0]["symbol"]
    ch = client.get(f"/api/chain?symbol={sym}", headers=H)
    assert ch.status_code == 200 and ch.json()["strikes"]
    assert client.get("/api/journal", headers=H).status_code == 200
    assert client.get("/api/predictions", headers=H).status_code == 200
    assert client.post("/api/settings", headers=H, json={"capital": 300000}).status_code == 200
    assert client.get("/static/signals.html").status_code == 200
    assert client.get("/static/login_ok.html").status_code == 200
