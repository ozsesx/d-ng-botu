from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Multipliers:
    near: float = 4.5
    mid: float = 1.5
    old: float = 0.2


DEFAULT_BTC_SYMBOL = "btcusdt"

# Basit bir başlangıç listesi (kullanıcı UI'dan değiştirebilir).
DEFAULT_ALTS = [
    "ethusdt",
    "solusdt",
    "bnbusdt",
    "xrpusdt",
    "adausdt",
    "dogeusdt",
    "linkusdt",
    "avaxusdt",
    "dotusdt",
    "maticusdt",
    "atomusdt",
    "ltcusdt",
    "bchusdt",
    "trxusdt",
    "xlmusdt",
    "aptusdt",
    "arbusdt",
    "opususdt",
    "suiusdt",
    "nearusdt",
]

