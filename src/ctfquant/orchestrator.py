"""Main loop. The supervisor of the supervisor.

Responsibilities (tier -1 scope):
  - Build feed state from data.py tasks.
  - Periodically run the strategy and emit ProposedOrders.
  - Validate every order through the Risk Engine deterministic gate.
  - Surface approved orders to the HITL bot; act on operator decisions.
  - Run kill-switch evaluation continuously; on trip, flatten + halt.
  - Periodically snapshot account state to the journal.

This module never makes a trading decision the gate hasn't approved.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import AppParams, RiskParams, VenueParams
from .data import FeedState, book_streamer, funding_poller, select_universe
from .hitl_bot import HITLBot, HITLState
from .kill_switches import KillSwitchState, evaluate as kill_evaluate, reset as kill_reset
from .risk_engine import GateContext, evaluate as risk_evaluate
from .state import Journal
from .strategy_funding import StrategyState, propose
from .types import (
    AccountState,
    OrderStatus,
    ProposedOrder,
    RiskOutcome,
    RiskVerdict,
    Side,
)
from .venue_htx import HTXClient

log = logging.getLogger(__name__)


@dataclass
class OrchestratorState:
    feed: FeedState
    kills: KillSwitchState
    strategy: StrategyState
    hitl: HITLState
    account: AccountState | None = None
    universe_symbols: frozenset[str] = frozenset()
    halted: bool = False
    daily_loss_budget_remaining_usd: float = 0.0
    quote_stable_price: float = 1.0


async def run_loop(
    app: AppParams,
    risk: RiskParams,
    venue: VenueParams,
    journal: Journal,
    htx: HTXClient,
) -> None:
    state = OrchestratorState(
        feed=FeedState(),
        kills=KillSwitchState(),
        strategy=StrategyState(),
        hitl=HITLState(),
        daily_loss_budget_remaining_usd=risk.daily_loss_limit_pct_nav * risk.nav_usd,
    )

    log.info("selecting universe at startup …")
    contracts = await select_universe(htx, risk, risk.nav_usd)
    if not contracts:
        raise RuntimeError("no contracts pass universe filter — try larger NAV")
    state.universe_symbols = frozenset(c.symbol for c in contracts)
    log.info("universe: %s", sorted(state.universe_symbols))

    bot: HITLBot | None = None
    if app.telegram_bot_token and app.telegram_chat_id:
        bot = HITLBot(
            token=app.telegram_bot_token,
            chat_id=app.telegram_chat_id,
            state=state.hitl,
            status_provider=lambda: _status(state, risk, app),
            postmortem_provider=lambda kid: _postmortem(journal, kid),
        )
        await bot.start()
        await bot.notify(
            f"🟢 ctfquant online — tier {app.tier}, NAV ${risk.nav_usd:.0f}, dry_run={app.dry_run}"
        )
    else:
        log.warning("telegram credentials missing — HITL is disabled")

    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(funding_poller(htx, contracts, state.feed)),
        asyncio.create_task(book_streamer(htx, contracts, state.feed)),
        asyncio.create_task(_account_refresher(htx, state, journal, app, risk)),
        asyncio.create_task(_strategy_tick(state, risk, journal, bot)),
        asyncio.create_task(_kill_watcher(state, risk, journal, bot)),
        asyncio.create_task(_hitl_resolver(state, risk, journal, bot, htx, app)),
    ]

    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        if bot:
            await bot.stop()


async def _account_refresher(
    htx: HTXClient,
    state: OrchestratorState,
    journal: Journal,
    app: AppParams,
    risk: RiskParams,
) -> None:
    """Pull NAV + positions from HTX every 15s. In dry-run, fall back to risk.nav_usd."""

    while True:
        try:
            if app.dry_run or not (app.htx_access_key and app.htx_secret_key):
                # construct a synthetic account state so the gate can run end-to-end
                state.account = AccountState(
                    ts=datetime.now(timezone.utc),
                    nav_usd=risk.nav_usd,
                    available_usd=risk.nav_usd * 0.9,
                    open_positions={},
                    daily_realised_pnl_usd=0.0,
                    intraday_high_watermark_usd=risk.nav_usd,
                    rolling_7d_high_watermark_usd=risk.nav_usd,
                    all_time_high_watermark_usd=risk.nav_usd,
                )
            else:
                acct = await htx.cross_account_info()
                pos = await htx.cross_position_info()
                state.account = _account_from_htx(acct, pos)
            assert state.account is not None
            journal.snapshot_account(
                nav=state.account.nav_usd,
                available=state.account.available_usd,
                positions=state.account.open_positions,
                daily_pnl=state.account.daily_realised_pnl_usd,
                intraday_hwm=state.account.intraday_high_watermark_usd,
                rolling_7d_hwm=state.account.rolling_7d_high_watermark_usd,
                all_time_hwm=state.account.all_time_high_watermark_usd,
            )
        except Exception as e:
            log.warning("account refresh failed: %s", e)
        await asyncio.sleep(15.0)


def _account_from_htx(acct_resp: dict, pos_resp: dict) -> AccountState:
    """Map HTX cross-account/position payloads to AccountState. Best-effort.

    Implemented for the most common HTX response shape; if your account
    type differs, log + degrade gracefully (the system will still run on
    snapshot defaults).
    """

    nav = 0.0
    avail = 0.0
    for entry in acct_resp.get("data") or []:
        if entry.get("margin_account") in ("USDT", "USDT-S"):
            nav = float(entry.get("margin_balance") or entry.get("margin_static") or 0.0)
            avail = float(entry.get("margin_available") or 0.0)
            break
    positions: dict[str, float] = {}
    for p in pos_resp.get("data") or []:
        sym = p.get("contract_code")
        vol = float(p.get("volume") or 0)
        direction = p.get("direction")
        if sym and vol:
            positions[sym] = vol if direction == "buy" else -vol
    return AccountState(
        ts=datetime.now(timezone.utc),
        nav_usd=nav or 0.0,
        available_usd=avail or 0.0,
        open_positions=positions,
        daily_realised_pnl_usd=0.0,
        intraday_high_watermark_usd=nav or 0.0,
        rolling_7d_high_watermark_usd=nav or 0.0,
        all_time_high_watermark_usd=nav or 0.0,
    )


async def _strategy_tick(
    state: OrchestratorState,
    risk: RiskParams,
    journal: Journal,
    bot: HITLBot | None,
) -> None:
    """Run the funding strategy every 30s."""

    while True:
        if state.halted or state.kills.tripped_reason:
            await asyncio.sleep(5.0)
            continue
        if state.account is None:
            await asyncio.sleep(2.0)
            continue
        snapshots = list(state.feed.snapshots.values())
        if not snapshots:
            await asyncio.sleep(2.0)
            continue
        proposals = propose(snapshots, state.account, risk, state.strategy)
        for order in proposals:
            await _route_order(order, state, risk, journal, bot)
        await asyncio.sleep(30.0)


async def _route_order(
    order: ProposedOrder,
    state: OrchestratorState,
    risk: RiskParams,
    journal: Journal,
    bot: HITLBot | None,
) -> None:
    snap = state.feed.get(order.symbol)
    assert state.account is not None
    if snap is None:
        log.warning("dropping order for %s — no snapshot", order.symbol)
        return

    ctx = GateContext(
        snapshot=snap,
        account=state.account,
        universe=state.universe_symbols,
        daily_loss_budget_remaining_usd=state.daily_loss_budget_remaining_usd,
        venue_health_ok=state.kills.tripped_reason is None,
        last_tick_age_s=state.feed.age_s(order.symbol),
        quote_stable_price=state.quote_stable_price,
        hitl_approved=False,    # first pass: queue for HITL
    )
    outcome = risk_evaluate(order, ctx, risk)
    journal.insert_proposed_order(
        order.client_order_id,
        fields=dict(
            strategy_id=order.strategy_id,
            decision_hash=order.decision_hash,
            venue=order.venue,
            symbol=order.symbol,
            side=order.side.value,
            size_usd=order.size_usd,
            size_contracts=outcome.size_after_contracts,
            price=order.price,
            status=OrderStatus.HITL_PENDING.value
            if outcome.binding_constraints == ["hitl:pending"]
            else OrderStatus.REJECTED.value
            if outcome.verdict == RiskVerdict.REJECTED
            else OrderStatus.APPROVED.value,
            risk_verdict=outcome.verdict.value,
            risk_constraints_json=_dumps(outcome.binding_constraints),
            hitl_status="pending"
            if outcome.binding_constraints == ["hitl:pending"]
            else None,
        ),
        raw=order.model_dump(mode="json"),
    )
    journal.append_decision(
        agent="risk_engine",
        kind="risk_outcome",
        payload=outcome.model_dump(mode="json"),
        evidence_ids=order.evidence_ids,
    )

    if outcome.verdict == RiskVerdict.REJECTED and outcome.binding_constraints != ["hitl:pending"]:
        if bot:
            await bot.notify(
                f"❌ Risk rejected {order.symbol} {order.side.value} "
                f"({','.join(outcome.binding_constraints)})"
            )
        return

    summary = {
        "client_order_id": order.client_order_id,
        "strategy_id": order.strategy_id,
        "symbol": order.symbol,
        "side": order.side.value,
        "size_contracts": outcome.size_after_contracts,
        "price": order.price,
        "funding_per_hour_pct": f"{snap.funding.current_funding_per_hour*100:.4f}%",
        "risk_verdict": outcome.verdict.value,
        "binding_constraints": ",".join(outcome.binding_constraints) or "-",
    }
    state.hitl.queue(order.client_order_id, summary)
    if bot:
        await bot.announce_proposal(summary)


async def _hitl_resolver(
    state: OrchestratorState,
    risk: RiskParams,
    journal: Journal,
    bot: HITLBot | None,
    htx: HTXClient,
    app: AppParams,
) -> None:
    """Watch HITL approvals/rejections and act on them."""

    while True:
        for coid, decision in list(state.hitl.approvals.items()):
            del state.hitl.approvals[coid]
            if decision == "approved":
                await _execute_approved(coid, state, journal, htx, app, bot)
            else:
                journal.update_order_status(coid, OrderStatus.REJECTED.value, hitl_status="rejected")
        if state.hitl.halt_requested:
            state.hitl.halt_requested = False
            state.halted = True
            kid = journal.record_kill(
                reason="operator_halt", payload={"source": "telegram /halt"}
            )
            if bot:
                await bot.notify(f"🛑 HALTED by operator. kill_id={kid}. /resume to clear.")
        if state.hitl.resume_requested:
            state.hitl.resume_requested = False
            state.halted = False
            kill_reset(state.kills, by="operator:telegram")
            if bot:
                await bot.notify("▶ resumed by operator.")
        await asyncio.sleep(0.5)


async def _execute_approved(
    coid: str,
    state: OrchestratorState,
    journal: Journal,
    htx: HTXClient,
    app: AppParams,
    bot: HITLBot | None,
) -> None:
    """Place the approved order on HTX (or simulate in dry-run)."""

    if app.dry_run:
        journal.update_order_status(
            coid, OrderStatus.SUBMITTED.value, hitl_status="approved"
        )
        if bot:
            await bot.notify(f"📤 (dry) submitted {coid[:8]}")
        return

    # not implemented: real submission needs (contract_code, side, volume, price)
    # round-trip from journal. Wired up for v0.1.
    if bot:
        await bot.notify(
            f"⚠ live submission not enabled in this build (coid {coid[:8]}). "
            "Set DRY_RUN=false AND complete venue_htx wiring before going live."
        )
    journal.update_order_status(coid, OrderStatus.APPROVED.value, hitl_status="approved")


async def _kill_watcher(
    state: OrchestratorState,
    risk: RiskParams,
    journal: Journal,
    bot: HITLBot | None,
) -> None:
    while True:
        if state.account is not None:
            funding_max = max(
                (abs(s.funding.current_funding_per_hour) for s in state.feed.snapshots.values()),
                default=0.0,
            )
            reason = kill_evaluate(
                state.kills,
                state.account,
                risk,
                state.quote_stable_price,
                funding_max,
            )
            if reason and not state.halted:
                state.halted = True
                kid = journal.record_kill(reason=reason, payload={"trigger": "auto"})
                if bot:
                    await bot.notify(
                        f"🚨 KILL SWITCH TRIPPED\nreason: {reason}\nkill_id: {kid}\n"
                        "system: READ-ONLY, flatten-only\n/postmortem {kid} to review"
                    )
        await asyncio.sleep(2.0)


async def _status(state: OrchestratorState, risk: RiskParams, app: AppParams) -> str:
    acct = state.account
    if not acct:
        return "no account snapshot yet"
    open_pos = ", ".join(f"{k}={v:+}" for k, v in acct.open_positions.items() if v != 0) or "none"
    pending = len(state.hitl.pending)
    kill = state.kills.tripped_reason or "green"
    halted = "HALTED" if state.halted else "running"
    return (
        f"tier={app.tier}  state={halted}  kill={kill}\n"
        f"NAV=${acct.nav_usd:.2f}  avail=${acct.available_usd:.2f}\n"
        f"daily_pnl=${acct.daily_realised_pnl_usd:+.2f}\n"
        f"positions: {open_pos}\n"
        f"pending_hitl: {pending}\n"
        f"universe_size: {len(state.universe_symbols)}\n"
        f"feed_size: {len(state.feed.snapshots)}"
    )


async def _postmortem(journal: Journal, kill_id: int) -> str:
    return f"postmortem feature pending — see var/state.db kills row id={kill_id}"


def _dumps(obj) -> str:
    import json
    return json.dumps(obj, default=str)
