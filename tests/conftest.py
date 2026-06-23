"""Test fixtures for ctfquant."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from ctfquant.config import RiskParams
from ctfquant.risk_engine import GateContext
from ctfquant.types import (
    AccountState,
    ContractSpec,
    FundingInfo,
    MarketSnapshot,
    OrderType,
    ProposedOrder,
    Side,
    Stage,
    TimeInForce,
)


@pytest.fixture
def params() -> RiskParams:
    return RiskParams(nav_usd=200.0)


@pytest.fixture
def account() -> AccountState:
    return AccountState(
        ts=datetime.now(timezone.utc),
        nav_usd=200.0,
        available_usd=180.0,
        open_positions={},
        daily_realised_pnl_usd=0.0,
        intraday_high_watermark_usd=200.0,
        rolling_7d_high_watermark_usd=200.0,
        all_time_high_watermark_usd=200.0,
    )


@pytest.fixture
def contract() -> ContractSpec:
    # ADA-USDT-style: 1 ct = 10 ADA face value, very approximate
    return ContractSpec(
        symbol="ADA-USDT",
        contract_code="ADA-USDT",
        contract_size=10.0,
        price_tick=0.0001,
    )


@pytest.fixture
def snapshot(contract: ContractSpec) -> MarketSnapshot:
    now = datetime.now(timezone.utc)
    return MarketSnapshot(
        symbol="ADA-USDT",
        ts=now,
        bid=0.4998,
        ask=0.5002,
        mid=0.5,
        last=0.5,
        mark=0.5,
        funding=FundingInfo(
            symbol="ADA-USDT",
            funding_interval_hours=8,
            next_funding_ts=now + timedelta(hours=2),
            current_funding_per_period=0.005,    # 0.5%/8h ≈ 0.0625%/h
            estimated_funding_per_period=0.005,
        ),
        contract=contract,
        top_of_book_depth_usd=50_000.0,
        last_tick_age_s=0.5,
    )


@pytest.fixture
def universe() -> frozenset[str]:
    return frozenset({"ADA-USDT", "DOT-USDT", "ALGO-USDT", "LINK-USDT"})


@pytest.fixture
def ctx(snapshot: MarketSnapshot, account: AccountState, universe: frozenset[str]) -> GateContext:
    return GateContext(
        snapshot=snapshot,
        account=account,
        universe=universe,
        daily_loss_budget_remaining_usd=16.0,
        venue_health_ok=True,
        last_tick_age_s=0.5,
        quote_stable_price=1.0,
        hitl_approved=True,
    )


def make_order(
    *,
    symbol: str = "ADA-USDT",
    side: Side = Side.SELL,
    size_contracts: int = 1,
    price: float = 0.5,
    venue: str = "HTX",
    approval_required: list[str] | None = None,
) -> ProposedOrder:
    decision_hash = hashlib.sha256(f"{symbol}:{side}:{size_contracts}".encode()).hexdigest()
    coid = hashlib.sha256(f"strat-1|{decision_hash}|{datetime.now().timestamp()}".encode()).hexdigest()
    return ProposedOrder(
        client_order_id=coid,
        strategy_id="strat-1",
        decision_hash=decision_hash,
        venue=venue,                                          # type: ignore[arg-type]
        symbol=symbol,
        side=side,
        size_usd=size_contracts * 10 * price,
        size_contracts=size_contracts,
        order_type=OrderType.POST_ONLY,
        price=price,
        tif=TimeInForce.POST_ONLY,
        max_slippage_bps=20,
        ttl_seconds=60,
        evidence_ids=["evt-1"],
        stage=Stage.MICRO,
        approval_required=approval_required or ["risk_engine", "hitl"],
    )
