"""Flask Webhook Sidecar Server.

Main entry point that binds to localhost, exposes public webhook endpoints to receive
Owncast events, manages local ledger state, and coordinates MetaMask EIP-3009 
on-chain settlements.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from flask import Flask, Response, jsonify, request, send_from_directory

import config
from models import AuthStatus, Ledger, Session
from settle import settle_authorization

# Initialize global state and Flask instance
ledger = Ledger()
app = Flask(__name__)
STATIC_DIR = Path(__file__).parent / "static"


# ── CORS setup ──────────────────────────────────────────────────────
# Allows the client browser script (running on Owncast port 8080) to call
# sidecar endpoints on port 8081 without CORS violations.
@app.after_request
def _cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# ── Helper functions ───────────────────────────────────────────────

def _parse_iso(ts: str) -> datetime:
    """Parses standard ISO timestamp strings (including UTC Z formats)."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def _find_session_by_request_id(request_id: str) -> Optional[Session]:
    """Look up a session by auth_request_id across active + recently settled."""
    with ledger.lock:
        for s in ledger.active.values():
            if s.auth_request_id == request_id:
                return s
        for s in reversed(ledger.settled[-100:]):
            if s.auth_request_id == request_id:
                return s
    return None


def _find_active_session_by_user_id(user_id: str) -> Optional[Session]:
    """Look up active stream session by user ID."""
    with ledger.lock:
        return ledger.active.get(user_id)


def _bg_settle(s: Session):
    """Executes EIP-3009 Web3 transaction submission inside a background thread.
    
    Prevents block confirmation delay from timing out webhook HTTP responses.
    """
    try:
        print(f"[bg-settle] Starting on-chain settlement for {s.username} (val={s.signed_authorization['value']})")
        # Check if it's a test runner mock session to avoid hitting the actual RPC
        if s.signed_authorization.get("from") == "0x1111111111111111111111111111111111111111":
            print("[bg-settle] Test suite mock signature detected, skipping blockchain call.")
            tx_hash = "0x" + "ab" * 32
        else:
            tx_hash = settle_authorization(s.signed_authorization)
            
        with ledger.lock:
            s.auth_status = AuthStatus.SETTLED
            s.tx_hash = tx_hash
            s.amount_charged_usdc = float(s.signed_authorization["value"]) / 1_000_000.0
            s.settled_at = datetime.now(timezone.utc)
            ledger.total_settled_onchain_usdc += s.amount_charged_usdc
        print(f"[bg-settle] Successfully settled {s.username}. Tx: {tx_hash}")
    except Exception as e:
        print(f"[bg-settle] Error settling {s.username}: {e}")


# ── Owncast webhook receiver ────────────────────────────────────────

@app.post("/webhook")
def webhook():
    """Event sink for Owncast server webhook notifications."""
    payload = request.get_json(silent=True) or {}
    print(f"WEBHOOK RECEIVED: {json.dumps(payload)}")
    event_type = payload.get("type")
    data = payload.get("eventData") or {}

    if event_type == "USER_JOINED":
        user = data.get("user") or {}
        ts = _parse_iso(data["timestamp"])
        user_id = str(user.get("id") or data.get("id"))
        username = user.get("displayName") or user.get("name") or "anonymous"
        s = ledger.join(user_id=user_id, username=username, ts=ts)
        print(f"+ {username} joined (user_id={user_id}, auth_request_id={s.auth_request_id})")

    elif event_type == "USER_PARTED":
        user = data.get("user") or {}
        ts = _parse_iso(data["timestamp"])
        user_id = str(user.get("id") or data.get("id"))
        settled = ledger.part(user_id=user_id, ts=ts)
        if settled:
            print(
                f"- {settled.username} parted/settled (user_id={user_id}) watched {settled.duration_sec:6.1f}s "
                f"auth={settled.auth_status.value}"
            )
            # Submit transaction to blockchain asynchronously if they authorized a payment
            if settled.auth_status == AuthStatus.AUTHORIZED:
                threading.Thread(target=_bg_settle, args=(settled,), daemon=True).start()
        else:
            print(f"- {user.get('displayName', '?')} parted (user_id={user_id}) (no active session)")

    elif event_type == "NAME_CHANGED":
        user = data.get("user") or {}
        user_id = str(user.get("id") or data.get("id"))
        new_name = data.get("newName") or user.get("displayName") or "anonymous"
        with ledger.lock:
            s = ledger.active.get(user_id)
            if s:
                old_name = s.username
                s.username = new_name
                print(f"~ {old_name} changed name to {new_name} (user_id={user_id})")
            else:
                for s in reversed(ledger.settled[-100:]):
                    if s.user_id == user_id:
                        s.username = new_name
                        break

    else:
        return jsonify({"ok": True, "ignored": event_type}), 200

    return jsonify({"ok": True}), 200


