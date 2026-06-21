"""Data models and Session/Ledger state management for the sidecar.

Tracks active viewers, manages join/part logic, and stores EIP-3009 signed payloads.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import config

class AuthStatus(str, Enum):
    """Lifecycle states of the viewer payment authorization."""
    PENDING = "pending"        # Joined, user prompt showing
    AUTHORIZED = "authorized"  # Viewer signed in MetaMask, awaiting checkout
    DECLINED = "declined"      # Viewer selected "watch for free"
    SETTLED = "settled"        # USDC transaction confirmed on Arc Chain
    EXPIRED = "expired"        # Reaped before they signed or completed


@dataclass
class Session:
    """Represents a viewer streaming session."""
    user_id: str
    username: str
    joined_at: datetime
    parted_at: Optional[datetime] = None
    settled: bool = False
    auth_status: AuthStatus = AuthStatus.PENDING
    tier_cents: Optional[int] = None
    signed_authorization: Optional[dict] = None
    auth_request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    amount_charged_usdc: Optional[float] = None
    tx_hash: Optional[str] = None
    settled_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None

    @property
    def duration_sec(self) -> float:
        """Calculates total stream duration watched in seconds."""
        end = self.parted_at or datetime.now(timezone.utc)
        return max(0.0, (end - self.joined_at).total_seconds())

    @property
    def owed_usd(self) -> float:
        """Calculates value accumulated during the session based on config.RATE_PER_SECOND."""
        return self.duration_sec * config.RATE_PER_SECOND


@dataclass
class Ledger:
    """Manages thread-safe lookup maps for all active and completed sessions."""
    lock: threading.Lock = field(default_factory=threading.Lock)
    active: dict[str, Session] = field(default_factory=dict)
    settled: list[Session] = field(default_factory=list)
    total_earned_usd: float = 0.0
    total_viewer_seconds: float = 0.0
    total_settled_onchain_usdc: float = 0.0

    def join(self, user_id: str, username: str, ts: datetime) -> Session:
        """Registers a new user join event. Handles re-joins thread-safely."""
        with self.lock:
            existing = self.active.get(user_id)
            if existing and not existing.settled:
                # Force settle existing session if they rejoined without parting
                self._settle(existing)
            s = Session(user_id=user_id, username=username, joined_at=ts, last_seen_at=ts)
            self.active[user_id] = s
            return s

    def part(self, user_id: str, ts: datetime) -> Optional[Session]:
        """Registers a user parting event."""
        with self.lock:
            s = self.active.get(user_id)
            if not s:
                return None
            s.parted_at = ts
            self._settle(s)
            return s

    def _settle(self, s: Session) -> None:
        """Helper to archive a finished session in the settled list."""
        s.settled = True
        owed = s.owed_usd
        self.total_earned_usd += owed
        self.total_viewer_seconds += s.duration_sec
        self.settled.append(s)
        self.active.pop(s.user_id, None)
        if len(self.settled) > 500:
            self.settled = self.settled[-500:]

    def reap_stale(self) -> list[Session]:
        """Identifies active sessions that went silent and auto-settles them."""
        now = datetime.now(timezone.utc)
        reaped: list[Session] = []
        with self.lock:
            stale_ids = [
                uid for uid, s in self.active.items()
                if (now - (s.last_seen_at or s.joined_at)).total_seconds() > config.STALE_SESSION_TIMEOUT_SEC
            ]
            for uid in stale_ids:
                s = self.active[uid]
                s.parted_at = now
                self._settle(s)
                reaped.append(s)
        return reaped

    def snapshot(self) -> dict:
        """Provides a JSON-serializable snapshot of the ledger for the dashboard."""
        with self.lock:
            return {
                "rate_per_second_usd": config.RATE_PER_SECOND,
                "streamer_wallet": config.STREAMER_WALLET,
                "usdc_contract": config.USDC_ARC_ADDRESS,
                "usdc_chain_id": config.USDC_CHAIN_ID,
                "tiers": config.TIERS,
                "active_viewers": [
                    {
                        "user_id": s.user_id,
                        "username": s.username,
                        "joined_at": s.joined_at.isoformat(),
                        "duration_sec": round(s.duration_sec, 2),
                        "owed_usd": round(s.owed_usd, 6),
                        "auth_status": s.auth_status.value,
                        "tier_cents": s.tier_cents,
                        "auth_request_id": s.auth_request_id,
                    }
                    for s in self.active.values()
                ],
                "active_count": len(self.active),
                "settled_count": len(self.settled),
                "total_earned_usd": round(self.total_earned_usd, 6),
                "total_viewer_seconds": round(self.total_viewer_seconds, 2),
                "total_settled_onchain_usdc": round(self.total_settled_onchain_usdc, 6),
                "recent_settlements": [
                    {
                        "username": s.username,
                        "duration_sec": round(s.duration_sec, 2),
                        "owed_usd": round(s.owed_usd, 6),
                        "auth_status": s.auth_status.value,
                        "tier_cents": s.tier_cents,
                        "amount_charged_usdc": s.amount_charged_usdc,
                        "tx_hash": s.tx_hash,
                        "joined_at": s.joined_at.isoformat(),
                        "parted_at": s.parted_at.isoformat() if s.parted_at else None,
                        "settled_at": s.settled_at.isoformat() if s.settled_at else None,
                    }
                    for s in reversed(self.settled[-20:])
                ],
            }
