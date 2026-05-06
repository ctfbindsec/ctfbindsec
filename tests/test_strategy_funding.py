"""Strategy funding capture tests."""

from __future__ import annotations

from datetime import timedelta, timezone

from ctfquant.strategy_funding import (
    StrategyState,
    funding_per_hour,
    in_funding_blackout,
    propose,
    proposed_side,
)
from ctfquant.types import Side


def test_funding_per_hour_8h(snapshot):
    snap2 = snapshot.model_copy(update={
        "funding": snapshot.funding.model_copy(update={
            "current_funding_per_period": 0.004,
            "funding_interval_hours": 8,
        })
    })
    assert funding_per_hour(snap2) == 0.004 / 8


def test_proposed_side_sells_on_positive_funding(snapshot):
    snap2 = snapshot.model_copy(update={
        "funding": snapshot.funding.model_copy(update={
            "current_funding_per_period": 0.005,
            "funding_interval_hours": 8,
        })
    })
    assert proposed_side(snap2) == Side.SELL


def test_proposed_side_buys_on_negative_funding(snapshot):
    snap2 = snapshot.model_copy(update={
        "funding": snapshot.funding.model_copy(update={
            "current_funding_per_period": -0.005,
            "funding_interval_hours": 8,
        })
    })
    assert proposed_side(snap2) == Side.BUY


def test_blackout_window_around_boundary(snapshot):
    soon = snapshot.model_copy(update={
        "funding": snapshot.funding.model_copy(update={
            "next_funding_ts": snapshot.funding.next_funding_ts - timedelta(hours=1, minutes=59, seconds=30)
        })
    })
    assert in_funding_blackout(soon, 60.0)


def test_propose_emits_when_threshold_crossed(snapshot, account, params):
    state = StrategyState()
    snap2 = snapshot.model_copy(update={
        "funding": snapshot.funding.model_copy(update={
            "current_funding_per_period": 0.0080,   # 0.10%/h on 8h
            "funding_interval_hours": 8,
        })
    })
    out = propose([snap2], account, params, state)
    assert len(out) == 1
    assert out[0].side == Side.SELL
    assert out[0].symbol == snap2.symbol


def test_propose_skips_below_threshold(snapshot, account, params):
    state = StrategyState()
    snap2 = snapshot.model_copy(update={
        "funding": snapshot.funding.model_copy(update={
            "current_funding_per_period": 0.0001,
            "funding_interval_hours": 8,
        })
    })
    out = propose([snap2], account, params, state)
    assert out == []


def test_propose_skips_above_kill_threshold(snapshot, account, params):
    state = StrategyState()
    snap2 = snapshot.model_copy(update={
        "funding": snapshot.funding.model_copy(update={
            "current_funding_per_period": 0.020,    # 0.25%/h, > 0.1% kill
            "funding_interval_hours": 8,
        })
    })
    out = propose([snap2], account, params, state)
    assert out == []


def test_propose_respects_concurrent_position_cap(snapshot, account, params):
    state = StrategyState()
    full_account = account.model_copy(update={"open_positions": {"DOT-USDT": 1, "LINK-USDT": -1}})
    snap2 = snapshot.model_copy(update={
        "funding": snapshot.funding.model_copy(update={
            "current_funding_per_period": 0.0080,
            "funding_interval_hours": 8,
        })
    })
    out = propose([snap2], full_account, params, state)
    assert out == []


def test_propose_cooldown_prevents_resubmit(snapshot, account, params):
    state = StrategyState(proposal_cooldown_s=10.0)
    snap2 = snapshot.model_copy(update={
        "funding": snapshot.funding.model_copy(update={
            "current_funding_per_period": 0.0080,
            "funding_interval_hours": 8,
        })
    })
    out1 = propose([snap2], account, params, state)
    out2 = propose([snap2], account, params, state)
    assert len(out1) == 1
    assert out2 == []
