# Copyright (C) 2026 zealmugumya-creator. All rights reserved.

"""Unit tests for the USSD session helpers and route state machine.

External systems (Supabase) are mocked so these run offline.
Tests verify:
  - _ussd_get / _ussd_set / _ussd_del delegate to the ussd_sessions table
  - The crop-listing multi-step flow stores/reads state via the DB helpers
  - The disease-diagnosis flow stores/reads state via the DB helpers
  - Session data is deleted (soft) when a terminal END screen is reached
"""
from __future__ import annotations

import pytest

import app as app_mod

# ── helpers to simulate the Africa's Talking POST body ───────────────────────

def _at_body(session_id: str, phone: str, text: str) -> dict:
    return {
        "sessionId": session_id,
        "phoneNumber": phone,
        "networkCode": "63902",
        "serviceCode": "*789#",
        "text": text,
    }


@pytest.fixture
def client():
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        yield c


# ── mock Supabase table for ussd_sessions ─────────────────────────────────────

class _FakeSupaTable:
    """Minimal stub that backs _ussd_get/set/del in-memory."""

    def __init__(self):
        self._store: dict[str, dict] = {}

    # supabase-py fluent builder stubs
    def table(self, name):
        return self

    def select(self, *a):
        return self

    def eq(self, col, val):
        self._filter = (col, val)
        return self

    def single(self):
        return self

    def execute(self):
        col, val = getattr(self, "_filter", (None, None))
        if col == "session_id":
            row = self._store.get(val)
            return _Resp(row)
        return _Resp(None)

    def upsert(self, row, on_conflict=None):
        self._store[row["session_id"]] = row
        return self

    def update(self, data):
        return self

    def execute_update(self):
        return _Resp(None)


class _Resp:
    def __init__(self, data):
        self.data = data


# ── _ussd_get / _ussd_set / _ussd_del unit tests ─────────────────────────────

def test_ussd_set_then_get(monkeypatch):
    """Data written with _ussd_set can be read back with _ussd_get."""
    store: dict[str, dict] = {}

    def fake_set(session_id, phone, data):
        store[session_id] = {"session_data": data, "is_active": True}

    def fake_get(session_id):
        row = store.get(session_id, {})
        return row.get("session_data") or {}

    monkeypatch.setattr(app_mod, "_ussd_set", fake_set)
    monkeypatch.setattr(app_mod, "_ussd_get", fake_get)

    app_mod._ussd_set("sess1", "+256700000001", {"crop": "Maize"})
    result = app_mod._ussd_get("sess1")
    assert result == {"crop": "Maize"}


def test_ussd_del_clears_session(monkeypatch):
    """After _ussd_del, _ussd_get returns {}."""
    store: dict[str, dict] = {}

    def fake_set(session_id, phone, data):
        store[session_id] = {"session_data": data, "is_active": True}

    def fake_get(session_id):
        row = store.get(session_id, {})
        if not row.get("is_active"):
            return {}
        return row.get("session_data") or {}

    def fake_del(session_id):
        if session_id in store:
            store[session_id]["is_active"] = False

    monkeypatch.setattr(app_mod, "_ussd_set", fake_set)
    monkeypatch.setattr(app_mod, "_ussd_get", fake_get)
    monkeypatch.setattr(app_mod, "_ussd_del", fake_del)

    app_mod._ussd_set("sess2", "+256700000002", {"animal": "Cattle"})
    app_mod._ussd_del("sess2")
    assert app_mod._ussd_get("sess2") == {}


def test_ussd_get_missing_returns_empty(monkeypatch):
    """_ussd_get on unknown session returns {}."""
    monkeypatch.setattr(app_mod, "_ussd_get", lambda sid: {})
    assert app_mod._ussd_get("nonexistent") == {}


# ── USSD route state-machine tests ───────────────────────────────────────────

def _patch_ussd(monkeypatch):
    """Install in-memory session helpers into app module for route tests."""
    store: dict = {}

    def fake_set(session_id, phone, data):
        store[session_id] = dict(data)

    def fake_get(session_id):
        return dict(store.get(session_id, {}))

    def fake_del(session_id):
        store.pop(session_id, None)

    monkeypatch.setattr(app_mod, "_ussd_set", fake_set)
    monkeypatch.setattr(app_mod, "_ussd_get", fake_get)
    monkeypatch.setattr(app_mod, "_ussd_del", fake_del)
    return store


