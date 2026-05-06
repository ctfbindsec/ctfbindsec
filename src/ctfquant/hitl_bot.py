"""Telegram HITL gate.

The PR Agent and Compliance Agent are stubs in v0; the HITL bot is the
ONLY interactive surface. Operator commands map to deterministic actions
on the orchestrator. The bot itself never makes trading decisions and
never has venue credentials — it sets booleans on a shared state object
that the orchestrator polls.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

log = logging.getLogger(__name__)


@dataclass
class HITLState:
    """Shared mutable state read by the orchestrator and written by the bot."""

    pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    approvals: dict[str, str] = field(default_factory=dict)   # client_order_id -> "approved"|"rejected"
    halt_requested: bool = False
    resume_requested: bool = False
    last_status_request_at: datetime | None = None

    def queue(self, client_order_id: str, summary: dict[str, Any]) -> None:
        self.pending[client_order_id] = summary

    def resolve(self, client_order_id: str, decision: str) -> bool:
        if client_order_id not in self.pending:
            return False
        self.pending.pop(client_order_id)
        self.approvals[client_order_id] = decision
        return True


def _short(coid: str) -> str:
    return coid[:8]


def _resolve_short_id(state: HITLState, short: str) -> str | None:
    matches = [k for k in state.pending if k.startswith(short)]
    if len(matches) == 1:
        return matches[0]
    return None


class HITLBot:
    """Wraps a python-telegram-bot Application around HITLState."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        state: HITLState,
        status_provider: Callable[[], Awaitable[str]],
        postmortem_provider: Callable[[int], Awaitable[str]],
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._state = state
        self._status_provider = status_provider
        self._postmortem_provider = postmortem_provider
        self._app: Application | None = None

    async def start(self) -> None:
        self._app = Application.builder().token(self._token).build()
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("positions", self._cmd_positions))
        self._app.add_handler(CommandHandler("approve", self._cmd_approve))
        self._app.add_handler(CommandHandler("reject", self._cmd_reject))
        self._app.add_handler(CommandHandler("halt", self._cmd_halt))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))
        self._app.add_handler(CommandHandler("postmortem", self._cmd_postmortem))
        self._app.add_handler(CommandHandler("queue", self._cmd_queue))
        await self._app.initialize()
        await self._app.start()
        assert self._app.updater is not None
        await self._app.updater.start_polling(drop_pending_updates=True)
        log.info("HITL bot ready, chat_id=%s", self._chat_id)

    async def stop(self) -> None:
        if self._app:
            assert self._app.updater is not None
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    async def notify(self, text: str) -> None:
        if not self._app:
            log.warning("notify called before start: %s", text)
            return
        await self._app.bot.send_message(chat_id=self._chat_id, text=text, parse_mode=None)

    async def announce_proposal(self, summary: dict[str, Any]) -> None:
        coid = summary["client_order_id"]
        text = (
            f"📥 Proposed order  {_short(coid)}\n"
            f"strategy: {summary.get('strategy_id','?')}\n"
            f"{summary.get('symbol','?')}  {summary.get('side','?')}  "
            f"{summary.get('size_contracts','?')} ct  @ {summary.get('price','?')}\n"
            f"funding/h: {summary.get('funding_per_hour_pct','?')}\n"
            f"risk: {summary.get('risk_verdict','?')}  cuts: {summary.get('binding_constraints','-')}\n"
            f"\n/approve {_short(coid)}   /reject {_short(coid)}"
        )
        await self.notify(text)

    # ---------- handlers ----------

    def _allowed(self, update: Update) -> bool:
        return update.effective_chat is not None and str(update.effective_chat.id) == self._chat_id

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        self._state.last_status_request_at = datetime.now(timezone.utc)
        text = await self._status_provider()
        await update.effective_message.reply_text(text)             # type: ignore[union-attr]

    async def _cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        text = await self._status_provider()
        await update.effective_message.reply_text(text)             # type: ignore[union-attr]

    async def _cmd_queue(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        if not self._state.pending:
            await update.effective_message.reply_text("queue empty")  # type: ignore[union-attr]
            return
        lines = []
        for coid, s in self._state.pending.items():
            lines.append(
                f"{_short(coid)} {s.get('symbol')} {s.get('side')} {s.get('size_contracts')}ct"
            )
        await update.effective_message.reply_text("\n".join(lines))    # type: ignore[union-attr]

    async def _cmd_approve(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        if not ctx.args:
            await update.effective_message.reply_text("usage: /approve <order_id_short>")  # type: ignore[union-attr]
            return
        full = _resolve_short_id(self._state, ctx.args[0])
        if not full:
            await update.effective_message.reply_text(f"no pending order matching {ctx.args[0]!r}")  # type: ignore[union-attr]
            return
        self._state.resolve(full, "approved")
        await update.effective_message.reply_text(f"✅ approved {_short(full)}")  # type: ignore[union-attr]

    async def _cmd_reject(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        if not ctx.args:
            await update.effective_message.reply_text("usage: /reject <order_id_short>")  # type: ignore[union-attr]
            return
        full = _resolve_short_id(self._state, ctx.args[0])
        if not full:
            await update.effective_message.reply_text(f"no pending order matching {ctx.args[0]!r}")  # type: ignore[union-attr]
            return
        self._state.resolve(full, "rejected")
        await update.effective_message.reply_text(f"✋ rejected {_short(full)}")  # type: ignore[union-attr]

    async def _cmd_halt(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        self._state.halt_requested = True
        await update.effective_message.reply_text("🛑 HALT requested. System will flatten and stop.")  # type: ignore[union-attr]

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        self._state.resume_requested = True
        await update.effective_message.reply_text("▶ resume requested.")  # type: ignore[union-attr]

    async def _cmd_postmortem(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        if not ctx.args:
            await update.effective_message.reply_text("usage: /postmortem <kill_id>")  # type: ignore[union-attr]
            return
        try:
            kill_id = int(ctx.args[0])
        except ValueError:
            await update.effective_message.reply_text("kill_id must be an integer")  # type: ignore[union-attr]
            return
        text = await self._postmortem_provider(kill_id)
        await update.effective_message.reply_text(text)  # type: ignore[union-attr]
