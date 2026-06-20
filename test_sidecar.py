"""Smoke test the sidecar (Day 2: browser-script flow).

Tests the full auth lifecycle including the /lookup/by-user-id endpoint
that the browser script uses to discover its session.

Run:
    1. python sidecar.py
    2. python test_sidecar.py
"""

import time
import uuid
from datetime import datetime, timezone, timedelta

import urllib.request
import json


SIDECAR_URL = "http://127.0.0.1:8081"


def post(url, data=None):
    body = json.dumps(data).encode("utf-8") if data is not None else b""
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def get(url):
    """GET and return parsed JSON. Returns (data, status) for 4xx/5xx."""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def post_event(event_type: str, user_id: str, display_name: str,
               ts: datetime) -> None:
    payload = {
        "type": event_type,
        "eventData": {
            "id": user_id,
            "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "user": {"id": user_id, "displayName": display_name},
        },
    }
    post(f"{SIDECAR_URL}/webhook", payload)
    print(f"  -> {event_type} ({display_name})")


def fetch_snapshot():
    data, _ = get(f"{SIDECAR_URL}/")
    return data


def assert_eq(got, expected, label=""):
    if got != expected:
        print(f"  ✗ {label}: expected {expected!r}, got {got!r}")
        raise AssertionError(f"{label}: {got!r} != {expected!r}")
    print(f"  ✓ {label}")


def main():
    base = datetime.now(timezone.utc)

    # ── Scenario 1: browser discovers its session via /lookup ──────
    print("Scenario 1: browser discovers session via /lookup")
    uid1 = str(uuid.uuid4())
    post_event("USER_JOINED", uid1, "alice", base)

    # Browser calls /lookup/by-user-id/<uid1>
    lookup, _ = get(f"{SIDECAR_URL}/lookup/by-user-id/{uid1}")
    assert_eq(lookup["found"], True, "lookup found")
    req_id = lookup["auth_request_id"]
    print(f"  -> discovered auth_request_id={req_id}")

    # Unknown user → 404
    r404, status = get(f"{SIDECAR_URL}/lookup/by-user-id/unknown-user-id")
    assert_eq(status, 404, "lookup unknown user returns 404 status")
    assert_eq(r404["found"], False, "lookup unknown user returns found=false")
    print("  ✓ /lookup returns 404 for unknown user")

    # Browser polls /session — should be needs_auth with tiers
    sess, _ = get(f"{SIDECAR_URL}/session/{req_id}")
    assert_eq(sess["state"], "needs_auth")
    assert len(sess["tiers"]) == 4, f"expected 4 tiers, got {len(sess['tiers'])}"
    print(f"  ✓ /session returns needs_auth with {len(sess['tiers'])} tiers")
    print(f"  ✓ streamer_wallet={sess['streamer_wallet']}")
    print(f"  ✓ usdc_chain_id={sess['usdc_chain_id']}")

    # ── Scenario 2: full happy path (authorize + settle) ────────────
    print("\nScenario 2: full auth + settle flow")
    fake_auth = {
        "from": "0x" + "11" * 20,
        "to": sess["streamer_wallet"],
        "value": "250000",   # $0.25 in 6-decimal
        "validAfter": "0",
        "validBefore": str(sess["valid_before"]),
        "nonce": "0x" + "22" * 32,
        "v": "1b",
        "r": "0x" + "33" * 32,
        "s": "0x" + "44" * 32,
    }
    r = post(f"{SIDECAR_URL}/authorize/{req_id}", {
        "tier_cents": 25,
        "authorization": fake_auth,
    })
    assert_eq(r["ok"], True, "authorize accepted")
    sess, _ = get(f"{SIDECAR_URL}/session/{req_id}")
    assert_eq(sess["state"], "authorized", "state after auth")

    # Lookup should now show authorized
    lookup, _ = get(f"{SIDECAR_URL}/lookup/by-user-id/{uid1}")
    assert_eq(lookup["auth_status"], "authorized", "lookup auth_status after authorize")

    # User leaves → settle
    post_event("USER_PARTED", uid1, "alice", base + timedelta(seconds=300))
    r = post(f"{SIDECAR_URL}/settle/{req_id}", {
        "tx_hash": "0x" + "ab" * 32,
        "amount_usdc": "0.25",
    })
    assert_eq(r["ok"], True, "settle accepted")

    # ── Scenario 3: decline flow ───────────────────────────────────
    print("\nScenario 3: decline flow")
    uid2 = str(uuid.uuid4())
    post_event("USER_JOINED", uid2, "bob", base + timedelta(seconds=1))
    lookup, _ = get(f"{SIDECAR_URL}/lookup/by-user-id/{uid2}")
    req2 = lookup["auth_request_id"]
    r = post(f"{SIDECAR_URL}/decline/{req2}")
    assert_eq(r["ok"], True, "decline accepted")
    sess, _ = get(f"{SIDECAR_URL}/session/{req2}")
    assert_eq(sess["state"], "declined", "state after decline")
    post_event("USER_PARTED", uid2, "bob", base + timedelta(seconds=60))

    # ── Scenario 4: stale reaper ───────────────────────────────────
    print("\nScenario 4: stale session reaper")
    uid3 = str(uuid.uuid4())
    post_event("USER_JOINED", uid3, "carol", base + timedelta(seconds=2))
    # Don't part or auth — let the reaper handle it
    time.sleep(40)  # wait past STALE_SESSION_TIMEOUT_SEC (30s) + slack

    # ── Final assertions ────────────────────────────────────────────
    snap = fetch_snapshot()
    print("\nFinal snapshot:")

    alice_settled = next(s for s in snap["recent_settlements"] if s["username"] == "alice")
    assert_eq(alice_settled["auth_status"], "settled")
    assert_eq(alice_settled["amount_charged_usdc"], 0.25)
    assert_eq(alice_settled["tx_hash"], "0x" + "ab" * 32)

    bob_settled = next(s for s in snap["recent_settlements"] if s["username"] == "bob")
    assert_eq(bob_settled["auth_status"], "declined")

    # carol should have been reaped with pending auth
    carol_settled = next(
        (s for s in snap["recent_settlements"] if s["username"] == "carol"),
        None,
    )
    assert carol_settled is not None, "carol should be in settled (reaped)"
    assert_eq(carol_settled["auth_status"], "pending")

    assert snap["total_settled_onchain_usdc"] >= 0.25

    print(f"\n{'=' * 40}")
    print(f"Total earned (metered): ${snap['total_earned_usd']}")
    print(f"Total onchain settled:  ${snap['total_settled_onchain_usdc']}")
    print(f"Settled sessions:       {snap['settled_count']}")
    print(f"{'=' * 40}")
    print("\n✓ All 4 scenarios passed")


if __name__ == "__main__":
    main()