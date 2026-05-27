from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .config import Multipliers
from .engine import clamp, slice_multipliers


@dataclass(frozen=True)
class TempoResult:
    step_strength: list[float]  # [-1, 1] per step (most recent last)
    weighted_strength: list[float]  # [-1, 1] per step after segment multiplier (scaled)


def compute_step_strength(btc_closes: list[float], alt_closes: list[float]) -> list[float]:
    if len(btc_closes) < 3 or len(alt_closes) < 3:
        return []
    n = min(len(btc_closes), len(alt_closes))
    btc = np.asarray(btc_closes[-n:], dtype=float)
    alt = np.asarray(alt_closes[-n:], dtype=float)
    btc_pct = np.diff(btc) / btc[:-1]
    alt_pct = np.diff(alt) / alt[:-1]

    v = np.tanh(((alt_pct - btc_pct) * 80.0))
    dom_mask = (btc_pct < 0.0) & (alt_pct > 0.0)
    v = v + dom_mask.astype(float) * np.tanh(((np.abs(btc_pct) + np.abs(alt_pct)) * 80.0))
    v = np.clip(v, -1.0, 1.0)
    return [float(x) for x in v]


def apply_tempo_multipliers(step_strength: list[float], mult: Multipliers) -> TempoResult:
    n = len(step_strength)
    if n == 0:
        return TempoResult(step_strength=[], weighted_strength=[])
    w = slice_multipliers(n, mult)  # oldest->near
    s = np.asarray(step_strength, dtype=float)
    weighted = s * w
    # normalize weighted per-step into [-1,1] for coloring (keep sign, compress large mult)
    m = float(np.max(np.abs(weighted))) if n else 1.0
    if m <= 0:
        ws = weighted
    else:
        ws = np.tanh(weighted / m * 2.0)
    ws = np.clip(ws, -1.0, 1.0)
    return TempoResult(step_strength=[float(x) for x in s], weighted_strength=[float(x) for x in ws])


def strength_to_rgb(x: float) -> str:
    """
    x in [-1, 1]. -1 -> dark red, 0 -> gray, +1 -> bright green.
    Returns CSS rgb() string.
    """
    x = clamp(float(x), -1.0, 1.0)
    if x >= 0:
        # gray->green
        g = int(60 + 195 * x)
        r = int(80 * (1 - x))
        b = int(80 * (1 - x))
    else:
        # gray->red
        xx = -x
        r = int(60 + 195 * xx)
        g = int(80 * (1 - xx))
        b = int(80 * (1 - xx))
    return f"rgb({r},{g},{b})"


def dwell_stats(states: list[int], step_seconds: int = 4) -> dict[int, dict[str, float]]:
    """
    Compute average dwell durations per state (in minutes).
    states: sequence like [0,0,1,1,1,3,3, ...]
    """
    if not states:
        return {}
    out: dict[int, list[int]] = {}
    cur = states[0]
    run = 1
    for s in states[1:]:
        if s == cur:
            run += 1
        else:
            out.setdefault(cur, []).append(run)
            cur = s
            run = 1
    out.setdefault(cur, []).append(run)

    res: dict[int, dict[str, float]] = {}
    for st, runs in out.items():
        mins = [r * step_seconds / 60.0 for r in runs]
        res[st] = {
            "count": float(len(runs)),
            "avg_min": float(np.mean(mins)) if mins else 0.0,
            "p50_min": float(np.median(mins)) if mins else 0.0,
        }
    return res


def next_transition_hint(states: list[int], elapsed_steps_in_state: int, lookahead_min: float, step_seconds: int = 4) -> str:
    if len(states) < 30:
        return "yetersiz veri"

    # collect dwell lengths of the current state from history (exclude trailing run)
    cur = states[-1]
    runs: list[int] = []
    st = states[0]
    run = 1
    for s in states[1:]:
        if s == st:
            run += 1
        else:
            if st == cur:
                runs.append(run)
            st = s
            run = 1

    if not runs:
        return "yetersiz geçiş"

    horizon_steps = int(max(1, lookahead_min * 60 / step_seconds))
    cutoff = elapsed_steps_in_state + horizon_steps
    p = sum(1 for r in runs if r <= cutoff) / len(runs)

    avg = float(np.mean(runs))
    exp_rem_steps = max(0.0, avg - elapsed_steps_in_state)
    exp_rem_min = exp_rem_steps * step_seconds / 60.0

    return f"%{p*100:.0f} ihtimalle {lookahead_min:.0f} dk içinde değişim | beklenen kalan: {exp_rem_min:.1f} dk"

