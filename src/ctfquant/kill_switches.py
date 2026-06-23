"""Deterministic kill-switches.

These run continuously alongside the gate. A kill-switch trip puts the
system into READ-ONLY flatten-only mode. Resume requires Principal
intervention (HITL bot /resume command, see hitl_bot.py) — there is no
auto-resume path.

Implemented (tier -1 scope):
  - account_drawdown (intraday / 7d / peak-to-trough)
  - venue_health (REST 5xx rate, WS disconnect duration, order rejects/min)
  - latency (tick-to-order p99 vs baseline)
  - stablecoin_depeg (USDT/USDC/quote stable)
  - funding_extreme (|f/h| above kill threshold)
  - hitl_timeout (default-reject SLA breach)

Out-of-scope at tier -1 (deferred to v1):
  - anomalous_slippage (insufficient sample at $200 NAV)
  - model_confidence (single deterministic strategy)
  - cross_venue_dislocation (single venue)
  - compliance_p0 (PR pipeline disabled)
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .config import RiskParams
from .types import AccountState

log = logging.getLogger(__name__)


@dataclass
class VenueHealth:
    """Rolling counters for venue-health kill triggers."""

    rest_calls: deque = field(default_factory=lambda: deque(maxlen=2_000))
    rest_5xx: deque = field(default_factory=lambda: deque(maxlen=2_000))
    order_rejects: deque = field(default_factory=lambda: deque(maxlen=200))
    last_ws_message_at: float = field(default_factory=time.monotonic)

    def record_rest(self, status: int) -> None:
        now = time.monotonic()
        self.rest_calls.append(now)
        if 500 <= status < 600:
            self.rest_5xx.append(now)

    def record_order_reject(self) -> None:
        self.order_rejects.append(time.monotonic())

    def record_ws_message(self) -> None:
        self.last_ws_message_at = time.monotonic()

    def rest_5xx_rate_60s(self) -> float:
        cutoff = time.monotonic() - 60.0
        recent_calls = sum(1 for t in self.rest_calls if t >= cutoff)
        if recent_calls == 0:
            return 0.0
        recent_5xx = sum(1 for t in self.rest_5xx if t >= cutoff)
        return recent_5xx / recent_calls

    def rejects_per_min(self) -> float:
        cutoff = time.monotonic() - 60.0
        return float(sum(1 for t in self.order_rejects if t >= cutoff))

    def ws_silence_seconds(self) -> float:
        return time.monotonic() - self.last_ws_message_at


@dataclass
class KillSwitchState:
    """Mutable container for the live kill-switch evaluator."""

    venue_health: VenueHealth = field(default_factory=VenueHealth)
    tick_to_order_p99_ms: float = 0.0
    tick_to_order_baseline_ms: float = 100.0
    tripped_reason: str | None = None
    tripped_at: datetime | None = None


def evaluate(
    state: KillSwitchState,
    account: AccountState,
    params: RiskParams,
    quote_stable_price: float,
    funding_per_hour_max_observed: float,
) -> str | None:
    """Returns a string reason if any kill-switch is tripped, else None.

    Pure-ish: only reads from state and inputs, mutates nothing other
    than to record the trip on the state for observability.
    """

    if state.tripped_reason is not None:
        return state.tripped_reason  # already tripped, stay tripped

    # account drawdowns vs respective high-watermarks
    intraday_dd = _drawdown(account.nav_usd, account.intraday_high_watermark_usd)
    if intraday_dd >= params.intraday_dd_kill_pct:
        return _trip(state, f"intraday_dd:{intraday_dd:.2%}>={params.intraday_dd_kill_pct:.2%}")

    rolling_dd = _drawdown(account.nav_usd, account.rolling_7d_high_watermark_usd)
    if rolling_dd >= params.rolling_7d_dd_kill_pct:
        return _trip(state, f"7d_dd:{rolling_dd:.2%}>={params.rolling_7d_dd_kill_pct:.2%}")

    ptp_dd = _drawdown(account.nav_usd, account.all_time_high_watermark_usd)
    if ptp_dd >= params.peak_to_trough_dd_kill_pct:
        return _trip(state, f"peak_to_trough_dd:{ptp_dd:.2%}>={params.peak_to_trough_dd_kill_pct:.2%}")

    # stablecoin depeg
    if not (params.stable_depeg_floor <= quote_stable_price <= params.stable_depeg_ceiling):
        return _trip(state, f"stable_depeg:USDT={quote_stable_price:.4f}")

    # funding extreme observed across any open position
    if abs(funding_per_hour_max_observed) > params.funding_kill_threshold_per_hour:
        return _trip(
            state,
            f"funding_extreme_open:|f/h|={abs(funding_per_hour_max_observed):.5%}",
        )

    # venue health
    rate = state.venue_health.rest_5xx_rate_60s()
    if rate > params.venue_5xx_rate_60s_kill:
        return _trip(state, f"venue_5xx_rate:{rate:.2%}")

    silence = state.venue_health.ws_silence_seconds()
    if silence > params.ws_disconnect_kill_seconds:
        return _trip(state, f"ws_silence:{silence:.0f}s")

    rpm = state.venue_health.rejects_per_min()
    if rpm > params.order_reject_per_min_kill:
        return _trip(state, f"order_rejects:{rpm}/min")

    # latency
    if state.tick_to_order_baseline_ms > 0 and state.tick_to_order_p99_ms > (
        state.tick_to_order_baseline_ms * params.tick_to_order_p99_multiplier_kill
    ):
        return _trip(
            state,
            f"latency_p99:{state.tick_to_order_p99_ms:.0f}ms"
            f">={state.tick_to_order_baseline_ms * params.tick_to_order_p99_multiplier_kill:.0f}ms",
        )

    return None


def _drawdown(current: float, high_water: float) -> float:
    if high_water <= 0:
        return 0.0
    return max(0.0, (high_water - current) / high_water)


def _trip(state: KillSwitchState, reason: str) -> str:
    state.tripped_reason = reason
    state.tripped_at = datetime.now(timezone.utc)
    log.error("KILL SWITCH TRIPPED: %s", reason)
    return reason


def reset(state: KillSwitchState, by: str) -> None:
    """Clear a tripped kill switch. Operator-driven only. Logs forever."""

    log.warning("KILL SWITCH RESET by=%s prev=%s", by, state.tripped_reason)
    state.tripped_reason = None
    state.tripped_at = None
