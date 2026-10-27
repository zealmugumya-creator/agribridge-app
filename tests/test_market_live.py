# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Live market board: farmers' asking prices -> averages, change vs 24h, snapshots, alerts."""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

import app as app_mod

REPO = Path(__file__).resolve().parent.parent
NOW = datetime.datetime.now(datetime.UTC)


def iso(**kw):
    return (NOW - datetime.timedelta(**kw)).isoformat()


@pytest.fixture
def client():
    app_mod.app.config["TESTING"] = True
    app_mod._LIVE_CACHE.update(t=0.0, data=None)
    app_mod._last_spike_alert.clear()
    with app_mod.app.test_client() as c:
        yield c


def _db(monkeypatch, listings, refs=(), history=()):
    inserted, alerts = [], []

    def fake_get(table, filters=None, limit=100):
        return {"listings": list(listings), "market_prices": list(refs),
                "market_price_history": list(history)}.get(table, [])

    monkeypatch.setattr(app_mod, "supa_get", fake_get)
    monkeypatch.setattr(app_mod, "supa_insert", lambda t, d: inserted.append((t, d)) or [])
    monkeypatch.setattr(app_mod, "_discord_notify", lambda **k: alerts.append(k))
    return inserted, alerts


L = lambda crop, price, qty=100, district="Kampala": {  # noqa: E731
    "crop_name": crop, "price_per_kg": price, "quantity_kg": qty, "district": district}


def test_averages_range_and_sellers_come_from_listings(client, monkeypatch):
    _db(monkeypatch, [L("Maize", 700), L("maize ", 800), L("MAIZE", 900, district="Mbarara"), L("Beans", 3000)])
    j = client.get("/api/market/live").get_json()
    maize = next(r for r in j["rows"] if r["crop"] == "Maize")
    assert (maize["avg"], maize["low"], maize["high"], maize["sellers"]) == (800, 700, 900, 3)
    assert maize["qty_kg"] == 300 and maize["median"] == 800
    assert maize["districts"]["Mbarara"]["avg"] == 900 and maize["districts"]["Kampala"]["sellers"] == 2
    assert maize["change_pct"] is None            # no history yet: no invented change


def test_bad_prices_are_ignored_and_reference_price_shown(client, monkeypatch):
    _db(monkeypatch, [L("Maize", "abc"), L("Maize", -5), L("Maize", 0), L("Maize", 1000)],
        refs=[{"crop_name": "Maize", "price_per_kg": 750, "market": "Kampala Central"},
              {"crop_name": "Coffee", "price_per_kg": 12000, "market": "Kampala Central"}])
    rows = {r["crop"]: r for r in client.get("/api/market/live").get_json()["rows"]}
    assert rows["Maize"]["sellers"] == 1 and rows["Maize"]["avg"] == 1000 and rows["Maize"]["ref_price"] == 750
    assert rows["Coffee"]["sellers"] == 0 and rows["Coffee"]["avg"] is None and rows["Coffee"]["ref_price"] == 12000


def test_change_is_measured_against_24h_ago(client, monkeypatch):
    hist = [{"crop": "maize", "avg_price": 800, "captured_at": iso(hours=25)},
            {"crop": "maize", "avg_price": 950, "captured_at": iso(hours=2)}]
    _db(monkeypatch, [L("Maize", 880)], history=hist)
    maize = client.get("/api/market/live").get_json()["rows"][0]
    assert maize["change_basis"] == "24h" and maize["change_pct"] == 10.0     # 800 -> 880, not vs 2h ago


def test_change_falls_back_to_first_reading_when_history_is_young(client, monkeypatch):
    _db(monkeypatch, [L("Maize", 900)], history=[{"crop": "maize", "avg_price": 800, "captured_at": iso(hours=3)}])
    maize = client.get("/api/market/live").get_json()["rows"][0]
    assert maize["change_basis"] == "since_start" and maize["change_pct"] == 12.5


def test_snapshot_written_only_when_stale(client, monkeypatch):
    inserted, _ = _db(monkeypatch, [L("Maize", 800), L("Beans", 3000)])
    client.get("/api/market/live")
    assert inserted and inserted[0][0] == "market_price_history"
    assert {r["crop"] for r in inserted[0][1]} == {"maize", "beans"} and inserted[0][1][0]["district"] is None

    app_mod._LIVE_CACHE.update(t=0.0, data=None)
    inserted2, _ = _db(monkeypatch, [L("Maize", 800)],
                       history=[{"crop": "maize", "avg_price": 800, "captured_at": iso(minutes=2)}])
    client.get("/api/market/live")
    assert inserted2 == []                           # fresh snapshot exists: don't pile up rows


def test_big_move_alerts_discord_once(client, monkeypatch):
    hist = [{"crop": "maize", "avg_price": 500, "captured_at": iso(hours=25)}]
    _, alerts = _db(monkeypatch, [L("Maize", 800), L("Maize", 820)], history=hist)
    client.get("/api/market/live")
    app_mod._LIVE_CACHE.update(t=0.0, data=None)
    client.get("/api/market/live")
    assert len(alerts) == 1 and alerts[0]["event_type"] == "price.spike"


def test_one_seller_does_not_trigger_an_alert(client, monkeypatch):
    hist = [{"crop": "maize", "avg_price": 500, "captured_at": iso(hours=25)}]
    _, alerts = _db(monkeypatch, [L("Maize", 900)], history=hist)
    client.get("/api/market/live")
    assert alerts == []


def test_results_are_cached_briefly(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_mod, "_build_live_board", lambda: calls.append(1) or {"ok": True, "rows": []})
    client.get("/api/market/live")
    client.get("/api/market/live")
    assert len(calls) == 1


def test_migration_is_reversible_and_private():
    up = (REPO / "supabase/migrations/0011_market_price_history.up.sql").read_text().lower()
    assert (REPO / "supabase/migrations/0011_market_price_history.down.sql").exists()
    assert "enable row level security" in up and "to anon" not in up and "to authenticated" not in up


def test_prices_page_is_wired_to_the_live_endpoint():
    html = (REPO / "static/index.html").read_text(encoding="utf-8")
    assert "/api/market/live" in html and "renderLiveRows" in html and "tick-up" in html


def test_discord_fields_have_no_stray_markdown(monkeypatch):
    seen = {}

    class Fake:
        enabled = True

        def send(self, msg):
            seen.update(msg.fields)

    monkeypatch.setattr(app_mod, "DISCORD", Fake())
    app_mod._discord_notify("support.test", "t", "**table:** contact_messages\n**id:** 5")
    assert seen == {"table": "contact_messages", "id": "5"}
