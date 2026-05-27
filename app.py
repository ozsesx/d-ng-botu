from __future__ import annotations

import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

# Streamlit Cloud: repo kökünü Python yoluna ekle
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st
from streamlit_autorefresh import st_autorefresh
import numpy as np

import plotly.graph_objects as go

# GitHub'da dosyalar src/ içinde VEYA repo kökünde olabilir (ikisini de destekle)
if (_ROOT / "src" / "binance_api.py").exists():
    from src.binance_api import top_usdt_perp_by_quote_volume
    from src.config import Multipliers
    from src.engine import RadarEngine
    from src.analytics import apply_tempo_multipliers, compute_step_strength, next_transition_hint, strength_to_rgb
    from src.db import RadarDB
    from src.memory import compute_6m_divergence, ingest_last_6_months_daily
else:
    from binance_api import top_usdt_perp_by_quote_volume
    from config import Multipliers
    from engine import RadarEngine
    from analytics import apply_tempo_multipliers, compute_step_strength, next_transition_hint, strength_to_rgb
    from db import RadarDB
    from memory import compute_6m_divergence, ingest_last_6_months_daily


WINDOW_PRESETS = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
    "6h": 6 * 60 * 60,
    "1d": 24 * 60 * 60,
    "3d": 3 * 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "15d": 15 * 24 * 60 * 60,
}

PRED_PRESETS = {
    "1d": 24 * 60 * 60,
    "3d": 3 * 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "15d": 15 * 24 * 60 * 60,
}

# Streamlit Cloud: Binance REST engellenince kullanılacak sabit 80 coin
_BUILTIN_FALLBACK_80: tuple[str, ...] = (
    "ethusdt", "solusdt", "bnbusdt", "xrpusdt", "dogeusdt", "adausdt", "trxusdt",
    "linkusdt", "avaxusdt", "suiusdt", "xlmusdt", "bchusdt", "hbarusdt", "ltcusdt",
    "nearusdt", "aptusdt", "arbusdt", "opusdt", "filusdt", "injusdt", "atomusdt",
    "etcusdt", "icpusdt", "renderusdt", "fetusdt", "wldusdt", "pepeusdt", "shibusdt",
    "dotusdt", "polusdt", "uniusdt", "aaveusdt", "algousdt", "sandusdt", "manausdt",
    "grtusdt", "ftmusdt", "runeusdt", "thetausdt", "eosusdt", "xtzusdt", "flowusdt",
    "neousdt", "kavausdt", "egldusdt", "axsusdt", "chzusdt", "crvusdt", "ldousdt",
    "mkrusdt", "snxusdt", "compusdt", "1inchusdt", "galausdt", "enjusdt", "dydxusdt",
    "imxusdt", "gmtusdt", "apeusdt", "lrcusdt", "celousdt", "zilusdt", "iotausdt",
    "qtumusdt", "zecusdt", "dashusdt", "ksmusdt", "blzusdt", "sfpusdt", "cfxusdt",
    "magicusdt", "pendleusdt", "seiusdt", "tiausdt", "wifusdt", "ondousdt", "jupusdt",
    "enausdt", "notusdt", "tonusdt", "bomeusdt",
)


def _fallback_symbols(limit: int = 80) -> list[str]:
    return list(_BUILTIN_FALLBACK_80[:limit])


def get_engine() -> RadarEngine:
    if "engine" not in st.session_state:
        st.session_state.engine = RadarEngine()
    return st.session_state.engine


def ensure_state() -> None:
    st.session_state.setdefault("universe_mode", "Top 80 (24h hacim)")
    st.session_state.setdefault("symbols_text", "")
    st.session_state.setdefault("window_key_global", "15m")
    st.session_state.setdefault("mult_global", {"near": 4.5, "mid": 1.5, "old": 0.2})

    st.session_state.setdefault("selected_coin", "SOLUSDT")
    st.session_state.setdefault("coin_overrides", {})  # coin-> {window_key, mult{near,mid,old}}
    st.session_state.setdefault("pred_memory_key", "1d")

    st.session_state.setdefault("kasa_tl", 30000.0)
    st.session_state.setdefault("risk_tl", 2000.0)
    st.session_state.setdefault("kaldirac", 10.0)
    st.session_state.setdefault("div6m", {})  # symbol->pct