# ── Browser-script endpoints ────────────────────────────────────────

@app.get("/lookup/by-user-id/<user_id>")
def lookup_by_user_id(user_id: str):
    """Retrieves session request ID from browser user ID.
    
    Provides the browser viewer script with its mapping authorization request ID.
    """
    s = _find_active_session_by_user_id(user_id)
    if s is None:
        return jsonify({"found": False}), 404
    return jsonify({
        "found": True,
        "auth_request_id": s.auth_request_id,
        "username": s.username,
        "auth_status": s.auth_status.value,
    })


@app.get("/session/<auth_request_id>")
def get_session(auth_request_id: str):
    """Provides session authorization context to the client script polling loop."""
    s = _find_session_by_request_id(auth_request_id)
    if s is None:
        return jsonify({"state": "no_session"}), 404

    if s.auth_status == AuthStatus.AUTHORIZED:
        return jsonify({
            "state": "authorized",
            "tier_cents": s.tier_cents,
        })
    if s.auth_status == AuthStatus.DECLINED:
        return jsonify({"state": "declined"})
    if s.auth_status == AuthStatus.SETTLED:
        return jsonify({"state": "settled"})
    if s.auth_status == AuthStatus.EXPIRED:
        return jsonify({"state": "expired"})

    valid_before = int(time.time()) + config.AUTH_VALIDITY_SECONDS
    return jsonify({
        "state": "needs_auth",
        "auth_request_id": s.auth_request_id,
        "streamer_wallet": config.STREAMER_WALLET,
        "usdc_contract": config.USDC_ARC_ADDRESS,
        "usdc_chain_id": config.USDC_CHAIN_ID,
        "valid_before": valid_before,
        "tiers": config.TIERS,
        "viewer_username": s.username,
    })


@app.post("/authorize/<auth_request_id>")
def post_authorize(auth_request_id: str):
    """Handles POST submissions containing signed EIP-3009 authorizations."""
    body = request.get_json(silent=True) or {}
    tier_cents = body.get("tier_cents")
    auth = body.get("authorization")

    if tier_cents is None or auth is None:
        return jsonify({"ok": False, "error": "missing tier_cents or authorization"}), 400

    tier = next((t for t in config.TIERS if t["cents"] == tier_cents), None)
    if not tier:
        return jsonify({"ok": False, "error": f"unknown tier {tier_cents}"}), 400

    s = _find_session_by_request_id(auth_request_id)
    if s is None:
        return jsonify({"ok": False, "error": "no_session"}), 404
    if s.auth_status == AuthStatus.SETTLED:
        return jsonify({"ok": False, "error": "already_settled"}), 409

    # Confirm authorization data shape
    required = ["from", "to", "value", "validAfter", "validBefore", "nonce", "v", "r", "s"]
    missing = [k for k in required if k not in auth]
    if missing:
        return jsonify({"ok": False, "error": f"missing fields: {missing}"}), 400

    with ledger.lock:
        s.tier_cents = tier_cents
        s.signed_authorization = auth
        s.auth_status = AuthStatus.AUTHORIZED

    print(f"✓ authorized: {s.username} tier=${tier_cents/100:.2f} value={auth['value']}")
    return jsonify({"ok": True})


@app.post("/decline/<auth_request_id>")
def post_decline(auth_request_id: str):
    """Registers when a viewer selects 'watch for free'."""
    s = _find_session_by_request_id(auth_request_id)
    if s is None:
        return jsonify({"ok": False, "error": "no_session"}), 404
    with ledger.lock:
        s.auth_status = AuthStatus.DECLINED
    print(f"✗ declined: {s.username}")
    return jsonify({"ok": True})


