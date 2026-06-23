"""Risk Engine tests.

These are the most important tests in the codebase. Every numeric
invariant from the spec gets a positive (rejects bad input) test.
Property tests use hypothesis to fuzz adversarial size/price/funding
inputs.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from ctfquant.config import RiskParams
from ctfquant.risk_engine import GateContext, evaluate
from ctfquant.types import RiskVerdict, Side

from .conftest import make_order


def test_basic_approval(ctx, params):
    order = make_order(size_contracts=1, price=0.5)
    out = evaluate(order, ctx, params)
    assert out.verdict == RiskVerdict.APPROVED, out.binding_constraints


def test_universe_whitelist_rejects(ctx, params):
    order = make_order(symbol="DOGE-USDT")
    out = evaluate(order, ctx, params)
    assert out.verdict == RiskVerdict.REJECTED
    assert any("universe" in c for c in out.binding_constraints)


def test_stable_depeg_floor_rejects(ctx, params):
    bad_ctx = dataclasses.replace(ctx, quote_stable_price=0.99)
    order = make_order()
    out = evaluate(order, bad_ctx, params)
    assert out.verdict == RiskVerdict.REJECTED
    assert any("stable_depeg" in c for c in out.binding_constraints)


def test_stable_depeg_ceiling_rejects(ctx, params):
    bad_ctx = dataclasses.replace(ctx, quote_stable_price=1.02)
    order = make_order()
    out = evaluate(order, bad_ctx, params)
    assert out.verdict == RiskVerdict.REJECTED
    assert any("stable_depeg" in c for c in out.binding_constraints)


def test_stale_tick_rejects(ctx, params):
    bad_ctx = dataclasses.replace(ctx, last_tick_age_s=10.0)
    order = make_order()
    out = evaluate(order, bad_ctx, params)
    assert out.verdict == RiskVerdict.REJECTED
    assert any("stale_tick" in c for c in out.binding_constraints)


def test_unhealthy_venue_rejects(ctx, params):
    bad_ctx = dataclasses.replace(ctx, venue_health_ok=False)
    order = make_order()
    out = evaluate(order, bad_ctx, params)
    assert out.verdict == RiskVerdict.REJECTED


def test_price_deviation_rejects(ctx, params):
    # mid is 0.5; +60bps = 0.503
    order = make_order(price=0.503)
    out = evaluate(order, ctx, params)
    assert out.verdict == RiskVerdict.REJECTED
    assert any("price_deviation" in c for c in out.binding_constraints)


def test_extreme_funding_rejects(ctx, params):
    # current_funding_per_hour from fixture is 0.0625%/h. Bump it to 0.2%/h.
    snap = ctx.snapshot
    funding = snap.funding.model_copy(update={"current_funding_per_period": 0.020})  # 0.020/8 = 0.25%/h
    snap2 = snap.model_copy(update={"funding": funding})
    bad_ctx = dataclasses.replace(ctx, snapshot=snap2)
    order = make_order()
    out = evaluate(order, bad_ctx, params)
    assert out.verdict == RiskVerdict.REJECTED
    assert any("funding_extreme" in c for c in out.binding_constraints)


def test_funding_normalization_4h_vs_8h(ctx, params):
    """Same per-hour funding rate, different funding intervals: same verdict.

    This is the bug class Copilot flagged on the spec — quote rate of
    0.20%/8h and 0.10%/4h are the same per-hour rate (0.025%/h), and the
    gate must treat them identically.
    """

    base_snap = ctx.snapshot
    base_contract = base_snap.contract

    f8 = base_snap.funding.model_copy(update={
        "funding_interval_hours": 8,
        "current_funding_per_period": 0.0020,      # 0.20%/8h
        "estimated_funding_per_period": 0.0020,
    })
    f4 = base_snap.funding.model_copy(update={
        "funding_interval_hours": 4,
        "current_funding_per_period": 0.0010,      # 0.10%/4h == same per hour
        "estimated_funding_per_period": 0.0010,
    })

    s8 = base_snap.model_copy(update={"funding": f8})
    s4 = base_snap.model_copy(update={"funding": f4})

    out8 = evaluate(make_order(), dataclasses.replace(ctx, snapshot=s8), params)
    out4 = evaluate(make_order(), dataclasses.replace(ctx, snapshot=s4), params)
    assert out8.verdict == out4.verdict, (out8, out4)


def test_position_too_large_size_cut(ctx, params):
    # 1 ct = 10 ADA × $0.5 = $5; cap is 10% × $200 = $20 → 4 contracts max.
    order = make_order(size_contracts=10)
    out = evaluate(order, ctx, params)
    assert out.verdict == RiskVerdict.SIZE_CUT
    assert out.size_after_contracts == 4
    assert any("position_cap" in c for c in out.binding_constraints)


def test_contract_too_large_rejects(ctx, params):
    # synthetic huge-contract market: 1 ct = 100 ADA × $0.5 = $50, cap $20 → 0
    snap = ctx.snapshot
    contract2 = snap.contract.model_copy(update={"contract_size": 100.0})
    snap2 = snap.model_copy(update={"contract": contract2})
    bad_ctx = dataclasses.replace(ctx, snapshot=snap2)
    out = evaluate(make_order(size_contracts=1), bad_ctx, params)
    assert out.verdict == RiskVerdict.REJECTED
    assert any("contract_too_large" in c for c in out.binding_constraints)


def test_concurrent_positions_cap(ctx, params):
    account2 = ctx.account.model_copy(update={
        "open_positions": {"DOT-USDT": 1, "LINK-USDT": -1}
    })
    bad_ctx = dataclasses.replace(ctx, account=account2)
    out = evaluate(make_order(symbol="ADA-USDT"), bad_ctx, params)
    assert out.verdict == RiskVerdict.REJECTED
    assert any("concurrent_positions_cap" in c for c in out.binding_constraints)


def test_existing_position_does_not_trigger_concurrent_cap(ctx, params):
    # Two positions, but adding to one already open does not increase count.
    account2 = ctx.account.model_copy(update={
        "open_positions": {"ADA-USDT": -1, "DOT-USDT": 1}
    })
    ctx2 = dataclasses.replace(ctx, account=account2)
    out = evaluate(make_order(symbol="ADA-USDT", side=Side.SELL), ctx2, params)
    # may or may not approve (post-trade pos cap), but not blocked on count
    assert not any("concurrent_positions_cap" in c for c in out.binding_constraints)


def test_insufficient_collateral_rejects(ctx, params):
    poor_account = ctx.account.model_copy(update={"available_usd": 0.5})
    bad_ctx = dataclasses.replace(ctx, account=poor_account)
    out = evaluate(make_order(size_contracts=1), bad_ctx, params)
    assert out.verdict == RiskVerdict.REJECTED
    assert any("insufficient_collateral" in c for c in out.binding_constraints)


def test_daily_loss_budget_exhausted_rejects(ctx, params):
    bad_ctx = dataclasses.replace(ctx, daily_loss_budget_remaining_usd=0.50)
    out = evaluate(make_order(size_contracts=1), bad_ctx, params)
    assert out.verdict == RiskVerdict.REJECTED
    assert any("daily_loss_budget" in c for c in out.binding_constraints)


def test_hitl_pending_rejected_until_approved(ctx, params):
    not_approved = dataclasses.replace(ctx, hitl_approved=False)
    out = evaluate(make_order(approval_required=["risk_engine", "hitl"]), not_approved, params)
    assert out.verdict == RiskVerdict.REJECTED
    assert any("hitl:pending" in c for c in out.binding_constraints)


def test_kill_switch_inheritance_required(ctx, params):
    """kill_switch_inheritance can't be False on a ProposedOrder — pydantic Literal[True] enforces at construction."""

    from ctfquant.types import OrderType, ProposedOrder, Side, Stage, TimeInForce

    with pytest.raises(Exception):
        ProposedOrder(
            client_order_id="a" * 64,
            strategy_id="s",
            decision_hash="d",
            venue="HTX",                                    # type: ignore[arg-type]
            symbol="ADA-USDT",
            side=Side.SELL,
            size_usd=5.0,
            size_contracts=1,
            order_type=OrderType.POST_ONLY,
            price=0.5,
            tif=TimeInForce.POST_ONLY,
            max_slippage_bps=20,
            ttl_seconds=60,
            kill_switch_inheritance=False,                  # type: ignore[arg-type]
            evidence_ids=["evt-1"],
            stage=Stage.MICRO,
            approval_required=["risk_engine"],              # type: ignore[list-item]
        )


