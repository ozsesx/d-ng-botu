from __future__ import annotations

import time
from dataclasses import dataclass

from .binance_api import iter_1d_klines
from .db import DailyBar, RadarDB


@dataclass(frozen=True)
class DivergenceResult:
    symbol: str
    divergence_pct: float


def ingest_last_6_months_daily(db: RadarDB, symbols: list[str]) -> None:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 185 * 24 * 60 * 60 * 1000  # ~6 months buffer
    for sym in symbols:
        bars = []
        for k in iter_1d_klines(sym, start_ms=start_ms):
            # kline format:
            # 0 open_time, 1 open, 2 high, 3 low, 4 close
            try:
                bars.append(
                    DailyBar(
                        symbol=sym.lower(),
                        open_time_ms=int(k[0]),
                        open=float(k[1]),
                        high=float(k[2]),
                        low=float(k[3]),
                        close=float(k[4]),
                    )
                )
            except Exception:
                continue
        if bars:
            db.upsert_daily_bars(bars)


def compute_6m_divergence(db: RadarDB, btc_symbol: str, alt_symbols: list[str], btc_drop_thr: float = -0.02) -> dict[str, float]:
    """
    For BTC days where daily return <= -2%, compute % of those days where alt return > 0.
    Returns symbol->percent (0..100)
    """
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 185 * 24 * 60 * 60 * 1000

    btc = db.load_daily_closes(btc_symbol, start_ms=start_ms)
    btc_by_t = {t: (o, c) for (t, o, c) in btc}
    btc_drop_days = set()
    for t, (o, c) in btc_by_t.items():
        if o <= 0:
            continue
        r = (c - o) / o
        if r <= btc_drop_thr:
            btc_drop_days.add(t)

    if not btc_drop_days:
        return {s.upper(): 0.0 for s in alt_symbols}

    out: dict[str, float] = {}
    for sym in alt_symbols:
        rows = db.load_daily_closes(sym, start_ms=start_ms)
        good = 0
        total = 0
        for t, o, c in rows:
            if t not in btc_drop_days:
                continue
            total += 1
            if o > 0 and (c - o) / o > 0:
                good += 1
        out[sym.upper()] = (good / total * 100.0) if total else 0.0
    return out

