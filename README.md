# Owncast Per-Second Streaming Webhook Sidecar

Permissionless payments sidecar for [Owncast](https://owncast.online/), built for the **Lepton Agents Hackathon** (Canteen × Circle). 

It implements a webhook subscriber that derives viewing duration from join/leave events and settles flat rates to live streamers using MetaMask-signed EIP-3009 USDC transactions on the **Arc Testnet** blockchain.

---

## 🏗️ Codebase Structure

The project has been factorized into modular, cleanly documented components:

*   **`config.py`** — Centralized configuration management. Loads environment variables (Streamer wallet, RPC settings, pricing, chain configuration).
*   **`models.py`** — Ledger and Session data structures tracking active viewers and transaction statuses thread-safely.
*   **`settle.py`** — Handles EIP-3009 USDC on-chain transaction formulation, signing, and submission via `web3.py`.
*   **`sidecar.py`** — Flask server managing HTTP APIs, Owncast webhook receivers, and background transaction dispatching.
*   **`static/owncast-pay.js`** — Client-side injection script. Reads the visitor's user session, displays the tier selection modal, and requests MetaMask signature validation.
*   **`test_sidecar.py`** — End-to-end smoke test suite verifying registration, declines, session discovery, and timeouts.

---

## ⚡ Quickstart

### 1. Install Dependencies
Ensure you have Python 3.12+ and install the requirements:
```bash
/Users/joel/miniconda3/envs/hackathon-owncast/bin/pip install -r requirements.txt
```

### 2. Configure Environment Variables
You need to fund the streamer's wallet on Arc Testnet with USDC to pay for gas. 
*   **Arc Faucet**: Get testnet USDC on [faucet.circle.com](https://faucet.circle.com/) (select Arc network).
*   Set the private key for your streamer wallet:
    ```bash
    export STREAMER_PRIVATE_KEY="0x_STREAMER_PRIVATE_KEY_HERE"
    ```

### 3. Run the Sidecar
Start the server:
```bash
/Users/joel/miniconda3/envs/hackathon-owncast/bin/python sidecar.py
```

### 4. Wire it to Owncast
1.  Run Owncast on port `8080`.
2.  Open the Owncast Admin Interface (`http://localhost:8080/admin`, default login: `admin` / `abc123`).
3.  **Integrations ➡️ Webhooks ➡️ Add Webhook**:
    *   **URL**: `http://localhost:8081/webhook`
    *   **Events**: Enable `User Joined`, `User Parted`, and `User Changed Name` (so nicknames stay synchronized).
4.  **Customize ➡️ Custom JavaScript**:
    *   Fetch and copy the client JS script:
        ```bash
        curl http://localhost:8081/static/owncast-pay.js | pbcopy
        ```
    *   Paste it directly into the Custom JavaScript admin textarea (Owncast's CSP blocks external scripts).

5.  Open the stream page in your browser (`http://localhost:8080`) ➡️ MetaMask will prompt you to connect and sign your tier authorization!
6.  Close the tab, and watch the sidecar console submit the settlement to Arc Testnet.

---

## ⚙️ Configuration Options

Configure these options via environment variables:

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `STREAMER_PRIVATE_KEY` | None | Private key of the streamer wallet (used to submit gas & execute txs) |
| `SIDECAR_HOST` | `127.0.0.1` | Host to bind the sidecar server to |
| `SIDECAR_PORT` | `8081` | Port to bind the sidecar server to |
| `RATE_PER_SECOND` | `0.0001` | Flat stream rate rate in USD/sec |
| `STREAMER_WALLET` | `0xb3629f8...` | Address receiving the settled USDC |
| `USDC_ARC_ADDRESS` | `0x3600000...` | System contract address of USDC on Arc Testnet |
| `USDC_CHAIN_ID` | `5042002` | Network chain ID of the Arc Testnet |
| `ARC_RPC_URL` | `https://rpc.testnet.arc.network` | Public blockchain gateway provider URL |

---

## 🚀 State Machine

```
        USER_JOINED
            │
            ▼
        PENDING ◄──────────── reaper (30s stale without heartbeat)
        │    │
        │    ├── /authorize (signed EIP-3009) ──► AUTHORIZED (starts 10s heartbeat)
        │    │
        │    └── /decline  ─────────────────────► DECLINED (starts 10s heartbeat)
        │
        │  (USER_PARTED before authorize)
        ▼
     no charge
        │
        ▼
   AUTHORIZED + USER_PARTED ──► /settle (onchain via Web3.py) ──► SETTLED
```

---

## 💡 How It Works under the Hood

### 1. Gasless Viewer Payments (EIP-3009)
The project utilizes the **USDC EIP-3009 (TransferWithAuthorization)** protocol to allow viewers to pay streamers completely gaslessly:
- **EIP-712 Signature**: The viewer signs an off-chain authorization stating they permit the streamer to transfer a maximum USDC amount (e.g. `$0.05` for 5 min) before a specific expiration block.
- **On-chain Settlement**: The viewer does not execute the blockchain transaction and does not need any native gas token (ETH/ARC). Instead, the streamer's server captures this signed authorization and submits it to the Arc Testnet USDC contract on `USER_PARTED`, paying the gas fee.

### 2. Self-Healing Wallet Mismatch Recovery
Web3 browser environments often have multiple accounts connected, and the active account selected in the MetaMask extension window may differ from the site's primary connected account. 
- **Signer Verification**: The sidecar backend validates the EIP-712 signature locally using `eth_account.messages.encode_typed_data` to recover the signer.
- **Signer Mismatch Recovery**: If the recovered signer address (e.g. `0xc973...`) does not match the browser's stated `from` address (e.g. `0x2cf5...`), the backend returns a `400` error with `error: "signer_mismatch"`. The browser frontend automatically catches this error, updates its internal address variables to the recovered signer, and automatically re-prompts the user with the correct parameters, resolving mismatch issues transparently.

### 3. Heartbeat-Aware Session Reaper
Owncast only fires `USER_JOINED` and `USER_PARTED` webhooks. If a connection is dropped or a server restarts, the `USER_PARTED` event might be missed.
- **Heartbeat Loop**: To safely prune dropped sessions without affecting active viewers, the browser script sends a periodic keepalive ping to `/session/<auth_request_id>` every 10 seconds.
- **Reaper**: The sidecar backend maintains a thread-safe `last_seen_at` timestamp. The reaper daemon prunes only sessions that have gone silent (no heartbeat) for more than 30 seconds.