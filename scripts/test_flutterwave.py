#!/usr/bin/env python3
# Copyright (C) 2026 Mugumya Zeal. All rights reserved.

"""
Flutterwave end-to-end sandbox test.
Tests: initiate checkout → poll transaction → simulate webhook → verify order updated.

Usage:
  FLW_SECRET_KEY=FLWSECK_TEST-xxx python scripts/test_flutterwave.py

Set BASE_URL to your Render URL or http://localhost:5000 for local testing.
"""

import json
import os
import sys
import time
import uuid

import requests

BASE_URL      = os.environ.get("BASE_URL", "http://localhost:5000")
FLW_SECRET    = os.environ.get("FLW_SECRET_KEY", "")
FLW_WEBHOOK_H = os.environ.get("FLW_WEBHOOK_HASH", "test-webhook-hash")
ADMIN_PW      = os.environ.get("ADMIN_PASSWORD", "")

if not FLW_SECRET:
    sys.exit("Set FLW_SECRET_KEY env var to your Flutterwave TEST secret key.")

def log(label, data=None):
    print(f"\n{'='*50}")
    print(f"  {label}")
    if data:
        print(json.dumps(data, indent=2, default=str))

# ── Step 1: Get an admin JWT ──────────────────────────────────────────────────
def get_admin_token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"password": ADMIN_PW}, timeout=10)
    assert r.status_code == 200, f"Login failed: {r.text}"
    token = r.json().get("token") or r.json().get("access_token")
    log("Admin login OK", {"token_prefix": token[:20] + "..."})
    return token

# ── Step 2: Initiate a Flutterwave checkout via the backend ──────────────────
def initiate_checkout(token):
    payload = {
        "listing_id": None,           # sandbox — no real listing needed
        "farmer_id": str(uuid.uuid4()),
        "product": "Test Maize (sandbox)",
        "quantity": 10,
        "unit": "kg",
        "price_per_unit": 1500,
        "currency": "UGX",
        "delivery_address": "Kampala, Uganda",
        "payment_provider": "flutterwave",
        "customer_email": "test@agribridge.co",
        "customer_name": "Test Buyer",
        "customer_phone": "+256700000000",
    }
    r = requests.post(f"{BASE_URL}/api/orders",
                      json=payload,
                      headers={"Authorization": f"Bearer {token}"},
                      timeout=15)
    data = r.json()
    log("Checkout initiated", {"status": r.status_code, "response": data})
    assert r.status_code in (200, 201), f"Checkout failed: {r.text}"
    return data

# ── Step 3: Simulate a Flutterwave webhook (successful payment) ───────────────
def simulate_webhook(order_id: str, tx_ref: str):
    payload = {
        "event": "charge.completed",
        "data": {
            "id": 123456789,
            "tx_ref": tx_ref,
            "flw_ref": f"FLW-MOCK-{uuid.uuid4().hex[:8].upper()}",
            "amount": 15000,
            "currency": "UGX",
            "status": "successful",
            "order_id": order_id,
            "customer": {
                "email": "test@agribridge.co",
                "name": "Test Buyer",
                "phone_number": "+256700000000",
            },
        },
    }
    body = json.dumps(payload)
    # Flutterwave signs with verif-hash header
    sig = FLW_WEBHOOK_H  # In sandbox this is the raw hash value from dashboard
    r = requests.post(f"{BASE_URL}/api/payments/flutterwave/webhook",
                      data=body,
                      headers={
                          "Content-Type": "application/json",
                          "verif-hash": sig,
                      },
                      timeout=10)
    log("Webhook simulated", {"status": r.status_code, "response": r.text[:200]})
    assert r.status_code == 200, f"Webhook handler returned {r.status_code}: {r.text}"

# ── Step 4: Verify order payment_status updated ───────────────────────────────
def verify_order(token, order_id):
    r = requests.get(f"{BASE_URL}/api/orders/{order_id}",
                     headers={"Authorization": f"Bearer {token}"},
                     timeout=10)
    data = r.json()
    log("Order after webhook", data)
    status = data.get("payment_status") or data.get("status")
    assert status in ("paid", "confirmed"), f"Expected paid/confirmed, got: {status}"
    print("\n✅  Flutterwave end-to-end test PASSED")

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    token    = get_admin_token()
    result   = initiate_checkout(token)
    order_id = result.get("order_id") or result.get("id")
    tx_ref   = result.get("tx_ref") or result.get("reference") or f"AGRI-{order_id}"
    print(f"\n→ Order ID: {order_id}")
    print(f"→ tx_ref:   {tx_ref}")
    print(f"→ Checkout URL: {result.get('checkout_url', 'N/A')}")
    print("\n(Opening checkout URL in a real browser would let you complete payment)")
    print("Simulating webhook now...")
    time.sleep(1)
    simulate_webhook(order_id, tx_ref)
    time.sleep(1)
    verify_order(token, order_id)