def mult_from_dict(d: dict) -> Multipliers:
    return Multipliers(near=float(d["near"]), mid=float(d["mid"]), old=float(d["old"]))


def calc_contract_size_usdt(kasa_tl: float, risk_tl: float, leverage: float) -> float:
    # Basit yaklaşım: risk * kaldıraç kadar pozisyon büyüklüğü.
    # (Gerçek hesap: stop mesafesi vb. ile değişir; burada UI için canlı hesap makinesi.)
    leverage = max(1.0, float(leverage))
    return max(0.0, float(risk_tl) * leverage)


def universe_from_ui() -> list[str]:
    mode = st.session_state.universe_mode
    if mode == "Top 80 (24h hacim)":
        return get_cached_universe(80)
    # manuel
    raw = st.session_state.symbols_text.strip()
    if not raw:
        return get_cached_universe(80)
    parts = []
    for line in raw.replace(",", "\n").splitlines():
        s = line.strip().lower()
        if not s:
            continue
        if not s.endswith("usdt"):
            s += "usdt"
        parts.append(s)
    # unique preserving order
    seen = set()
    out = []
    for s in parts:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= 80:
            break
    return out


def _is_streamlit_cloud() -> bool:
    """Streamlit Cloud'da Binance REST genelde engellenir."""
    if os.environ.get("STREAMLIT_RUNTIME_ENVIRONMENT") == "cloud":
        return True
    return str(_ROOT).startswith("/mount/src")


def get_cached_universe(limit: int = 80) -> list[str]:
    """Cloud'da REST çağrılmaz; yerelde dene, hata olursa sabit listeye düş."""
    cache_key = f"universe_{limit}"
    if cache_key not in st.session_state:
        symbols = _fallback_symbols(limit)
        source = "fallback"

        if not _is_streamlit_cloud():
            try:
                symbols, source = top_usdt_perp_by_quote_volume(limit)
            except Exception:
                pass

        st.session_state[cache_key] = symbols
        st.session_state["universe_source"] = source
    return st.session_state[cache_key]


def apply_global_config(engine: RadarEngine) -> None:
    window_seconds = WINDOW_PRESETS[st.session_state.window_key_global]
    mult = mult_from_dict(st.session_state.mult_global)
    symbols = universe_from_ui()
    engine.configure(symbols=symbols, window_seconds=window_seconds, multipliers=mult)
    engine.start()


def apply_coin_override(engine: RadarEngine, coin: str) -> tuple[int, Multipliers]:
    coin = coin.upper()
    overrides = st.session_state.coin_overrides.get(coin)
    if not overrides:
        return WINDOW_PRESETS[st.session_state.window_key_global], mult_from_dict(st.session_state.mult_global)
    return WINDOW_PRESETS[overrides["window_key"]], mult_from_dict(overrides["mult"])


