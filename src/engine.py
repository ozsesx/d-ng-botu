from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any

import numpy as np
import websockets

from .config import DEFAULT_BTC_SYMBOL, Multipliers


BINANCE_FAPI_WS = "wss://fstream.binance.com/stream?streams="


def pct_change(prev: float, cur: float) -> float:
    if prev <= 0:
        return 0.0
    return (cur - prev) / prev


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def step_score(btc_pct: float, alt_pct: float) -> float:
    """
    Returns [-1, 1] for a single 4s step.

    - Outperformance is alt_pct - btc_pct
    - Special boost when BTC down but alt up (dominance)
    """
    rel = alt_pct - btc_pct
    if btc_pct < 0.0 and alt_pct > 0.0:
        rel += abs(btc_pct) + abs(alt_pct)
    # scale to keep stable in tiny pct values (4s)
    x = rel * 80.0
    return float(math.tanh(x))


def slice_multipliers(n: int, mult: Multipliers) -> np.ndarray:
    """
    Split n steps into 3 chronological equal parts:
    oldest, mid, near (most recent).
    """
    if n <= 0:
        return np.zeros(0, dtype=float)
    a = np.full(n, mult.old, dtype=float)
    third = n // 3
    if third == 0:
        a[:] = mult.near
        return a
    a[third : 2 * third] = mult.mid
    a[2 * third :] = mult.near
    # if remainder exists (n not divisible by 3), bias extra steps to "near"
    return a


@dataclass
class CoinSnapshot:
    symbol: str
    power_0_100: float
    state: int  # 1 strongest, 2 weakest, 3 deaf/stable, 0 none
    minutes_in_state: float
    next_state_hint: str
    divergence_6m_pct: float | None = None


@dataclass
class EngineSnapshot:
    ts: float
    window_steps: int
    cycle1: list[CoinSnapshot]
    cycle2: list[CoinSnapshot]
    cycle3: list[CoinSnapshot]
    matrix: dict[str, int]  # symbol->state (1/2/3/0)
    last_tick_age_s: float