# ---------------- property tests ----------------


@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    contracts=st.integers(min_value=1, max_value=1000),
    price_dev_bps=st.floats(min_value=0, max_value=500, allow_nan=False),
)
def test_property_size_cut_never_exceeds_position_cap(ctx, params, contracts, price_dev_bps):
    """Whatever contract count and price (within ±500bps), the size_after
    must never exceed the per-position cap when the verdict is APPROVED or
    SIZE_CUT."""

    mid = ctx.snapshot.mid
    px = mid * (1 + price_dev_bps / 10_000)
    order = make_order(size_contracts=contracts, price=px)
    out = evaluate(order, ctx, params)
    if out.verdict in (RiskVerdict.APPROVED, RiskVerdict.SIZE_CUT):
        contract_notional = ctx.snapshot.contract.contract_size * mid
        cap_usd = params.max_position_pct_nav * ctx.account.nav_usd
        assert out.size_after_contracts * contract_notional <= cap_usd + 1e-6


@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    funding_per_period=st.floats(min_value=-0.05, max_value=0.05, allow_nan=False),
    interval_hours=st.sampled_from([4, 8]),
)
def test_property_funding_threshold_uses_per_hour_only(
    ctx, params, funding_per_period, interval_hours
):
    """Two funding rates with the same per-hour value (regardless of
    interval) MUST produce identical verdicts."""

    snap = ctx.snapshot
    fa = snap.funding.model_copy(update={
        "funding_interval_hours": interval_hours,
        "current_funding_per_period": funding_per_period,
        "estimated_funding_per_period": funding_per_period,
    })
    # equivalent rate at the alternate interval
    other_interval = 4 if interval_hours == 8 else 8
    fb = snap.funding.model_copy(update={
        "funding_interval_hours": other_interval,
        "current_funding_per_period": funding_per_period * (other_interval / interval_hours),
        "estimated_funding_per_period": funding_per_period * (other_interval / interval_hours),
    })
    sa = snap.model_copy(update={"funding": fa})
    sb = snap.model_copy(update={"funding": fb})
    out_a = evaluate(make_order(), dataclasses.replace(ctx, snapshot=sa), params)
    out_b = evaluate(make_order(), dataclasses.replace(ctx, snapshot=sb), params)
    assert out_a.verdict == out_b.verdict, (
        out_a.binding_constraints,
        out_b.binding_constraints,
    )
