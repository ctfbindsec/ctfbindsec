"""HTX USDT-M perpetuals adapter.

Public market data is available without authentication. Trading needs
HMAC-SHA256 v2-signed requests, IP-whitelisted API keys with withdraw
permission OFF.

Auth scheme reference (HTX HMAC-SHA256 v2):
  payload = METHOD + "\n" + host + "\n" + path + "\n" + sorted_params
  signature = base64(hmac_sha256(secret, payload))

Public endpoints we use:
  GET  /linear-swap-api/v1/swap_contract_info       contract specs
  GET  /linear-swap-api/v1/swap_index               index price
  GET  /linear-swap-api/v1/swap_funding_rate        current funding
  GET  /linear-swap-api/v1/swap_historical_funding_rate
  WS   wss://api.hbdm.com/linear-swap-ws            mark, depth, trades

Private endpoints we use:
  POST /linear-swap-api/v1/swap_cross_account_info  account NAV / margin
  POST /linear-swap-api/v1/swap_cross_position_info open positions
  POST /linear-swap-api/v1/swap_cross_order         place order
  POST /linear-swap-api/v1/swap_cross_cancel        cancel order

This module returns typed objects (or raw dicts where shape is large
and pre-typing adds little value). It does NOT make trading decisions
— the orchestrator drives.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from urllib.parse import quote, urlencode

import httpx
import websockets

from .config import VenueParams
from .types import ContractSpec, FundingInfo, MarketSnapshot

log = logging.getLogger(__name__)


class HTXError(Exception):
    pass


def _utc_iso_minutes() -> str:
    """HTX expects Timestamp formatted as YYYY-MM-DDTHH:MM:SS (no ms, no tz)."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _sign(method: str, host: str, path: str, params: dict[str, Any], secret: str) -> str:
    sorted_items = sorted(params.items(), key=lambda kv: kv[0])
    encoded = "&".join(
        f"{quote(str(k), safe='')}={quote(str(v), safe='')}" for k, v in sorted_items
    )
    payload = f"{method.upper()}\n{host}\n{path}\n{encoded}"
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class HTXClient:
    """Async HTX USDT-M client. Reuses one httpx.AsyncClient for connection pooling."""

    def __init__(self, params: VenueParams, access_key: str = "", secret_key: str = "") -> None:
        self._params = params
        self._access_key = access_key
        self._secret_key = secret_key
        self._http = httpx.AsyncClient(
            base_url=f"https://{params.host_usdt_m}",
            timeout=httpx.Timeout(
                connect=params.rest_connect_timeout_s,
                read=params.rest_read_timeout_s,
                write=params.rest_read_timeout_s,
                pool=params.rest_read_timeout_s,
            ),
        )
        self._private_call_times: list[float] = []

    async def aclose(self) -> None:
        await self._http.aclose()

    # ----------------- public REST -----------------

    async def get_contract_info(self) -> list[ContractSpec]:
        r = await self._http.get("/linear-swap-api/v1/swap_contract_info")
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "ok":
            raise HTXError(f"contract_info: {body}")
        out: list[ContractSpec] = []
        for row in body["data"]:
            try:
                out.append(
                    ContractSpec(
                        symbol=row["contract_code"],
                        contract_code=row["contract_code"],
                        contract_size=float(row["contract_size"]),
                        price_tick=float(row["price_tick"]),
                    )
                )
            except (KeyError, ValueError) as e:
                log.warning("skipping contract row %s: %s", row.get("contract_code"), e)
        return out

    async def get_funding(self, contract_code: str) -> dict[str, Any]:
        r = await self._http.get(
            "/linear-swap-api/v1/swap_funding_rate", params={"contract_code": contract_code}
        )
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "ok":
            raise HTXError(f"funding_rate: {body}")
        return body["data"]

    async def get_index(self, contract_code: str) -> float:
        r = await self._http.get(
            "/linear-swap-api/v1/swap_index", params={"contract_code": contract_code}
        )
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "ok":
            raise HTXError(f"index: {body}")
        for row in body["data"]:
            if row.get("contract_code") == contract_code:
                return float(row["index_price"])
        raise HTXError(f"index: {contract_code} not in response")

    # ----------------- private REST -----------------

    async def _private_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self._access_key or not self._secret_key:
            raise HTXError("private endpoints require HTX_ACCESS_KEY and HTX_SECRET_KEY")

        # naive rate limit: 144/3s for USDT-M private
        now = time.monotonic()
        self._private_call_times = [t for t in self._private_call_times if now - t < 3.0]
        if len(self._private_call_times) >= self._params.private_rate_limit_per_3s:
            sleep_for = 3.0 - (now - self._private_call_times[0]) + 0.05
            log.warning("rate limit guard sleep %.2fs", sleep_for)
            await asyncio.sleep(sleep_for)
        self._private_call_times.append(time.monotonic())

        params = {
            "AccessKeyId": self._access_key,
            "SignatureMethod": "HmacSHA256",
            "SignatureVersion": "2",
            "Timestamp": _utc_iso_minutes(),
        }
        signature = _sign("POST", self._params.host_usdt_m, path, params, self._secret_key)
        params["Signature"] = signature
        url = f"{path}?{urlencode(params, quote_via=quote)}"
        r = await self._http.post(
            url,
            json=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            raise HTXError(f"{path}: {data}")
        return data

    async def cross_account_info(self) -> dict[str, Any]:
        return await self._private_post(
            "/linear-swap-api/v1/swap_cross_account_info", {"margin_account": "USDT"}
        )

    async def cross_position_info(self) -> dict[str, Any]:
        return await self._private_post(
            "/linear-swap-api/v1/swap_cross_position_info", {}
        )

    async def place_cross_order(
        self,
        *,
        contract_code: str,
        client_order_id: str,
        direction: str,
        offset: str,
        volume: int,
        price: float,
        order_price_type: str = "post_only",
        lever_rate: int = 1,
        reduce_only: int = 0,
    ) -> dict[str, Any]:
        body = {
            "contract_code": contract_code,
            "client_order_id": _coerce_client_order_id(client_order_id),
            "direction": direction,
            "offset": offset,
            "volume": int(volume),
            "price": price,
            "order_price_type": order_price_type,
            "lever_rate": int(lever_rate),
            "reduce_only": int(reduce_only),
        }
        return await self._private_post("/linear-swap-api/v1/swap_cross_order", body)

    async def cancel_cross_order(self, *, contract_code: str, order_id: str) -> dict[str, Any]:
        return await self._private_post(
            "/linear-swap-api/v1/swap_cross_cancel",
            {"contract_code": contract_code, "order_id": order_id},
        )

    # ----------------- public WS -----------------

    async def stream_public(self, subs: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to a list of public-WS topics. HTX uses gzip; library handles it."""

        url = self._params.ws_public_url
        async with websockets.connect(url, max_queue=256) as ws:
            for sub in subs:
                await ws.send(json.dumps(sub))
            while True:
                raw = await ws.recv()
                if isinstance(raw, bytes):
                    import gzip
                    raw = gzip.decompress(raw).decode("utf-8")
                msg = json.loads(raw)
                if "ping" in msg:
                    await ws.send(json.dumps({"pong": msg["ping"]}))
                    continue
                yield msg


def build_snapshot(
    contract: ContractSpec,
    bid: float,
    ask: float,
    mark: float,
    last: float,
    funding_payload: dict[str, Any],
    last_tick_age_s: float,
) -> MarketSnapshot:
    """Compose a typed MarketSnapshot from heterogeneous HTX payloads."""

    interval_str = str(funding_payload.get("funding_interval", "8")).rstrip("h")
    try:
        interval_hours = int(interval_str)
    except ValueError:
        interval_hours = 8
        log.warning("unparseable funding_interval %r, defaulting to 8h", funding_payload.get("funding_interval"))

    next_funding_ms = funding_payload.get("next_funding_time")
    if next_funding_ms:
        next_funding = datetime.fromtimestamp(int(next_funding_ms) / 1000, tz=timezone.utc)
    else:
        next_funding = datetime.now(timezone.utc)

    funding = FundingInfo(
        symbol=contract.symbol,
        funding_interval_hours=interval_hours,
        next_funding_ts=next_funding,
        current_funding_per_period=float(funding_payload.get("funding_rate", 0.0)),
        estimated_funding_per_period=float(funding_payload.get("estimated_rate", funding_payload.get("funding_rate", 0.0))),
    )

    mid = (bid + ask) / 2.0 if bid and ask else last or mark
    return MarketSnapshot(
        symbol=contract.symbol,
        ts=datetime.now(timezone.utc),
        bid=bid,
        ask=ask,
        mid=mid,
        last=last,
        mark=mark,
        funding=funding,
        contract=contract,
        last_tick_age_s=last_tick_age_s,
    )


def _coerce_client_order_id(coid: str) -> int:
    """HTX's client_order_id is a uint64. Hash our hex sha256 down to fit."""

    return int(coid[:15], 16) if all(c in "0123456789abcdef" for c in coid[:15]) else int(time.time() * 1000)
