# Owncast Per-Second Streaming Webhook Sidecar

Implementation plan for **Lepton Agents Hackathon** (Canteen × Circle).
Building proposal #3 from the Distribution Bootstrap post: the
**Owncast Per-Second Streaming Webhook Sidecar**, settled on **Arc**
via **x402 + Circle Gateway/Nanopayments**.

## Hackathon constraints (re-read from the official page)

| Constraint | Source |
|---|---|
| Settlement on Arc, not Base | "Settlement · Arc · <500ms" |
| Must run on Circle Agent Stack | 20% of judging score |
| USDC on Arc | "settled on Arc in testnet USDC" |
| Idea #3 is in the official Prior Art list | owncast/owncast row, 11k stars |
| Idea #6 ("rate of flow, by the second") maps 1:1 | Prior Art entry #6 |
| Traction is 30% of judging | Real viewers + real payments flowing |
| 2 weeks (Jun 15 → Jun 29) | Online, async judging |
| Built-in Arc testnet via ARC CLI | `uv tool install git+https://github.com/the-canteen-dev/ARC-cli` |
| Circle CLI for x402 + agent wallets | `npm install -g @circle-fin/cli` |
| Reference impl exists | `circlefin/arc-nanopayments` (TS, Next.js + Supabase) |

## Stack decisions (post-research)

| Layer | Choice | Rationale |
|---|---|---|
| Sidecar runtime | **Python 3.12 + Flask** | Already running. Keep it. |
| WebSocket | `flask` + polling for v1, `websockets` lib for v2 | v1 doesn't need WS — HTTP POSTs from the browser script suffice |
| Browser script | Vanilla JS, hosted on sidecar at `/static/owncast-pay.js` | Injected via Owncast admin Custom JavaScript field |
| Wallet | MetaMask with Arc testnet added | x402 signs via `eth_signTypedData_v4` |
| Settlement chain | **Arc Testnet** (Canteen-hosted RPC via ARC CLI) | Hackathon-mandated |
| Payment protocol | **x402 (browser signs EIP-3009) + web3.py direct submission** | Bypasses the Node-only GatewayClient SDK; submits `transferWithAuthorization` directly to USDC on Arc |
| Facilitator | **None — direct USDC contract call** | Hackathon-friendly; EIP-3009 allows `value ≤ max` natively. Tradeoff: no Gateway batching (gas is paid by the streamer wallet per settlement, ~free on Arc testnet). Honest per-second settlement. |
| USDC | Native USDC on Arc | `faucet.circle.com` → Arc Testnet, free |

## What we're building (3 components)

```
┌────────────────────────────────────────────────────────────┐
│  Owncast (port 8080)                                        │
│  Streamer OBS → Owncast → viewers watch at :8080            │
└─────────────────┬──────────────────────────┬────────────────┘
                  │ webhooks                  │ HTML page
                  ▼                          ▼
┌────────────────────────────────────────────────────────────┐
│  Sidecar (Flask, port 8081) — OUR CODE                      │
│  POST /webhook  ← Owncast USER_JOINED / USER_PARTED         │
│  GET  /static/owncast-pay.js  → injected into Owncast HTML  │
│  POST /authorize/<id>  ← browser sends signed EIP-3009 auth │
│  POST /decline/<id>    ← viewer chose "Watch free"          │
│  GET  /session/<id>    ← browser polls for auth request     │
│  GET  /                → live HTML dashboard               │
└─────────────┬──────────────────────────────────────────────┘
              │ uses
              ▼
┌────────────────────────────────────────────────────────────┐
│  @circle-fin/x402-batching GatewayClient                    │
│  chain: 'arcTestnet'                                        │
│  - Buyer deposits USDC to Gateway (one-time)                │
│  - Viewer signs EIP-3009 auth offchain (zero gas)           │
│  - Gateway batches all auths into one onchain settlement    │
│  - Streamer wallet accumulates USDC balance                │
└────────────────────────────────────────────────────────────┘
```

## Why this works for per-second

Gateway batches offchain signed authorizations and settles them
periodically in one onchain transaction. A viewer signs **once** for
"$0.05 max for 2 hours"; the sidecar records the exact seconds
watched; on `USER_PARTED`, it submits the **actual** amount (≤ max)
for batched settlement. **No per-event gas.** Sub-cent payments
finally clear.

This is exactly what the proposal #3 in the bootstrap post describes,
and it directly matches the Prior Art entry #6 on the hackathon page.

