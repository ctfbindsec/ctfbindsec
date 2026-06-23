"""Typed contracts mirroring schemas/ where applicable.

These are pydantic models for runtime validation. They MUST stay in
shape-compatible sync with schemas/proposed_order.schema.json and
schemas/strategy_report.schema.json.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    POST_ONLY = "post_only"
    IOC = "ioc"


class TimeInForce(str, Enum):
    GTC = "GTC"
    IOC = "IOC"
    POST_ONLY = "POST_ONLY"


class Stage(str, Enum):
    PAPER = "paper"
    MICRO = "micro"
    RAMPED = "ramped"
    FULL = "full"


class OrderStatus(str, Enum):
    PROPOSED = "proposed"
    HITL_PENDING = "hitl_pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    KILLED = "killed"
    EXPIRED = "expired"


class ProposedOrder(BaseModel):
    """Cross-component contract. Validated at every boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_order_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_id: str = Field(min_length=1)
    decision_hash: str = Field(min_length=1)
    venue: Literal["HTX"]
    symbol: str = Field(min_length=1)
    side: Side
    size_usd: float = Field(gt=0)
    size_contracts: int = Field(gt=0)
    order_type: OrderType
    price: float = Field(gt=0)
    tif: TimeInForce
    reduce_only: bool = False
    max_slippage_bps: int = Field(ge=0, le=100)
    ttl_seconds: int = Field(ge=0)
    kill_switch_inheritance: Literal[True] = True
    evidence_ids: list[str] = Field(min_length=1)
    stage: Stage
    approval_required: list[Literal["risk_engine", "compliance", "hitl"]]
    notes: str = Field(default="", max_length=2000)

    @field_validator("approval_required")
    @classmethod
    def must_include_risk_engine(cls, v: list[str]) -> list[str]:
        if "risk_engine" not in v:
            raise ValueError("approval_required must include 'risk_engine'")
        return v


class FundingInfo(BaseModel):
    """Per-contract funding metadata, normalised to per-hour."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    funding_interval_hours: int  # 8 for most, 4 for many alts
    next_funding_ts: datetime
    current_funding_per_period: float  # raw rate as quoted
    estimated_funding_per_period: float

    @property
    def current_funding_per_hour(self) -> float:
        return self.current_funding_per_period / self.funding_interval_hours

    @property
    def estimated_funding_per_hour(self) -> float:
        return self.estimated_funding_per_period / self.funding_interval_hours


class ContractSpec(BaseModel):
    """Subset of HTX swap_contract_info we use for sizing decisions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str            # e.g. ADA-USDT
    contract_code: str     # e.g. ADA-USDT
    contract_size: float   # face value of 1 contract in base coin
    price_tick: float
    qty_tick: int = 1
    min_qty: int = 1


class MarketSnapshot(BaseModel):
    """A single point-in-time view sufficient for risk gating."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    ts: datetime
    bid: float
    ask: float
    mid: float
    last: float
    mark: float
    funding: FundingInfo
    contract: ContractSpec
    top_of_book_depth_usd: float = 0.0
    last_tick_age_s: float = 0.0


class AccountState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: datetime
    nav_usd: float
    available_usd: float
    open_positions: dict[str, float]    # symbol -> signed contracts
    daily_realised_pnl_usd: float = 0.0
    intraday_high_watermark_usd: float = 0.0
    rolling_7d_high_watermark_usd: float = 0.0
    all_time_high_watermark_usd: float = 0.0


class RiskVerdict(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    SIZE_CUT = "size_cut"


class RiskOutcome(BaseModel):
    """Output of every Risk Engine evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str
    verdict: RiskVerdict
    binding_constraints: list[str]
    size_after_contracts: int
    size_before_contracts: int
    notes: str = ""
    ts: datetime = Field(default_factory=utcnow)
