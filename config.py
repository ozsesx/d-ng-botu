from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Multipliers:
    near: float = 4.5
    mid: float = 1.5
    old: float = 0.2


DEFAULT_BTC_SYMBOL = "btcusdt"

# Binance REST engellenirse (Streamlit Cloud ABD IP) kullanılacak sabit 80 coin
FALLBACK_UNIVERSE_80: tuple[str, ...] = (
    "ethusdt",
    "solusdt",
    "bnbusdt",
    "xrpusdt",
    "dogeusdt",
    "adausdt",
    "trxusdt",
    "linkusdt",
    "avaxusdt",
    "suiusdt",
    "xlmusdt",
    "bchusdt",
    "hbarusdt",
    "ltcusdt",
    "nearusdt",
    "aptusdt",
    "arbusdt",
    "opusdt",
    "filusdt",
    "injusdt",
    "atomusdt",
    "etcusdt",
    "icpusdt",
    "renderusdt",
    "fetusdt",
    "wldusdt",
    "pepeusdt",
    "shibusdt",
    "dotusdt",
    "polusdt",
    "uniusdt",
    "aaveusdt",
    "algousdt",
    "sandusdt",
    "manausdt",
    "grtusdt",
    "ftmusdt",
    "runeusdt",
    "thetausdt",
    "eosusdt",
    "xtzusdt",
    "flowusdt",
    "neousdt",
    "kavausdt",
    "egldusdt",
    "axsusdt",
    "chzusdt",
    "crvusdt",
    "ldousdt",
    "mkrusdt",
    "snxusdt",
    "compusdt",
    "1inchusdt",
    "galausdt",
    "enjusdt",
    "dydxusdt",
    "imxusdt",
    "gmtusdt",
    "apeusdt",
    "lrcusdt",
    "celousdt",
    "zilusdt",
    "iotausdt",
    "qtumusdt",
    "zecusdt",
    "dashusdt",
    "ksmusdt",
    "blzusdt",
    "sfpusdt",
    "cfxusdt",
    "magicusdt",
    "pendleusdt",
    "seiusdt",
    "tiausdt",
    "wifusdt",
    "ondousdt",
    "jupusdt",
    "enausdt",
    "notusdt",
    "tonusdt",
    "bomeusdt",
)


def fallback_universe(limit: int = 80) -> list[str]:
    return list(FALLBACK_UNIVERSE_80[:limit])