## Day-by-day build plan

### Day 1 — Sidecar extensions (build on what's there)

**Goal:** Extend the existing `sidecar.py` to know about authorization
state, not just join/leave events.

**Changes to `sidecar.py`:**
- Add `AuthStatus` enum: `PENDING | AUTHORIZED | DECLINED | SETTLED | EXPIRED`
- Add `Session` fields: `auth_status`, `signed_authorization` (dict),
  `auth_request_payload` (the data the browser needs to sign),
  `tier_usdc` (how much this tier authorizes), `amount_charged_usdc`,
  `tx_hash`, `settled_at`
- Add tier table in env: `TIERS_JSON='[{"cents":5,"minutes":5},{"cents":10,"minutes":15},{"cents":25,"minutes":60}]'`
- Add HTTP endpoints:
  - `POST /authorize/<client_id>` — browser sends signed EIP-3009 auth
  - `POST /decline/<client_id>` — viewer declined
  - `GET  /session/<client_id>` — browser asks "what tier do you want me to sign?"
  - `GET  /static/owncast-pay.js` — serve the browser script
  - `GET  /` — replace JSON with an HTML dashboard

**Deliverable:** Open the dashboard, see active viewers with auth
status. POST mock auth via curl, see it transition to AUTHORIZED.

### Day 2 — Browser script + wallet flow

**Goal:** A viewer opening Owncast sees a tier-selection modal, picks
one, signs in MetaMask, and the sidecar receives the signed auth.

**New file: `static/owncast-pay.js`**
- On load, read `window.ethereum` (wait up to 2s if MetaMask still loading)
- Poll `GET /session/watcher` (a "me" endpoint) every 3s to check if sidecar wants auth
- When `REQUEST_AUTHORIZATION` arrives: show tier modal
- On confirm: `eth_requestAccounts` → build EIP-3009 typed data →
  `eth_signTypedData_v4` → `POST /authorize/<client_id>`
- On decline: `POST /decline/<client_id>`
- Reconnect on WS drop, retry auth if not received within 5s of page load

**How to inject:** In Owncast admin → Customize → Custom JavaScript,
paste: `<script src="http://localhost:8081/static/owncast-pay.js"></script>`

**Deliverable:** Open Owncast in a browser with MetaMask on Arc testnet
→ tier modal appears → click tier → MetaMask prompt with readable
fields → sign → sidecar shows AUTHORIZED.

### Day 3 — Gateway + Arc settlement

**Goal:** Real onchain settlement via Circle's x402-batching package.

**Setup:**
```bash
# Install ARC CLI for Canteen-hosted Arc testnet
uv tool install git+https://github.com/the-canteen-dev/ARC-cli

# Install Circle CLI for agent wallets
npm install -g @circle-fin/cli

# Fund viewer + streamer wallets with testnet USDC
# https://faucet.circle.com → Arc Testnet
```

**Architecture choice:** The official `@circle-fin/x402-batching` is
TypeScript. Options:
- **(a) Bridge:** Run a tiny Node service that exposes
  `POST /settle` → calls GatewayClient → returns tx. Sidecar calls it
  via HTTP. Cleanest separation.
- **(b) Direct:** Use `web3.py` to call the USDC contract on Arc
  directly with the signed EIP-3009 authorization. No batching, but
  per-second settlement is honest. Hackathon-friendly.
- **(c) Subprocess:** Sidecar shells out to a Node script.

**Recommended: (a).** The Node bridge is ~30 lines, reuses the official
SDK directly, and judges can read both code paths.

**New file: `settle.py` (Python, web3.py)**
```python
from web3 import Web3

def settle_authorization(w3, signed_auth, actual_amount_usdc):
    """Submit EIP-3009 transferWithAuthorization directly to USDC on Arc."""
    usdc = w3.eth.contract(
        address=Web3.to_checksum(USDC_ARC_ADDRESS),
        abi=USDC_ABI
    )
    tx = usdc.functions.transferWithAuthorization(
        Web3.to_checksum(signed_auth['from']),
        Web3.to_checksum(signed_auth['to']),
        int(actual_amount_usdc * 10**6),
        int(signed_auth['validAfter']),
        int(signed_auth['validBefore']),
        bytes.fromhex(signed_auth['nonce'][2:]),
        int(signed_auth['v'], 16),
        bytes.fromhex(signed_auth['r'][2:]),
        bytes.fromhex(signed_auth['s'][2:]),
    ).transact({'from': STREAMER_WALLET})
    return w3.eth.wait_for_transaction_receipt(tx)
```

