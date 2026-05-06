"""Tier -1 micro-experimental parameters for the $200 NAV HTX experiment.

These values are the runtime defaults loaded by every other module. They
are intentionally tighter than any spec tier because at $200 NAV the
system cannot recover from large drawdowns within a v0 experiment
window.

Override via env vars (see .env.example).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True, slots=True)
class RiskParams:
    """Tier -1 (micro experimental). Hard caps; never overridden by inputs."""

    nav_usd: float = 200.0

    # sizing
    max_position_pct_nav: float = 0.10        # 10% NAV per leg ($20)
    max_concurrent_positions: int = 2
    max_gross_pct_nav: float = 0.20           # 2 × 10%

    # loss budgets
    per_trade_loss_cap_pct_nav: float = 0.03  # 3% NAV ($6)
    daily_loss_limit_pct_nav: float = 0.08    # 8% NAV ($16)

    # drawdown kills (peak-to-trough vs NAV high-water-mark)
    intraday_dd_kill_pct: float = 0.08        # -8%
    rolling_7d_dd_kill_pct: float = 0.20      # -20%
    peak_to_trough_dd_kill_pct: float = 0.50  # -50%, full halt

    # leverage
    max_leverage: float = 1.0

    # funding entry (per-hour, normalised across funding-interval differences)
    funding_entry_threshold_per_hour: float = 0.0005   # 0.05%/h ≈ 0.4%/8h
    funding_kill_threshold_per_hour: float = 0.001     # |f/h|>0.1%/h halts new entries

    # microstructure / venue health
    max_ref_price_deviation_bps: float = 50.0          # order price within 50bps of mid
    stable_depeg_floor: float = 0.995
    stable_depeg_ceiling: float = 1.005
    venue_5xx_rate_60s_kill: float = 0.05              # >5% over 60s
    ws_disconnect_kill_seconds: float = 30.0
    order_reject_per_min_kill: float = 5.0
    tick_freshness_max_seconds: float = 5.0
    tick_to_order_p99_multiplier_kill: float = 2.0     # vs baseline
    slippage_zscore_kill: float = 4.0

    # universe filtering at startup
    universe_max_contract_pct_nav: float = 0.10        # 1 ct ≤ 10% NAV
    universe_min_24h_volume_usd: float = 5_000_000.0   # liquidity floor

    # HITL
    hitl_mandatory_first_n_trades: int = 999_999       # always on in v0
    hitl_timeout_seconds: float = 600.0                # 10 min, default reject

    # operational
    funding_boundary_blackout_seconds: float = 60.0    # avoid order placement ±60s of funding


@dataclass(frozen=True, slots=True)
class VenueParams:
    """HTX endpoints and rate-limit budget."""

    host_usdt_m: str = field(default_factory=lambda: _str("HTX_USDT_M_HOST", "api.hbdm.com"))
    ws_public_url: str = "wss://api.hbdm.com/linear-swap-ws"
    ws_private_url: str = "wss://api.hbdm.com/linear-swap-notification"

    # per-UID rate limits (USDT-M private)
    private_rate_limit_per_3s: int = 144
    trigger_rate_limit_per_s: int = 5
    public_rate_limit_per_10s: int = 100

    # request timeouts
    rest_connect_timeout_s: float = 5.0
    rest_read_timeout_s: float = 10.0


@dataclass(frozen=True, slots=True)
class AppParams:
    nav_usd: float = field(default_factory=lambda: _float("CTFQUANT_NAV_USD", 200.0))
    tier: str = field(default_factory=lambda: _str("CTFQUANT_TIER", "tier_minus_1"))
    dry_run: bool = field(default_factory=lambda: _bool("CTFQUANT_DRY_RUN", True))
    log_level: str = field(default_factory=lambda: _str("CTFQUANT_LOG_LEVEL", "INFO"))
    db_path: Path = field(
        default_factory=lambda: Path(_str("CTFQUANT_DB_PATH", "./var/state.db"))
    )

    htx_access_key: str = field(default_factory=lambda: _str("HTX_ACCESS_KEY", ""))
    htx_secret_key: str = field(default_factory=lambda: _str("HTX_SECRET_KEY", ""))
    telegram_bot_token: str = field(default_factory=lambda: _str("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _str("TELEGRAM_CHAT_ID", ""))


def load() -> tuple[AppParams, RiskParams, VenueParams]:
    app = AppParams()
    # nav from env propagates into RiskParams default
    risk = RiskParams(nav_usd=app.nav_usd)
    venue = VenueParams()
    return app, risk, venue
