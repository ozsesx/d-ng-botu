from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable

import requests

try:
    from .config import fallback_universe
except ImportError:
    from config import fallback_universe

log = logging.getLogger(__name__)

FAPI_BASE = "https://fapi.binance.com"

_HEADERS = {
    "User-Agent": "d-ng-botu/1.0 (Streamlit; +https://github.com/ozsesx/d-ng-botu)",
    "Accept": "application/json",
}


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str  # lowercase like "solusdt"


def _get_json(path: str, params: dict[str, Any] | None = None, timeout_s: float = 10.0) -> Any:
    r = requests.get(
        f"{FAPI_BASE}{path}",
        params=params,
        headers=_HEADERS,
        timeout=timeout_s,
    )
    r.raise_for_status()
    return r.json()


def list_usdt_perp_symbols() -> list[str]:
    """
    Returns lowercase USDT perpetual symbols (e.g. "solusdt").
    """
    try:
        data = _get_json("/fapi/v1/exchangeInfo")
    except requests.RequestException as exc:
        log.warning("exchangeInfo failed (%s), using fallback universe", exc)
        return fallback_universe(80)

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
    return out or fallback_universe(80)


def top_usdt_perp_by_quote_volume(limit: int = 80) -> tuple[list[str], str]:
    """
    Uses 24h ticker quoteVolume to pick a liquid universe quickly.
    Returns (symbols, source) where source is 'binance' or 'fallback'.
    """
    try:
        data = _get_json("/fapi/v1/ticker/24hr", timeout_s=15.0)
    except requests.RequestException as exc:
        log.warning("24hr ticker failed (%s), using fallback universe", exc)
        return fallback_universe(limit), "fallback"

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
    if not out:
        return fallback_universe(limit), "fallback"
    return out, "binance"


def fetch_1d_klines(symbol: str, start_ms: int, end_ms: int | None = None, limit: int = 1000) -> list[list[Any]]:
    params: dict[str, Any] = {"symbol": symbol.upper(), "interval": "1d", "startTime": start_ms, "limit": limit}
    if end_ms is not None:
        params["endTime"] = end_ms
    try:
        return _get_json("/fapi/v1/klines", params=params, timeout_s=20.0)
    except requests.RequestException:
        return []


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
