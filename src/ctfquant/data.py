"""Market-data ingestion + universe selection.

The Data Intelligence Layer in tier -1 is intentionally thin: HTX public
endpoints, no on-chain feeds, no sentiment, no macro. Anything we read
becomes a typed MarketSnapshot before any other module touches it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import RiskParams, VenueParams
from .types import ContractSpec, MarketSnapshot
from .venue_htx import HTXClient, build_snapshot

log = logging.getLogger(__name__)


@dataclass
class FeedState:
    """Latest known snapshot per symbol; mutated only by data tasks."""

    snapshots: dict[str, MarketSnapshot] = field(default_factory=dict)
    last_msg_at: dict[str, float] = field(default_factory=lambda: defaultdict(float))

    def update_snapshot(self, snap: MarketSnapshot) -> None:
        self.snapshots[snap.symbol] = snap
        self.last_msg_at[snap.symbol] = time.monotonic()

    def get(self, symbol: str) -> MarketSnapshot | None:
        return self.snapshots.get(symbol)

    def age_s(self, symbol: str) -> float:
        last = self.last_msg_at.get(symbol)
        if not last:
            return 1e9
        return time.monotonic() - last


async def select_universe(
    client: HTXClient, params: RiskParams, nav_usd: float
) -> list[ContractSpec]:
    """Pick contracts where 1 ct ≤ universe_max_contract_pct_nav of NAV.

    Approximates 1ct notional via the index price for each candidate.
    """

    contracts = await client.get_contract_info()
    cap_usd = params.universe_max_contract_pct_nav * nav_usd
    out: list[ContractSpec] = []

    # only USDT-quoted, only top-50 by alphabetical (HTX returns hundreds)
    candidates = [c for c in contracts if c.symbol.endswith("-USDT")]
    candidates.sort(key=lambda c: c.symbol)

    for c in candidates:
        try:
            px = await client.get_index(c.contract_code)
        except Exception as e:
            log.debug("skip %s index: %s", c.symbol, e)
            continue
        notional = c.contract_size * px
        if notional <= cap_usd:
            log.info("universe ✓ %-12s 1ct=$%.2f", c.symbol, notional)
            out.append(c)
        if len(out) >= 12:
            break
        await asyncio.sleep(0.05)  # be polite

    return out


async def funding_poller(
    client: HTXClient,
    universe: list[ContractSpec],
    feed: FeedState,
    interval_s: float = 30.0,
) -> None:
    """Poll per-contract funding + index periodically.

    HTX does push funding on a public WS topic, but the cadence is sparse
    enough that REST polling at 30s is simpler, predictable, and well
    inside rate limits.
    """

    by_code = {c.contract_code: c for c in universe}
    while True:
        for code, contract in by_code.items():
            try:
                fp = await client.get_funding(code)
                px = await client.get_index(code)
                # Build a minimal snapshot for risk-gate consumption when no WS yet
                existing = feed.get(contract.symbol)
                bid = existing.bid if existing else px
                ask = existing.ask if existing else px
                last = existing.last if existing else px
                snap = build_snapshot(
                    contract=contract,
                    bid=bid,
                    ask=ask,
                    mark=px,
                    last=last,
                    funding_payload=fp,
                    last_tick_age_s=feed.age_s(contract.symbol),
                )
                feed.update_snapshot(snap)
            except Exception as e:
                log.warning("funding poll %s failed: %s", code, e)
            await asyncio.sleep(0.1)
        await asyncio.sleep(interval_s)


async def book_streamer(
    client: HTXClient,
    universe: list[ContractSpec],
    feed: FeedState,
) -> None:
    """Subscribe to depth.step6 for top-of-book on each universe contract.

    Updates bid/ask on the cached snapshot. Funding metadata is unaffected
    here (set by funding_poller).
    """

    subs = [
        {"sub": f"market.{c.contract_code}.depth.step6", "id": f"depth-{c.contract_code}"}
        for c in universe
    ]

    by_code = {c.contract_code: c for c in universe}

    while True:
        try:
            async for msg in client.stream_public(subs):
                ch = msg.get("ch", "")
                if ".depth." not in ch:
                    continue
                code = ch.split(".")[1]
                contract = by_code.get(code)
                if contract is None:
                    continue
                tick = msg.get("tick") or {}
                bids = tick.get("bids") or []
                asks = tick.get("asks") or []
                if not bids or not asks:
                    continue
                bid = float(bids[0][0])
                ask = float(asks[0][0])
                existing = feed.get(contract.symbol)
                if existing is None:
                    # placeholder — funding_poller will fill funding shortly
                    continue
                snap = existing.model_copy(update={
                    "ts": datetime.now(timezone.utc),
                    "bid": bid,
                    "ask": ask,
                    "mid": (bid + ask) / 2.0,
                    "last": existing.last or (bid + ask) / 2.0,
                    "last_tick_age_s": 0.0,
                })
                feed.update_snapshot(snap)
        except Exception as e:
            log.warning("book streamer disconnected: %s — reconnecting in 2s", e)
            await asyncio.sleep(2.0)
