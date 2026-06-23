"""Append-only SQLite state.

The journal is the source of truth for "what did the system see, decide,
and execute" — required for any post-mortem after a kill-switch trip.

Writes are append-only by convention. Schema migrations are forward-only;
deprecated fields are retained.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    agent TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS proposed_orders (
    client_order_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    decision_hash TEXT NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    size_usd REAL NOT NULL,
    size_contracts INTEGER NOT NULL,
    price REAL NOT NULL,
    status TEXT NOT NULL,
    risk_verdict TEXT,
    risk_constraints_json TEXT,
    hitl_status TEXT,
    submitted_order_id TEXT,
    fill_avg_price REAL,
    fill_qty INTEGER,
    fee_usd REAL,
    funding_paid_usd REAL,
    realised_pnl_usd REAL,
    created_at TEXT NOT NULL,
    last_updated_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL,
    venue_order_id TEXT,
    ts TEXT NOT NULL,
    px REAL NOT NULL,
    qty INTEGER NOT NULL,
    fee_usd REAL NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    nav_usd REAL NOT NULL,
    available_usd REAL NOT NULL,
    open_positions_json TEXT NOT NULL,
    daily_realised_pnl_usd REAL NOT NULL,
    intraday_hwm REAL NOT NULL,
    rolling_7d_hwm REAL NOT NULL,
    all_time_hwm REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS kills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT,
    resolved_at TEXT,
    resolved_by TEXT
);

CREATE TABLE IF NOT EXISTS injection_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    sample TEXT NOT NULL,
    classifier_score REAL,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON proposed_orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created ON proposed_orders(created_at);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS idx_kills_ts ON kills(ts);
"""


class Journal:
    """Thin SQLite wrapper. Single-process; serialises via connection lock."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    def append_decision(
        self,
        agent: str,
        kind: str,
        payload: dict[str, Any],
        evidence_ids: list[str] | None = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO decisions(ts,agent,kind,payload_json,evidence_ids_json) "
                "VALUES (?,?,?,?,?)",
                (
                    _now_iso(),
                    agent,
                    kind,
                    json.dumps(payload, default=_json_default),
                    json.dumps(evidence_ids or []),
                ),
            )

    def insert_proposed_order(
        self, client_order_id: str, fields: dict[str, Any], raw: dict[str, Any]
    ) -> None:
        cols = list(fields.keys())
        with self._cursor() as cur:
            cur.execute(
                f"INSERT INTO proposed_orders(client_order_id,{','.join(cols)},raw_json,created_at,last_updated_at) "
                f"VALUES (?,{','.join('?' for _ in cols)},?,?,?)",
                (
                    client_order_id,
                    *(fields[c] for c in cols),
                    json.dumps(raw, default=_json_default),
                    _now_iso(),
                    _now_iso(),
                ),
            )

    def update_order_status(
        self, client_order_id: str, status: str, **fields: Any
    ) -> None:
        sets = ["status=?", "last_updated_at=?"]
        vals: list[Any] = [status, _now_iso()]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(v)
        vals.append(client_order_id)
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE proposed_orders SET {','.join(sets)} WHERE client_order_id=?", vals
            )

    def append_fill(
        self,
        client_order_id: str,
        venue_order_id: str | None,
        px: float,
        qty: int,
        fee_usd: float,
        raw: dict[str, Any],
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO fills(client_order_id,venue_order_id,ts,px,qty,fee_usd,raw_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    client_order_id,
                    venue_order_id,
                    _now_iso(),
                    px,
                    qty,
                    fee_usd,
                    json.dumps(raw, default=_json_default),
                ),
            )

    def snapshot_account(
        self,
        nav: float,
        available: float,
        positions: dict[str, float],
        daily_pnl: float,
        intraday_hwm: float,
        rolling_7d_hwm: float,
        all_time_hwm: float,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO account_snapshots(ts,nav_usd,available_usd,open_positions_json,"
                "daily_realised_pnl_usd,intraday_hwm,rolling_7d_hwm,all_time_hwm) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    _now_iso(),
                    nav,
                    available,
                    json.dumps(positions),
                    daily_pnl,
                    intraday_hwm,
                    rolling_7d_hwm,
                    all_time_hwm,
                ),
            )

    def record_kill(self, reason: str, payload: dict[str, Any] | None = None) -> int:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO kills(ts,reason,payload_json) VALUES (?,?,?)",
                (_now_iso(), reason, json.dumps(payload or {}, default=_json_default)),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def resolve_kill(self, kill_id: int, by: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE kills SET resolved_at=?, resolved_by=? WHERE id=?",
                (_now_iso(), by, kill_id),
            )

    def record_injection_attempt(
        self, source: str, sample: str, score: float | None = None, notes: str = ""
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO injection_attempts(ts,source,sample,classifier_score,notes) "
                "VALUES (?,?,?,?,?)",
                (_now_iso(), source, sample[:2000], score, notes),
            )

    def fetch_pending_hitl(self) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT client_order_id,strategy_id,symbol,side,size_contracts,price,"
                "risk_constraints_json,created_at "
                "FROM proposed_orders WHERE status=?",
                (("hitl_pending",)),
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    return str(o)