**Sidecar changes:** On `USER_PARTED` with `auth_status == AUTHORIZED`,
call `settle_authorization(...)` with the signed auth + actual amount
→ update session with tx hash.

**Why this works without Gateway batching:** On Arc testnet, gas is
paid in USDC and a single `transferWithAuthorization` call costs
~$0.0001. A demo session settling one viewer's auth = one tx = a
fraction of a cent. We don't need batching at the demo's volume.

**What we lose vs Gateway:** Real production would batch thousands
of viewer authorizations into one onchain tx. For the demo this is
acceptable; for the post-hackathon roadmap, we document the swap as
a 50-line change to use `@circle-fin/x402-batching` from a Node
bridge.

**Deliverable:** Viewer signs → leaves → settlement fires → Arc
testnet explorer shows USDC moved.

### Day 4 — Live dashboard + chat confirmation

**Goal:** Replace the JSON `/` with a real HTML dashboard.

**`/dashboard` route in sidecar:**
- Live list of active viewers (name, joined_at, duration ticking, $ owed)
- Settled sessions table with tx hashes, Arc testnet explorer links
- Tier selector for the streamer (configure price per minute)
- Stats: total earned, total seconds watched, conversion rate
- Server-Sent Events (SSE) or 2s polling for live updates

**Chat confirmation:** Post to Owncast chat via the admin chat API
when a settlement completes. Verify the endpoint exists in the Owncast
version, otherwise post via the websocket chat channel.

**Deliverable:** Open dashboard, see live sessions ticking up the $
counter in real time.

### Day 5 — Hardening

- SQLite persistence (sessions survive sidecar restart)
- Tier exhaustion + re-auth mid-stream
- Stale-session reaper (already exists, tighten to 20s)
- Handle facilitator/bridge errors gracefully (queue for retry)
- README with: Arc testnet setup, MetaMask config, faucet steps,
  end-to-end demo script

### Day 6 — Rehearsal + submission

- Run the demo 5x end to end, time it
- Record a 3-minute Loom walkthrough
- Write the submission form response (public repo, traction metrics)
- **Demo script** (3 min, judges watching the Loom):
  1. Show Owncast running locally (10s)
  2. Open dashboard in second tab (5s)
  3. Open stream in browser → tier modal → MetaMask sign (30s)
  4. Watch for 2 minutes while dashboard ticks (90s)
  5. Close tab → settlement fires → Arc explorer shows USDC (20s)
  6. README pitch (5s)

## Traction plan (the other 30%)

Traction is half the score. Plan for it from Day 1:
- Post in Canteen Discord + Arc builder Discord as soon as you have the
  webhook metering working (Day 1). "Building an Owncast payment sidecar
  on Arc, who's got a stream I can test against?"
- By Day 3, get 2-3 actual creators to run the sidecar against their
  Owncast instances (even briefly)
- Document traction in the README: "5 test sessions, 47 minutes of
  paid viewing, $0.42 settled on Arc"

## File structure (target)

```
hackathon-micro-payments/
├── README.md
├── PLAN.md                   ← this file
├── sidecar.py                ← extended with auth + dashboard
├── test_sidecar.py           ← existing
├── settle.py                 ← web3.py direct EIP-3009 submission
├── static/
│   └── owncast-pay.js        ← injected into Owncast
├── templates/
│   └── dashboard.html
├── requirements.txt          ← flask, web3.py
└── data/                     ← SQLite files (gitignored)
```

## What I'm NOT building (and why)

- **No Node.js port of the sidecar** — Python is working, switching costs a day
- **No x402 self-hosted facilitator** — Gateway handles it
- **No LLM agent** — proposal #3 is creator monetization, not agent-to-agent. Save agent work for another submission if you have time
- **No per-second exact settlement** — tiered is honest and matches the Gateway batching model
- **No production deployment** — local demo, judges visit your live link

## Open items to confirm before Day 1

- [ ] Do you want me to add a `bridge/` directory for the Node GatewayClient bridge, or would you rather use `web3.py` directly from the sidecar?
- [ ] Confirm MetaMask is installed and you have a wallet address ready
- [ ] Confirm you have ~10 minutes to run through the ARC CLI + faucet setup before we start coding

The corrected plan is ready. Want me to start Day 1 now, or do you want to walk through any of the decisions first?