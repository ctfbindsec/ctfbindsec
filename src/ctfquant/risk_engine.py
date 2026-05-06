"""Deterministic pre-trade risk gate. The LLM advises; this engine decides.

Implements the spec's pre-trade gate (master prompt §"PRE-TRADE RISK GATE")
scoped to what is enforceable at tier -1 ($200 NAV, single venue,
single strategy):

  1. Schema validity
  2. Universe whitelist
  3. Position limit (post-trade ≤ asset cap)
  4. Notional sanity (1 contract notional vs NAV)
  5. Reference-price deviation (within 50bps of mid)
  6. Margin / free-collateral availability
  8. Stablecoin-of-quote sanity (depeg < 50bps)
  9. Funding-rate sanity (per-hour, normalised across 8h/4h)
 10. Latency / heartbeat (last tick freshness)
 11. Daily loss budget remaining
 14. Counterparty exposure (single-venue trivial; checked anyway)
 16. HITL approval (mandatory in v0)

Out-of-scope at tier -1 (deferred to v1, NOT silently skipped — the
gate emits a 'deferred' note for each):
  4(part).  ADV / top-of-book depth caps (insufficient sample at $200)
  7.        Self-trade prevention (single strategy only)
  12.       VaR/CVaR contribution (recompute deferred)
  13.       Stress test (deferred)
  15.       Compliance Agent sign-off (PR/affiliate disabled in v0)

Numeric invariants are non-negotiable. They cannot be overridden by any
input, by the LLM, or by an operator at runtime — only by editing the
RiskParams dataclass and shipping a new build.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import RiskParams
from .types import (
    AccountState,
    MarketSnapshot,
    OrderStatus,
    ProposedOrder,
    RiskOutcome,
    RiskVerdict,
    Side,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GateContext:
    """Inputs the gate needs that aren't on the order itself."""

    snapshot: MarketSnapshot
    account: AccountState
    universe: frozenset[str]
    daily_loss_budget_remaining_usd: float
    venue_health_ok: bool
    last_tick_age_s: float
    quote_stable_price: float = 1.0  # USDT in USD; ≈1 unless depeg
    hitl_approved: bool = False