class RadarEngine:
    """
    Background engine:
    - Futures websocket (miniTicker) for BTC + universe
    - 4s "close" resample
    - rolling window deques
    - scoring + hysteresis lists
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()

        self._symbols: list[str] = []
        self._btc = DEFAULT_BTC_SYMBOL

        self._latest_price: dict[str, float] = {}
        self._latest_tick_ts: float = 0.0

        self._window_steps = 15 * 60 // 4  # default 15m
        self._multipliers = Multipliers()

        self._closes: dict[str, deque[float]] = {}
        self._step_states: dict[str, deque[int]] = {}  # per-step state history for Markov

        self._current_state: dict[str, int] = {}
        self._state_since_step: dict[str, int] = {}
        self._step_idx: int = 0

        self._snapshot: EngineSnapshot | None = None

        # hysteresis thresholds
        self._enter_thr = 80.0
        self._exit_thr = 65.0

    def configure(self, *, symbols: list[str], window_seconds: int, multipliers: Multipliers) -> None:
        symbols_lc = [s.lower() for s in symbols if s]
        if self._btc not in symbols_lc:
            symbols_lc = [self._btc] + symbols_lc
        window_steps = max(3, int(window_seconds // 4))
        with self._lock:
            changed_universe = symbols_lc != self._symbols
            self._symbols = symbols_lc
            self._window_steps = window_steps
            self._multipliers = multipliers
            if changed_universe:
                self._reset_buffers_locked()

    def _reset_buffers_locked(self) -> None:
        self._latest_price = {}
        self._latest_tick_ts = 0.0
        self._closes = {s: deque(maxlen=self._window_steps + 2) for s in self._symbols}
        self._step_states = {s: deque(maxlen=self._window_steps + 5) for s in self._symbols if s != self._btc}
        self._current_state = {s: 0 for s in self._symbols if s != self._btc}
        self._state_since_step = {s: 0 for s in self._symbols if s != self._btc}
        self._step_idx = 0
        self._snapshot = None

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_evt.clear()
            self._thread = threading.Thread(target=self._run_thread, name="RadarEngine", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        t = None
        with self._lock:
            t = self._thread
        if t:
            t.join(timeout=2.0)

    def get_snapshot(self) -> EngineSnapshot:
        with self._lock:
            if self._snapshot is None:
                age = (time.time() - self._latest_tick_ts) if self._latest_tick_ts else 1e9
                return EngineSnapshot(
                    ts=time.time(),
                    window_steps=self._window_steps,
                    cycle1=[],
                    cycle2=[],
                    cycle3=[],
                    matrix={s: 0 for s in self._symbols if s != self._btc},
                    last_tick_age_s=age,
                )
            return self._snapshot

    def get_closes_pair(self, symbol: str) -> tuple[list[float], list[float]]:
        """
        Returns (btc_closes, symbol_closes) aligned by the shortest available length.
        Both are close-price series sampled at 4s.
        """
        sym = symbol.lower()
        with self._lock:
            if self._btc not in self._closes or sym not in self._closes:
                return [], []
            btc = list(self._closes[self._btc])
            alt = list(self._closes[sym])
        n = min(len(btc), len(alt))
        if n <= 2:
            return [], []
        return btc[-n:], alt[-n:]

    def get_step_state_history(self, symbol: str, max_len: int = 5000) -> list[int]:
        sym = symbol.lower()
        with self._lock:
            hist = list(self._step_states.get(sym, ()))
        if max_len and len(hist) > max_len:
            return hist[-max_len:]
        return hist

    def _run_thread(self) -> None:
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        ws_task = asyncio.create_task(self._ws_loop())
        timer_task = asyncio.create_task(self._timer_loop())
        try:
            await asyncio.wait([ws_task, timer_task], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (ws_task, timer_task):
                if not t.done():
                    t.cancel()
            with self._lock:
                self._latest_tick_ts = self._latest_tick_ts or time.time()

    async def _ws_loop(self) -> None:
        backoff = 1.0
        while not self._stop_evt.is_set():
            with self._lock:
                streams = [f"{s}@miniTicker" for s in self._symbols]
            url = BINANCE_FAPI_WS + "/".join(streams)
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
                    backoff = 1.0
                    while not self._stop_evt.is_set():
                        msg = await ws.recv()
                        data = json.loads(msg)
                        payload = data.get("data") if isinstance(data, dict) else None
                        if not isinstance(payload, dict):
                            continue
                        sym = str(payload.get("s", "")).lower()
                        if not sym:
                            continue
                        try:
                            price = float(payload.get("c"))
                        except Exception:
                            continue
                        now = time.time()
                        with self._lock:
                            if sym in self._closes:
                                self._latest_price[sym] = price
                                self._latest_tick_ts = now
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(20.0, backoff * 1.7)

    async def _timer_loop(self) -> None:
        next_t = time.time()
        while not self._stop_evt.is_set():
            now = time.time()
            if now < next_t:
                await asyncio.sleep(min(0.2, next_t - now))
                continue

            next_t += 4.0
            self._finalize_step()

    def _finalize_step(self) -> None:
        with self._lock:
            self._step_idx += 1

            # write 4s close = latest price if available; else repeat last close
            for sym, dq in self._closes.items():
                p = self._latest_price.get(sym)
                if p is None:
                    if dq:
                        dq.append(dq[-1])
                    continue
                dq.append(p)

            if len(self._closes.get(self._btc, ())) < 3:
                self._snapshot = self.get_snapshot()
                return

            # compute power score for each alt
            btc = self._btc
            btc_close = np.array(self._closes[btc], dtype=float)
            if btc_close.size < 3:
                self._snapshot = self.get_snapshot()
                return

            # align window by last window_steps+1 closes
            w = min(self._window_steps + 1, btc_close.size)
            btc_close = btc_close[-w:]
            btc_pct = np.diff(btc_close) / btc_close[:-1]
            n_steps = int(btc_pct.size)
            multipliers = slice_multipliers(n_steps, self._multipliers)
            denom = float(np.sum(np.abs(multipliers))) if n_steps else 1.0

            power_by_sym: dict[str, float] = {}
            deaf_by_sym: dict[str, float] = {}

            for sym in self._symbols:
                if sym == btc:
                    continue
                closes = np.array(self._closes[sym], dtype=float)
                if closes.size < w:
                    continue
                closes = closes[-w:]
                alt_pct = np.diff(closes) / closes[:-1]
                # step scores [-1,1]
                v = np.tanh(((alt_pct - btc_pct) * 80.0))
                # dominance boost
                dom_mask = (btc_pct < 0.0) & (alt_pct > 0.0)
                v = v + dom_mask.astype(float) * np.tanh(((np.abs(btc_pct) + np.abs(alt_pct)) * 80.0))
                v = np.clip(v, -1.0, 1.0)

                weighted = float(np.sum(v * multipliers) / denom) if n_steps else 0.0
                power = (clamp(weighted, -1.0, 1.0) + 1.0) * 50.0
                power_by_sym[sym] = power

                # "deafness": low movement + low covariance with btc
                mean_abs_alt = float(np.mean(np.abs(alt_pct))) if alt_pct.size else 0.0
                std_alt = float(np.std(alt_pct)) if alt_pct.size else 0.0
                std_btc = float(np.std(btc_pct)) if btc_pct.size else 0.0
                corr = 0.0
                if alt_pct.size and std_alt > 0 and std_btc > 0:
                    corr = float(np.corrcoef(alt_pct, btc_pct)[0, 1])
                    if math.isnan(corr):
                        corr = 0.0
                # thresholds tuned for 4s returns (very small); clamp into [0,1]
                movement = clamp(mean_abs_alt / 0.0008, 0.0, 1.0)  # 0.08% avg abs return ~ "moving"
                volatility = clamp(std_alt / 0.0012, 0.0, 1.0)
                coupling = clamp(abs(corr), 0.0, 1.0)
                deaf = 1.0 - (0.55 * movement + 0.25 * volatility + 0.20 * coupling)
                deaf_by_sym[sym] = clamp(deaf, 0.0, 1.0)

            # apply hysteresis membership updates for cycle 1/2 based on power
            # cycle1 candidates: highest power; cycle2: lowest power
            all_syms = [s for s in power_by_sym.keys()]
            if not all_syms:
                self._snapshot = self.get_snapshot()
                return

            sorted_hi = sorted(all_syms, key=lambda s: power_by_sym[s], reverse=True)
            sorted_lo = sorted(all_syms, key=lambda s: power_by_sym[s])

            def update_membership(target_state: int, ranked: list[str]) -> list[str]:
                members = [s for s, st in self._current_state.items() if st == target_state]
                # remove exits
                for s in list(members):
                    if power_by_sym.get(s, 0.0) < self._exit_thr:
                        self._current_state[s] = 0
                        self._state_since_step[s] = self._step_idx
                members = [s for s, st in self._current_state.items() if st == target_state]
                # fill up to 15 with entrants above enter_thr
                for s in ranked:
                    if len(members) >= 15:
                        break
                    if self._current_state.get(s, 0) != 0:
                        continue
                    if power_by_sym.get(s, 0.0) >= self._enter_thr:
                        self._current_state[s] = target_state
                        self._state_since_step[s] = self._step_idx
                        members.append(s)
                return members

            cycle1_members = update_membership(1, sorted_hi)
            cycle2_members = update_membership(2, sorted_lo)

            # cycle3: top deafness but exclude cycle1/2 members
            excluded = set(cycle1_members) | set(cycle2_members)
            sorted_deaf = sorted([s for s in deaf_by_sym.keys() if s not in excluded], key=lambda s: deaf_by_sym[s], reverse=True)
            cycle3_members = sorted_deaf[:15]
            for s in cycle3_members:
                if self._current_state.get(s, 0) == 0:
                    self._current_state[s] = 3
                    self._state_since_step[s] = self._step_idx
            # remove cycle3 from those that dropped out
            for s, st in list(self._current_state.items()):
                if st == 3 and s not in cycle3_members and s not in excluded:
                    self._current_state[s] = 0
                    self._state_since_step[s] = self._step_idx

            # record per-step states for Markov
            for s in self._step_states.keys():
                self._step_states[s].append(int(self._current_state.get(s, 0)))

            # build snapshots
            def minutes_in_state(sym: str) -> float:
                since = int(self._state_since_step.get(sym, self._step_idx))
                steps = max(0, self._step_idx - since)
                return steps * 4.0 / 60.0

            def elapsed_steps_in_state(sym: str) -> int:
                since = int(self._state_since_step.get(sym, self._step_idx))
                return max(0, self._step_idx - since)

            def hint(sym: str) -> str:
                # placeholder; UI supplies memory horizon and lookahead
                hist = list(self._step_states.get(sym, ()))
                if len(hist) < 20:
                    return "yetersiz veri"
                cur = hist[-1]
                trans = Counter(zip(hist[:-1], hist[1:]))
                next_counts = {b: c for (a, b), c in trans.items() if a == cur}
                if not next_counts:
                    return "yetersiz geçiş"
                best_next = max(next_counts.items(), key=lambda kv: kv[1])[0]
                total = sum(next_counts.values())
                p = next_counts[best_next] / total if total else 0.0
                return f"%{p*100:.0f} ihtimalle Döngü {best_next}"

            def mk_coin(sym: str) -> CoinSnapshot:
                st = int(self._current_state.get(sym, 0))
                return CoinSnapshot(
                    symbol=sym.upper(),
                    power_0_100=float(power_by_sym.get(sym, 0.0)),
                    state=st,
                    minutes_in_state=minutes_in_state(sym),
                    next_state_hint=hint(sym),
                )

            cycle1 = [mk_coin(s) for s in sorted(cycle1_members, key=lambda s: power_by_sym.get(s, 0.0), reverse=True)]
            cycle2 = [mk_coin(s) for s in sorted(cycle2_members, key=lambda s: power_by_sym.get(s, 0.0))]
            cycle3 = [mk_coin(s) for s in cycle3_members]

            age = (time.time() - self._latest_tick_ts) if self._latest_tick_ts else 1e9
            self._snapshot = EngineSnapshot(
                ts=time.time(),
                window_steps=self._window_steps,
                cycle1=cycle1,
                cycle2=cycle2,
                cycle3=cycle3,
                matrix={s.upper(): int(self._current_state.get(s, 0)) for s in self._current_state.keys()},
                last_tick_age_s=float(age),
            )