def render_cycle_column(engine: RadarEngine, title: str, rows) -> None:
    st.subheader(title)
    if not rows:
        st.caption("Henüz veri yok.")
        return
    for r in rows:
        div = st.session_state.get("div6m", {}).get(r.symbol, None)
        tag = f" |  6A Ayrışma: **%{div:.0f}**" if isinstance(div, (int, float)) else ""
        mem_s = PRED_PRESETS.get(st.session_state.pred_memory_key, 24 * 60 * 60)
        mem_steps = int(mem_s // 4)
        hist = engine.get_step_state_history(r.symbol, max_len=mem_steps)
        # elapsed steps in current state from tail
        elapsed = 0
        if hist:
            cur = hist[-1]
            i = len(hist) - 1
            while i >= 0 and hist[i] == cur:
                elapsed += 1
                i -= 1
        hint = next_transition_hint(hist, elapsed_steps_in_state=elapsed, lookahead_min=30.0)
        st.write(
            f"**{r.symbol}**  |  Güç: **{r.power_0_100:.1f}**{tag}  |  Sayaç: **{r.minutes_in_state:.1f} dk**  |  Tahmin: {hint}"
        )


def render_matrix(matrix: dict[str, int]) -> None:
    # lightweight colored squares using markdown
    # 1 green, 2 red, 3 gray, 0 dark
    order = sorted(matrix.keys())
    cols = 16
    rows = (len(order) + cols - 1) // cols
    for i in range(rows):
        chunk = order[i * cols : (i + 1) * cols]
        line = []
        for sym in chunk:
            stt = matrix.get(sym, 0)
            if stt == 1:
                c = "🟩"
            elif stt == 2:
                c = "🟥"
            elif stt == 3:
                c = "⬜"
            else:
                c = "⬛"
            line.append(f"{c}{sym.replace('USDT','')}")
        st.write(" ".join(line))


def main() -> None:
    st.set_page_config(page_title="4s Radar", layout="wide")
    ensure_state()

    # 4 saniyede bir otomatik yenileme (engine background)
    st_autorefresh(interval=4000, key="autorefresh")

    engine = get_engine()
    apply_global_config(engine)
    db = RadarDB("radar.db")

    tab1, tab2 = st.tabs(["CANLI İZLEME ODASI (Genel Radar)", "RÖNTGEN ODASI (Coin Özel)"])

    with tab1:
        with st.container():
            c1, c2, c3, c4 = st.columns([2.2, 1.6, 1.6, 1.6])

            with c1:
                st.markdown("### Zaman Penceresi")
                st.session_state.window_key_global = st.radio(
                    "Zaman",
                    options=list(WINDOW_PRESETS.keys()),
                    horizontal=True,
                    index=list(WINDOW_PRESETS.keys()).index(st.session_state.window_key_global),
                    label_visibility="collapsed",
                )
                st.markdown("### Tahmin Hafızası (Markov)")
                st.session_state.pred_memory_key = st.radio(
                    "Hafıza",
                    options=list(PRED_PRESETS.keys()),
                    horizontal=True,
                    index=list(PRED_PRESETS.keys()).index(st.session_state.pred_memory_key),
                    label_visibility="collapsed",
                )

            with c2:
                st.markdown("### Çarpanlar (Yakın / Orta / Eski)")
                st.session_state.mult_global["near"] = st.number_input("Yakın", value=float(st.session_state.mult_global["near"]), step=0.1)
                st.session_state.mult_global["mid"] = st.number_input("Orta", value=float(st.session_state.mult_global["mid"]), step=0.1)
                st.session_state.mult_global["old"] = st.number_input("Eski", value=float(st.session_state.mult_global["old"]), step=0.1)

            with c3:
                st.markdown("### Evren (80 coin)")
                st.session_state.universe_mode = st.selectbox(
                    "Kaynak",
                    ["Top 80 (24h hacim)", "Manuel liste"],
                    index=0 if st.session_state.universe_mode == "Top 80 (24h hacim)" else 1,
                )
                if st.session_state.universe_mode == "Manuel liste":
                    st.session_state.symbols_text = st.text_area(
                        "Semboller (virgül veya satır):",
                        value=st.session_state.symbols_text,
                        height=120,
                        placeholder="SOLUSDT, ETHUSDT, ...",
                    )
                else:
                    st.caption("Likiditeye göre otomatik seçilir.")

                if st.button("6A Hafızayı Güncelle (SQLite)", use_container_width=True):
                    if _is_streamlit_cloud():
                        st.warning("6A hafıza Streamlit Cloud'da Binance REST engeli nedeniyle çalışmayabilir.")
                    try:
                        syms = [s.lower() for s in universe_from_ui()]
                        ingest_last_6_months_daily(db, symbols=["btcusdt"] + syms)
                        st.session_state.div6m = compute_6m_divergence(db, "btcusdt", syms)
                        st.success("6 aylık hafıza güncellendi.")
                    except Exception as exc:
                        st.error(f"6A hafıza güncellenemedi: {exc}")

            with c4:
                st.markdown("### Kasa / Risk / Kaldıraç")
                st.session_state.kasa_tl = st.number_input("Kasa (TL)", value=float(st.session_state.kasa_tl), step=100.0)
                st.session_state.risk_tl = st.number_input("Risk (TL)", value=float(st.session_state.risk_tl), step=50.0)
                st.session_state.kaldirac = st.number_input("Kaldıraç", value=float(st.session_state.kaldirac), step=1.0, min_value=1.0)
                usdt = calc_contract_size_usdt(st.session_state.kasa_tl, st.session_state.risk_tl, st.session_state.kaldirac)
                st.metric("Açılması Gereken Kontrat Büyüklüğü (USDT)", f"{usdt:,.0f}")

        snap = engine.get_snapshot()
        if st.session_state.get("universe_source") == "fallback":
            st.info(
                "Binance REST API bu sunucudan engellendi (Streamlit Cloud ABD IP). "
                "80 coin sabit listeden yüklendi; canlı websocket verisi gelmeye devam edebilir."
            )
        if snap.last_tick_age_s > 15:
            st.warning(f"Websocket verisi gecikiyor (son tick: {snap.last_tick_age_s:.0f}s).")

        colA, colB, colC = st.columns(3)
        with colA:
            render_cycle_column(engine, "DÖNGÜ 1 (En Güçlü 15)", snap.cycle1)
        with colB:
            render_cycle_column(engine, "DÖNGÜ 2 (En Çürük 15)", snap.cycle2)
        with colC:
            render_cycle_column(engine, "DÖNGÜ 3 (Sağır / Stabil 15)", snap.cycle3)

        st.markdown("### 80 Coin Durum Matrisi (Yeşil/Kırmızı/Gri)")
        render_matrix(snap.matrix)

    with tab2:
        symbols = sorted(list(snap.matrix.keys()))
        if st.session_state.selected_coin not in symbols and symbols:
            st.session_state.selected_coin = symbols[0]

        top_bar = st.columns([1.2, 2.2, 2.2])
        with top_bar[0]:
            st.markdown("### Coin Seç")
            st.session_state.selected_coin = st.selectbox("Coin", options=symbols if symbols else ["SOLUSDT"], index=0)
        coin = st.session_state.selected_coin

        # per-coin override state (isolated)
        ov = st.session_state.coin_overrides.get(coin)
        if not ov:
            ov = {"window_key": st.session_state.window_key_global, "mult": dict(st.session_state.mult_global)}
            st.session_state.coin_overrides[coin] = ov

        with top_bar[1]:
            st.markdown("### Coin Özel Zaman")
            ov["window_key"] = st.radio("Zaman (coin)", options=list(WINDOW_PRESETS.keys()), horizontal=True, index=list(WINDOW_PRESETS.keys()).index(ov["window_key"]), label_visibility="collapsed")
        with top_bar[2]:
            st.markdown("### Coin Özel Çarpanlar")
            ov["mult"]["near"] = st.number_input("Yakın (coin)", value=float(ov["mult"]["near"]), step=0.1)
            ov["mult"]["mid"] = st.number_input("Orta (coin)", value=float(ov["mult"]["mid"]), step=0.1)
            ov["mult"]["old"] = st.number_input("Eski (coin)", value=float(ov["mult"]["old"]), step=0.1)

        st.session_state.coin_overrides[coin] = ov

        st.markdown("### Tempo Şeridi Isı Haritası (4s adımlar)")
        win_s, mult = apply_coin_override(engine, coin)
        btc_closes, alt_closes = engine.get_closes_pair(coin)
        # window apply
        steps = int(win_s // 4)
        need = steps + 1
        if len(btc_closes) > need:
            btc_closes = btc_closes[-need:]
            alt_closes = alt_closes[-need:]

        step_strength = compute_step_strength(btc_closes, alt_closes)  # oldest->near
        tempo = apply_tempo_multipliers(step_strength, mult)
        if not tempo.weighted_strength:
            st.caption("Tempo şeridi için yeterli 4s adım birikiyor...")
        else:
            # render as 1-row heatmap: past (left) -> now (right)
            z = [tempo.weighted_strength]
            colors = []
            for x in np.linspace(-1, 1, 11):
                colors.append([float((x + 1) / 2), strength_to_rgb(float(x))])

            fig = go.Figure(
                data=go.Heatmap(
                    z=z,
                    zmin=-1,
                    zmax=1,
                    colorscale=colors,
                    showscale=False,
                    hovertemplate="Adım Gücü: %{z:.2f}<extra></extra>",
                )
            )
            fig.update_layout(
                height=140,
                margin=dict(l=10, r=10, t=10, b=10),
                xaxis=dict(showticklabels=False, visible=False),
                yaxis=dict(showticklabels=False, visible=False),
            )
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
            st.caption("Sol: geçmiş | Sağ: ŞU AN. Yeşil: baskın, Kırmızı: ezik, Gri: sağır.")

        st.markdown("### Debug Snapshot (opsiyonel)")
        with st.expander("Engine Snapshot"):
            st.json(asdict(snap))


if __name__ == "__main__":
    main()