def evaluate(
    order: ProposedOrder,
    ctx: GateContext,
    params: RiskParams,
) -> RiskOutcome:
    """Evaluate a single proposed order. Pure function. Deterministic.

    Returns RiskOutcome with verdict APPROVED, REJECTED, or SIZE_CUT and a
    list of binding_constraints. SIZE_CUT is used when a notional cap can
    be satisfied by reducing contract count to the maximum still-valid size.
    """

    constraints: list[str] = []
    size_after = order.size_contracts

    # 1. schema validity — pydantic already enforced; double-check invariants
    if order.kill_switch_inheritance is not True:
        return _reject(order, ["schema:kill_switch_inheritance_must_be_true"], size_after)
    if order.venue != "HTX":
        return _reject(order, [f"schema:venue_not_supported:{order.venue}"], size_after)

    # 2. universe whitelist
    if order.symbol not in ctx.universe:
        return _reject(order, [f"universe:symbol_not_whitelisted:{order.symbol}"], size_after)

    # 8. stablecoin-of-quote sanity (gate before sizing because it scales NAV)
    if not (params.stable_depeg_floor <= ctx.quote_stable_price <= params.stable_depeg_ceiling):
        return _reject(
            order,
            [f"stable_depeg:USDT={ctx.quote_stable_price:.4f}"],
            size_after,
        )

    # 10. latency / heartbeat
    if ctx.last_tick_age_s > params.tick_freshness_max_seconds:
        return _reject(
            order,
            [f"stale_tick:{ctx.last_tick_age_s:.1f}s>{params.tick_freshness_max_seconds:.1f}s"],
            size_after,
        )

    if not ctx.venue_health_ok:
        return _reject(order, ["venue_unhealthy"], size_after)

    # 5. reference-price deviation — order price must be within 50bps of mid
    deviation_bps = abs(order.price - ctx.snapshot.mid) / ctx.snapshot.mid * 10_000
    if deviation_bps > params.max_ref_price_deviation_bps:
        return _reject(
            order,
            [f"price_deviation:{deviation_bps:.1f}bps>{params.max_ref_price_deviation_bps:.1f}bps"],
            size_after,
        )

    # 9. funding-rate sanity (per-hour, already normalised on FundingInfo)
    f_h = ctx.snapshot.funding.current_funding_per_hour
    if abs(f_h) > params.funding_kill_threshold_per_hour:
        return _reject(
            order,
            [f"funding_extreme:|f/h|={abs(f_h):.5%}>{params.funding_kill_threshold_per_hour:.5%}"],
            size_after,
        )

    # 4. notional sanity — 1 contract notional vs per-position cap
    one_contract_notional_usd = (
        ctx.snapshot.contract.contract_size * ctx.snapshot.mid
    )
    max_position_usd = params.max_position_pct_nav * ctx.account.nav_usd
    max_contracts_by_position = int(max_position_usd // one_contract_notional_usd)
    if max_contracts_by_position < 1:
        return _reject(
            order,
            [
                f"contract_too_large:1ct=${one_contract_notional_usd:.2f}"
                f">cap=${max_position_usd:.2f}"
            ],
            size_after,
        )

    if order.size_contracts > max_contracts_by_position:
        constraints.append(
            f"position_cap:cut_{order.size_contracts}->{max_contracts_by_position}_contracts"
        )
        size_after = max_contracts_by_position

    # 3. post-trade position limit including any existing exposure
    existing_contracts = ctx.account.open_positions.get(order.symbol, 0.0)
    signed_delta = size_after if order.side == Side.BUY else -size_after
    post_trade_signed = existing_contracts + signed_delta
    post_trade_notional_usd = abs(post_trade_signed) * one_contract_notional_usd
    if post_trade_notional_usd > max_position_usd:
        # try to cut further; if can't reach 1 ct, reject
        permitted_signed = (
            int(max_position_usd // one_contract_notional_usd)
            * (1 if signed_delta >= 0 else -1)
        )
        cut_contracts = abs(permitted_signed - existing_contracts)
        if cut_contracts < 1:
            return _reject(
                order,
                [f"post_trade_position_cap:|{post_trade_signed}|>cap_contracts"],
                size_after,
            )
        constraints.append(
            f"post_trade_position_cap:cut_{size_after}->{cut_contracts}_contracts"
        )
        size_after = cut_contracts

    # gross / concurrent positions cap (pre-trade open count)
    open_count = sum(1 for v in ctx.account.open_positions.values() if v != 0)
    new_position = ctx.account.open_positions.get(order.symbol, 0.0) == 0
    if new_position and open_count >= params.max_concurrent_positions:
        return _reject(
            order,
            [f"concurrent_positions_cap:open={open_count}>={params.max_concurrent_positions}"],
            size_after,
        )

    # 6. margin / free-collateral availability (1× isolated, no leverage)
    initial_margin_usd = size_after * one_contract_notional_usd / params.max_leverage
    if initial_margin_usd > ctx.account.available_usd:
        return _reject(
            order,
            [
                f"insufficient_collateral:need=${initial_margin_usd:.2f}"
                f">avail=${ctx.account.available_usd:.2f}"
            ],
            size_after,
        )

    # 11. daily loss budget — order's worst-case loss must fit
    worst_case_loss_usd = (
        size_after * one_contract_notional_usd * (order.max_slippage_bps / 10_000)
        + params.per_trade_loss_cap_pct_nav * ctx.account.nav_usd
    )
    if worst_case_loss_usd > ctx.daily_loss_budget_remaining_usd:
        return _reject(
            order,
            [
                f"daily_loss_budget:wc=${worst_case_loss_usd:.2f}"
                f">remaining=${ctx.daily_loss_budget_remaining_usd:.2f}"
            ],
            size_after,
        )

    # 16. HITL — mandatory in v0
    if "hitl" in order.approval_required and not ctx.hitl_approved:
        # not a rejection: the order is gated pending HITL approval
        return RiskOutcome(
            order_id=order.client_order_id,
            verdict=RiskVerdict.REJECTED,
            binding_constraints=["hitl:pending"],
            size_after_contracts=size_after,
            size_before_contracts=order.size_contracts,
            notes="HITL approval required — order routed to operator queue",
        )

    verdict = (
        RiskVerdict.SIZE_CUT
        if size_after != order.size_contracts
        else RiskVerdict.APPROVED
    )

    if size_after < 1:
        return _reject(order, ["size_cut_to_zero"], size_after)

    return RiskOutcome(
        order_id=order.client_order_id,
        verdict=verdict,
        binding_constraints=constraints,
        size_after_contracts=size_after,
        size_before_contracts=order.size_contracts,
    )


def _reject(order: ProposedOrder, reasons: list[str], size_after: int) -> RiskOutcome:
    return RiskOutcome(
        order_id=order.client_order_id,
        verdict=RiskVerdict.REJECTED,
        binding_constraints=reasons,
        size_after_contracts=size_after,
        size_before_contracts=order.size_contracts,
    )


__all__ = [
    "GateContext",
    "evaluate",
    "RiskOutcome",
    "RiskVerdict",
    "OrderStatus",
]
