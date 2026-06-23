"""Funding-rate capture strategy (tier -1).

Hypothesis: when |funding/h| > entry_threshold and is paid by the longs
(positive funding) or shorts (negative funding), a delta-neutral or
single-leg position can capture the funding payment net of fees and
basis drift.

Tier -1 simplification: single-leg only (no spot hedge — too expensive
at $200 NAV). Open opposite-side perp position when funding is extreme.
This carries directional spot risk; the per-trade and daily loss caps
contain it. Hold across at most one funding boundary, then flatten.

The strategy emits ProposedOrder objects only. It does not place orders;
the orchestrator routes through Risk → HITL → Execution.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from .config import RiskParams
from .types import (
    AccountState,
    MarketSnapshot,
    OrderType,
    ProposedOrder,
    Side,
    Stage,
    TimeInForce,
)

log = logging.getLogger(__name__)


@dataclass
class StrategyState:
    """Per-symbol strategy memory."""

    last_proposal_ts: dict[str, float] = field(default_factory=dict)
    proposal_cooldown_s: float = 60.0   # don't re-propose same symbol within 60s


def funding_per_hour(snap: MarketSnapshot) -> float:
    return snap.funding.current_funding_per_hour


def proposed_side(snap: MarketSnapshot) -> Side | None:
    """Return the leg we want to open. None if funding does not justify a trade.

    Positive funding → longs pay shorts → we want to be SHORT to receive.
    Negative funding → shorts pay longs → we want to be LONG to receive.
    """

    f_h = funding_per_hour(snap)
    if f_h > 0:
        return Side.SELL
    if f_h < 0:
        return Side.BUY
    return None


def time_to_funding_s(snap: MarketSnapshot) -> float:
    return max(0.0, (snap.funding.next_funding_ts - datetime.now(timezone.utc)).total_seconds())


def in_funding_blackout(snap: MarketSnapshot, blackout_s: float) -> bool:
    """We avoid placing orders ±blackout_s seconds around the funding boundary.

    HTX micro-halts settlement around boundaries; new orders at that
    moment risk reject or unexpected fills.

    The window is [boundary - blackout_s, boundary + blackout_s] for the
    NEXT boundary AND the PREVIOUS boundary. We approximate previous-
    boundary distance as (interval - secs_to_next).
    """

    secs_to_next = time_to_funding_s(snap)
    interval_s = snap.funding.funding_interval_hours * 3600
    secs_since_prev = max(0.0, interval_s - secs_to_next)
    return secs_to_next < blackout_s or secs_since_prev < blackout_s


def propose(
    snapshots: Iterable[MarketSnapshot],
    account: AccountState,
    params: RiskParams,
    state: StrategyState,
    *,
    strategy_id: str = "funding_capture_v0",
) -> list[ProposedOrder]:
    """Walk the universe and emit one proposed order per qualifying contract."""

    out: list[ProposedOrder] = []
    now_mono = time.monotonic()

    open_count = sum(1 for v in account.open_positions.values() if v != 0)

    for snap in snapshots:
        if open_count >= params.max_concurrent_positions and snap.symbol not in account.open_positions:
            continue

        f_h = funding_per_hour(snap)
        if abs(f_h) < params.funding_entry_threshold_per_hour:
            continue
        if abs(f_h) > params.funding_kill_threshold_per_hour:
            log.warning(
                "skipping %s: |f/h|=%.5f exceeds kill threshold", snap.symbol, abs(f_h)
            )
            continue

        if in_funding_blackout(snap, params.funding_boundary_blackout_seconds):
            log.debug("skipping %s: inside funding blackout", snap.symbol)
            continue

        last = state.last_proposal_ts.get(snap.symbol, 0.0)
        if now_mono - last < state.proposal_cooldown_s:
            continue

        side = proposed_side(snap)
        if side is None:
            continue

        # 1 contract notional in USD
        ct_notional = snap.contract.contract_size * snap.mid
        cap_usd = params.max_position_pct_nav * account.nav_usd
        max_contracts = max(1, int(cap_usd // ct_notional))
        size_contracts = max_contracts

        # post-only at top-of-book to be a maker; the gate enforces ≤50bps deviation
        price = snap.bid if side == Side.SELL else snap.ask
        if price <= 0:
            continue

        decision_payload = (
            f"{snap.symbol}|{side.value}|{size_contracts}|"
            f"{f_h:.6f}|{snap.funding.next_funding_ts.isoformat()}"
        )
        decision_hash = hashlib.sha256(decision_payload.encode()).hexdigest()
        time_bucket = int(time.time()) // 60
        coid_input = f"{strategy_id}|{decision_hash}|{time_bucket}".encode()
        client_order_id = hashlib.sha256(coid_input).hexdigest()

        order = ProposedOrder(
            client_order_id=client_order_id,
            strategy_id=strategy_id,
            decision_hash=decision_hash,
            venue="HTX",                                 # type: ignore[arg-type]
            symbol=snap.symbol,
            side=side,
            size_usd=size_contracts * ct_notional,
            size_contracts=size_contracts,
            order_type=OrderType.POST_ONLY,
            price=price,
            tif=TimeInForce.POST_ONLY,
            reduce_only=False,
            max_slippage_bps=20,
            ttl_seconds=120,
            evidence_ids=[f"funding_snapshot:{snap.symbol}:{snap.ts.isoformat()}"],
            stage=Stage.MICRO,
            approval_required=["risk_engine", "hitl"],
            notes=(
                f"funding capture: f/h={f_h*100:.4f}% "
                f"(={snap.funding.current_funding_per_period*100:.4f}%/{snap.funding.funding_interval_hours}h), "
                f"next funding in {time_to_funding_s(snap)/60:.0f}m"
            ),
        )
        out.append(order)
        state.last_proposal_ts[snap.symbol] = now_mono

    return out