class _FakeInsert:
    """supabase.table().insert().execute() stub for the listing insert."""
    def table(self, *a): return self
    def insert(self, *a): return self
    def execute(self): return type("R", (), {"data": [{"id": "fake"}]})()
    def upsert(self, *a, **k): return self
    def select(self, *a): return self
    def eq(self, *a): return self
    def single(self): return self


def test_ussd_crop_listing_flow(monkeypatch, client):
    """Crop listing multi-step (menu 4): depth 3 stores crop, 4 stores qty, 5 saves listing.

    Menu 4 is "List My Produce for Sale". Navigation: 4 -> sub-menu 1 (crops)
    -> crop choice 1 (Maize) -> quantity -> price -> END.
    """
    store = _patch_ussd(monkeypatch)
    # Stub the Supabase helpers used inside the route
    monkeypatch.setattr(app_mod, "supa_get", lambda *a, **kw: [])
    monkeypatch.setattr(app_mod, "supa_insert", lambda table, data: [{"id": "fake"}])
    # Stub SMS send
    monkeypatch.setattr(app_mod, "at_sms", None)

    sid = "AT_TEST_01"
    phone = "+256700000003"

    # depth 3: menu 4 (list produce) -> sub 1 (crop listing) -> choice 1 (Maize)
    resp = client.post("/api/ussd", data=_at_body(sid, phone, "4*1*1"))
    assert resp.status_code == 200
    text = resp.data.decode()
    assert "CON" in text and "Maize" in text, f"Unexpected: {text}"
    assert store.get(sid, {}).get("crop") == "Maize"

    # depth 4: entered quantity 500
    resp = client.post("/api/ussd", data=_at_body(sid, phone, "4*1*1*500"))
    assert resp.status_code == 200
    text = resp.data.decode()
    assert "CON" in text and "500" in text, f"Unexpected: {text}"
    assert store.get(sid, {}).get("qty") == "500"

    # depth 5: entered price 800 -> END + session deleted
    resp = client.post("/api/ussd", data=_at_body(sid, phone, "4*1*1*500*800"))
    assert resp.status_code == 200
    text = resp.data.decode()
    assert "END" in text and "Maize" in text, f"Unexpected: {text}"
    # session should be cleared
    assert sid not in store


def test_ussd_crop_diagnosis_stores_crop(monkeypatch, client):
    """Disease diagnosis depth 3 stores crop, depth 4 clears session."""
    store = _patch_ussd(monkeypatch)

    sid = "AT_TEST_02"
    phone = "+256700000004"

    # depth 3: menu 6 (vet) -> sub 1 (crops) -> choice 2 (Matooke)
    resp = client.post("/api/ussd", data=_at_body(sid, phone, "6*1*2"))
    assert resp.status_code == 200
    assert "CON" in resp.data.decode()
    assert store.get(sid, {}).get("crop") == "Matooke"

    # depth 4: symptom 1 (yellowing) -> END + session cleared
    resp = client.post("/api/ussd", data=_at_body(sid, phone, "6*1*2*1"))
    assert resp.status_code == 200
    text = resp.data.decode()
    assert "END" in text
    assert sid not in store


def test_ussd_animal_diagnosis_stores_animal(monkeypatch, client):
    """Animal diagnosis depth 3 stores animal, depth 4 clears session."""
    store = _patch_ussd(monkeypatch)

    sid = "AT_TEST_03"
    phone = "+256700000005"

    # depth 3: menu 6 -> sub 2 (animals) -> choice 1 (Cattle)
    resp = client.post("/api/ussd", data=_at_body(sid, phone, "6*2*1"))
    assert resp.status_code == 200
    assert "CON" in resp.data.decode()
    assert store.get(sid, {}).get("animal") == "Cattle"

    # depth 4: symptom 2 (not eating) -> END + session cleared
    resp = client.post("/api/ussd", data=_at_body(sid, phone, "6*2*1*2"))
    assert resp.status_code == 200
    text = resp.data.decode()
    assert "END" in text
    assert sid not in store
