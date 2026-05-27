from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float


class RadarDB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        return con

    def _init(self) -> None:
        con = self._connect()
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_bars (
                  symbol TEXT NOT NULL,
                  open_time_ms INTEGER NOT NULL,
                  open REAL NOT NULL,
                  high REAL NOT NULL,
                  low REAL NOT NULL,
                  close REAL NOT NULL,
                  PRIMARY KEY(symbol, open_time_ms)
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_daily_symbol_time ON daily_bars(symbol, open_time_ms);")
            con.commit()
        finally:
            con.close()

    def upsert_daily_bars(self, bars: Iterable[DailyBar]) -> int:
        con = self._connect()
        try:
            rows = [
                (b.symbol.lower(), int(b.open_time_ms), float(b.open), float(b.high), float(b.low), float(b.close))
                for b in bars
            ]
            con.executemany(
                """
                INSERT INTO daily_bars(symbol, open_time_ms, open, high, low, close)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(symbol, open_time_ms) DO UPDATE SET
                  open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close
                """,
                rows,
            )
            con.commit()
            return len(rows)
        finally:
            con.close()

    def load_daily_closes(self, symbol: str, start_ms: int) -> list[tuple[int, float, float]]:
        """
        Returns [(open_time_ms, open, close), ...]
        """
        con = self._connect()
        try:
            cur = con.execute(
                "SELECT open_time_ms, open, close FROM daily_bars WHERE symbol=? AND open_time_ms>=? ORDER BY open_time_ms",
                (symbol.lower(), int(start_ms)),
            )
            return [(int(t), float(o), float(c)) for (t, o, c) in cur.fetchall()]
        finally:
            con.close()