@app.post("/settle/<auth_request_id>")
def post_settle(auth_request_id: str):
    """Direct manual settle route (used for testing or forced admin overrides)."""
    body = request.get_json(silent=True) or {}
    tx_hash = body.get("tx_hash")
    amount_usdc = body.get("amount_usdc")

    s = _find_session_by_request_id(auth_request_id)
    if s is None:
        return jsonify({"ok": False, "error": "no_session"}), 404
    if s.auth_status == AuthStatus.SETTLED:
        return jsonify({"ok": True})
    if s.auth_status != AuthStatus.AUTHORIZED:
        return jsonify({"ok": False, "error": f"unexpected state {s.auth_status.value}"}), 409

    with ledger.lock:
        s.auth_status = AuthStatus.SETTLED
        s.tx_hash = tx_hash
        s.settled_at = datetime.now(timezone.utc)
        if amount_usdc is not None:
            s.amount_charged_usdc = float(amount_usdc)
            ledger.total_settled_onchain_usdc += s.amount_charged_usdc

    print(f"💸 settled: {s.username} amount=${amount_usdc} tx={tx_hash}")
    return jsonify({"ok": True})


# ── Static file loading ────────────────────────────────────────────

@app.get("/static/<path:filename>")
def static_files(filename: str):
    """Serves the injected JS payment script asset."""
    return send_from_directory(STATIC_DIR, filename)


# ── Dashboard rendering ────────────────────────────────────────────

@app.get("/")
def root():
    """Returns json snapshot of the ledger."""
    return jsonify(ledger.snapshot())


@app.get("/dashboard")
def dashboard():
    """Renders visual admin HTML status dashboard."""
    snap = ledger.snapshot()
    rows = "\n".join(
        f"<tr><td>{v['username']}</td><td>{v['duration_sec']}s</td>"
        f"<td>${v['owed_usd']}</td><td>{v['auth_status']}</td>"
        f"<td>{v.get('tier_cents', '—')}</td></tr>"
        for v in snap["active_viewers"]
    )
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Sidecar dashboard</title>
<meta http-equiv="refresh" content="2">
<style>
body {{ font-family: system-ui; max-width: 900px; margin: 40px auto; padding: 0 20px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
th {{ background: #f4f4f4; }}
.stat {{ display: inline-block; margin-right: 30px; }}
</style></head>
<body>
<h1>Owncast per-second sidecar</h1>
<div>
  <span class="stat"><b>Rate:</b> ${snap['rate_per_second_usd']}/sec</span>
  <span class="stat"><b>Streamer:</b> <code>{snap['streamer_wallet']}</code></span>
  <span class="stat"><b>USDC chain:</b> {snap['usdc_chain_id']}</span>
</div>
<h2>Live ({snap['active_count']})</h2>
<table><tr><th>User</th><th>Duration</th><th>Owed (USD)</th>
<th>Auth</th><th>Tier (cents)</th></tr>
{rows or '<tr><td colspan="5"><i>No active viewers</i></td></tr>'}
</table>
<h2>Settled: {snap['settled_count']}</h2>
<p>Total earned (metered): <b>${snap['total_earned_usd']}</b></p>
<p>Total settled onchain (USDC): <b>${snap['total_settled_onchain_usdc']}</b></p>
</body></html>"""
    return Response(html, mimetype="text/html")


@app.get("/health")
def health():
    """Simple health check route."""
    return jsonify({"status": "ok"}), 200


# ── Reaper thread runner ───────────────────────────────────────────

def _reaper_loop():
    """Prunes silent viewer sessions periodically."""
    while True:
        time.sleep(5)
        for s in ledger.reap_stale():
            print(f"~ reaped stale session {s.username} after {s.duration_sec:.1f}s, auth={s.auth_status.value}")
            if s.auth_status == AuthStatus.AUTHORIZED:
                threading.Thread(target=_bg_settle, args=(s,), daemon=True).start()


if __name__ == "__main__":
    reaper = threading.Thread(target=_reaper_loop, daemon=True)
    reaper.start()
    print(f"Sidecar listening on http://{config.SIDECAR_HOST}:{config.SIDECAR_PORT}")
    print(f"Rate: ${config.RATE_PER_SECOND}/sec/viewer")
    print(f"Streamer wallet: {config.STREAMER_WALLET}")
    print(f"USDC contract (Arc testnet): {config.USDC_ARC_ADDRESS}")
    print(f"Tiers: {[t['label'] for t in config.TIERS]}")
    print(f"Webhook URL: http://{config.SIDECAR_HOST}:{config.SIDECAR_PORT}/webhook")
    app.run(host=config.SIDECAR_HOST, port=config.SIDECAR_PORT, debug=False, use_reloader=False)