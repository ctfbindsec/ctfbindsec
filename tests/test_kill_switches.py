"""Kill-switch tests."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from ctfquant.config import RiskParams
from ctfquant.kill_switches import KillSwitchState, evaluate, reset
from ctfquant.types import AccountState


def _account(nav: float, hwm: float = 200.0, hwm_7d: float = 200.0, atl_hwm: float = 200.0) -> AccountState:
    return AccountState(
        ts=datetime.now(timezone.utc),
        nav_usd=nav,
        available_usd=nav * 0.9,
        open_positions={},
        daily_realised_pnl_usd=0.0,
        intraday_high_watermark_usd=hwm,
        rolling_7d_high_watermark_usd=hwm_7d,
        all_time_high_watermark_usd=atl_hwm,
    )


def test_no_trip_under_normal_conditions():
    state = KillSwitchState()
    state.venue_health.record_ws_message()
    out = evaluate(state, _account(200.0), RiskParams(nav_usd=200.0), 1.0, 0.0)
    assert out is None


def test_intraday_dd_kill_trips_at_threshold():
    state = KillSwitchState()
    state.venue_health.record_ws_message()
    params = RiskParams(nav_usd=200.0)
    # 8% drop from 200 to 184
    out = evaluate(state, _account(184.0, hwm=200.0), params, 1.0, 0.0)
    assert out is not None
    assert "intraday_dd" in out


def test_peak_to_trough_kill_trips_at_threshold():
    state = KillSwitchState()
    state.venue_health.record_ws_message()
    params = RiskParams(nav_usd=200.0)
    # 50% peak-to-trough kill
    out = evaluate(state, _account(100.0, hwm=100.0, hwm_7d=100.0, atl_hwm=200.0), params, 1.0, 0.0)
    assert out is not None
    assert "peak_to_trough" in out


def test_stable_depeg_trips():
    state = KillSwitchState()
    state.venue_health.record_ws_message()
    out = evaluate(state, _account(200.0), RiskParams(nav_usd=200.0), 0.99, 0.0)
    assert out is not None and "stable_depeg" in out


def test_funding_extreme_open_trips():
    state = KillSwitchState()
    state.venue_health.record_ws_message()
    params = RiskParams(nav_usd=200.0)
    # 0.2%/h is well above the 0.1%/h kill threshold
    out = evaluate(state, _account(200.0), params, 1.0, 0.002)
    assert out is not None and "funding_extreme_open" in out


def test_ws_silence_trips():
    state = KillSwitchState()
    # last_ws_message_at is initialised to now; backdate it
    state.venue_health.last_ws_message_at = time.monotonic() - 60.0
    out = evaluate(state, _account(200.0), RiskParams(nav_usd=200.0), 1.0, 0.0)
    assert out is not None and "ws_silence" in out


def test_once_tripped_stays_tripped():
    state = KillSwitchState()
    state.venue_health.record_ws_message()
    params = RiskParams(nav_usd=200.0)
    first = evaluate(state, _account(184.0), params, 1.0, 0.0)
    assert first is not None
    # even with healthy account afterwards, still tripped
    second = evaluate(state, _account(200.0), params, 1.0, 0.0)
    assert second == first


def test_reset_clears_trip():
    state = KillSwitchState()
    state.venue_health.record_ws_message()
    params = RiskParams(nav_usd=200.0)
    evaluate(state, _account(184.0), params, 1.0, 0.0)
    assert state.tripped_reason is not None
    reset(state, by="operator:test")
    assert state.tripped_reason is None
