from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

import requests


FAPI_BASE = "https://fapi.binance.com"


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str  # lowercase like "solusdt"


def _get_json(path: str, params: dict[str, Any] | None = None, timeout_s: float = 10.0) -> Any:
    r = requests.get(f"{FAPI_BASE}{path}", params=params, timeout=timeout_s)
    r.raise_for_status()
    return r.json()


def list_usdt_perp_symbols() -> list[str]:
    """
    Returns lowercase USDT perpetual symbols (e.g. "solusdt").
    """
    data = _get_json("/fapi/v1/exchangeInfo")
    out: list[str] = []
    for s in data.get("symbols", []):
        if s.get("contractType") != "PERPETUAL":
            continue
        if s.get("quoteAsset") != "USDT":
            continue
        if s.get("status") != "TRADING":
            continue
        sym = str(s.get("symbol", "")).lower()
        if sym:
            out.append(sym)
    out.sort()
    return out


def top_usdt_perp_by_quote_volume(limit: int = 80) -> list[str]:
    """
    Uses 24h ticker quoteVolume to pick a liquid universe quickly.
    """
    data = _get_json("/fapi/v1/ticker/24hr")
    rows = []
    for r in data:
        sym = str(r.get("symbol", "")).lower()
        if not sym.endswith("usdt"):
            continue
        try:
            qv = float(r.get("quoteVolume", 0.0))
        except Exception:
            continue
        rows.append((qv, sym))
    rows.sort(reverse=True)
    out = []
    seen = set()
    for _, sym in rows:
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= limit:
            break
    return out


def fetch_1d_klines(symbol: str, start_ms: int, end_ms: int | None = None, limit: int = 1000) -> list[list[Any]]:
    params: dict[str, Any] = {"symbol": symbol.upper(), "interval": "1d", "startTime": start_ms, "limit": limit}
    if end_ms is not None:
        params["endTime"] = end_ms
    return _get_json("/fapi/v1/klines", params=params, timeout_s=20.0)


def iter_1d_klines(symbol: str, start_ms: int, end_ms: int | None = None) -> Iterable[list[Any]]:
    """
    Generator over daily klines, handling pagination.
    """
    cur = start_ms
    while True:
        batch = fetch_1d_klines(symbol, start_ms=cur, end_ms=end_ms, limit=1000)
        if not batch:
            return
        for row in batch:
            yield row
        last_open = int(batch[-1][0])
        next_cur = last_open + 24 * 60 * 60 * 1000
        if next_cur <= cur:
            return
        cur = next_cur
        if end_ms is not None and cur > end_ms:
            return
        time.sleep(0.05)

