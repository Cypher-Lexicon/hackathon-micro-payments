"""Centralized configuration module for the Owncast Webhook Sidecar.

Loads environmental variables and defines default values for network connections,
fees, USDC contracts, and wallet addresses.
"""

import json
import os

# Try to load environment variables from local .env file if it exists
def _load_env_file(filepath=".env"):
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    os.environ[key] = val

_load_env_file()

# Sidecar Server Bind Config
SIDECAR_HOST = os.environ.get("SIDECAR_HOST", "127.0.0.1")
SIDECAR_PORT = int(os.environ.get("SIDECAR_PORT", "8081"))

# Stream Pricing configuration
# Per-second price in USD. E.g., $0.0001/sec = 36 cents per hour.
RATE_PER_SECOND = float(os.environ.get("RATE_PER_SECOND", "0.0001"))

# Session & Authorization Lifecycle config
# A session is reaped if no USER_PARTED arrives within this time (30s)
STALE_SESSION_TIMEOUT_SEC = 30
# EIP-3009 validity window (2 hours default)
AUTH_VALIDITY_SECONDS = int(os.environ.get("AUTH_VALIDITY_SECONDS", "7200"))

# Pre-defined payment tiers presented to the viewer in MetaMask
DEFAULT_TIERS = [
    {"cents": 5,   "minutes": 5,   "label": "$0.05 for up to 5 min"},
    {"cents": 10,  "minutes": 15,  "label": "$0.10 for up to 15 min"},
    {"cents": 25,  "minutes": 60,  "label": "$0.25 for up to 1 hour"},
    {"cents": 100, "minutes": 300, "label": "$1.00 for up to 5 hours"},
]
TIERS_JSON = os.environ.get("TIERS_JSON", json.dumps(DEFAULT_TIERS))
TIERS = json.loads(TIERS_JSON)

# Web3 / Blockchain Network settings (Arc Testnet)
STREAMER_WALLET = os.environ.get("STREAMER_WALLET", "0xb3629f8d08e0205Ff0B2c73958D24B956FcD05cB")
USDC_ARC_ADDRESS = os.environ.get("USDC_ARC_ADDRESS", "0x3600000000000000000000000000000000000000")
USDC_CHAIN_ID = int(os.environ.get("USDC_CHAIN_ID", "5042002"))
ARC_RPC_URL = os.environ.get("ARC_RPC_URL", "https://rpc.testnet.arc.network")

# Private key of the streamer wallet (used to pay gas & settle transactions on-chain)
STREAMER_PRIVATE_KEY = os.environ.get("STREAMER_PRIVATE_KEY")
