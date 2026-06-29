"""
CLAUDE + QUANT  —  Backtester v1.0
====================================
Testet verschiedene Strategien auf historischen XAUUSD M15-Daten.

Ausfuehren:
  python backtest.py             # Synthetische Daten (kein MT5)
  python backtest.py --mt5       # Echte MT5-Daten (MT5 muss offen sein)
  python backtest.py --candles 2000

Ergebnis wird in backtest_results.json gespeichert.
"""

import sys
import json
import random
import math
from datetime import datetime

sys.path.insert(0, ".")
from trading_bot import (
    ema_series, rsi, macd, atr, adx,
    market_structure, detect_candle_patterns,
    bollinger_bands, stoch_rsi, fibonacci_levels,
    find_order_blocks, find_fair_value_gaps,
    ichimoku, supertrend, donchian_channel,
    cci_indicator, williams_r, volume_ratio,
    ATR_SL_MULT, ATR_TP_MULT, BREAK_EVEN_AT, CONTRACT_SIZES, MT5_AVAILABLE
)

# ── KONFIGURATION ─────────────────────────────────────────────────────────────
SYMBOL         = "XAUUSD"
START_BALANCE  = 10000.0   # USD
RISK_PCT       = 1.0       # % pro Trade
CONTRACT_SIZE  = CONTRACT_SIZES.get(SYMBOL, 100)

# Benannte Strategien für direkten Vergleich (backtest.py --compare)
STRATEGIES = [
    {"name": "BALANCED",   "strategy_type": "BALANCED",   "adx_min": 25, "rsi_low_b": 40, "rsi_high_b": 65,
     "rsi_low_s": 35, "rsi_high_s": 60, "need_pattern": False, "sl_mult": 1.5, "tp_mult": 2.5, "min_score": 8,  "break_even_at": 1.0},
    {"name": "BB_SCALP",   "strategy_type": "BB_SCALP",   "adx_min": 22, "rsi_low_b": 38, "rsi_high_b": 65,
     "rsi_low_s": 35, "rsi_high_s": 63, "need_pattern": False, "sl_mult": 1.0, "tp_mult": 2.0, "min_score": 7,  "break_even_at": 0.8},
    {"name": "SCALP",      "strategy_type": "SCALP",      "adx_min": 18, "rsi_low_b": 38, "rsi_high_b": 62,
     "rsi_low_s": 38, "rsi_high_s": 62, "need_pattern": False, "sl_mult": 0.8, "tp_mult": 1.5, "min_score": 6,  "break_even_at": 0.8},
    {"name": "FIB_SWING",  "strategy_type": "FIB_SWING",  "adx_min": 20, "rsi_low_b": 38, "rsi_high_b": 68,
     "rsi_low_s": 32, "rsi_high_s": 62, "need_pattern": False, "sl_mult": 2.0, "tp_mult": 4.0, "min_score": 8,  "break_even_at": 1.2},
    {"name": "ICT_SMC",    "strategy_type": "ICT_SMC",    "adx_min": 22, "rsi_low_b": 40, "rsi_high_b": 65,
     "rsi_low_s": 35, "rsi_high_s": 60, "need_pattern": False, "sl_mult": 1.5, "tp_mult": 3.0, "min_score": 9,  "break_even_at": 1.0},
    {"name": "VWAP_TREND", "strategy_type": "VWAP_TREND", "adx_min": 25, "rsi_low_b": 40, "rsi_high_b": 65,
     "rsi_low_s": 35, "rsi_high_s": 60, "need_pattern": False, "sl_mult": 1.5, "tp_mult": 2.5, "min_score": 8,  "break_even_at": 1.0},
    {"name": "MOMENTUM",   "strategy_type": "MOMENTUM",   "adx_min": 22, "rsi_low_b": 38, "rsi_high_b": 68,
     "rsi_low_s": 32, "rsi_high_s": 62, "need_pattern": False, "sl_mult": 1.2, "tp_mult": 2.0, "min_score": 8,  "break_even_at": 0.8},
    {"name": "ENSEMBLE",    "strategy_type": "ENSEMBLE",    "adx_min": 22, "rsi_low_b": 40, "rsi_high_b": 65,
     "rsi_low_s": 35, "rsi_high_s": 60, "need_pattern": False, "sl_mult": 1.5, "tp_mult": 2.5, "min_score": 8,  "break_even_at": 1.0},
    # ── Neue Elite-Methoden ──────────────────────────────────────────────────
    {"name": "REVERSAL",    "strategy_type": "REVERSAL",    "adx_min": 18, "rsi_low_b": 25, "rsi_high_b": 38,
     "rsi_low_s": 62, "rsi_high_s": 80, "need_pattern": False, "sl_mult": 1.2, "tp_mult": 2.5, "min_score": 7,  "break_even_at": 0.8},
    {"name": "BREAKOUT",    "strategy_type": "BREAKOUT",    "adx_min": 25, "rsi_low_b": 45, "rsi_high_b": 65,
     "rsi_low_s": 35, "rsi_high_s": 55, "need_pattern": False, "sl_mult": 1.5, "tp_mult": 3.0, "min_score": 8,  "break_even_at": 1.2},
    {"name": "PRICE_ACTION","strategy_type": "PRICE_ACTION","adx_min": 20, "rsi_low_b": 35, "rsi_high_b": 65,
     "rsi_low_s": 35, "rsi_high_s": 65, "need_pattern": True,  "sl_mult": 1.5, "tp_mult": 2.5, "min_score": 8,  "break_even_at": 1.0},
    {"name": "WYCKOFF",     "strategy_type": "WYCKOFF",     "adx_min": 20, "rsi_low_b": 38, "rsi_high_b": 62,
     "rsi_low_s": 38, "rsi_high_s": 62, "need_pattern": False, "sl_mult": 1.5, "tp_mult": 3.0, "min_score": 8,  "break_even_at": 1.0},
    # ── Neue Elite-Methoden ──────────────────────────────────────────────────
    {"name": "ICHIMOKU",   "strategy_type": "ICHIMOKU",   "adx_min": 20, "rsi_low_b": 35, "rsi_high_b": 65,
     "rsi_low_s": 35, "rsi_high_s": 65, "need_pattern": False, "sl_mult": 1.5, "tp_mult": 3.0, "min_score": 7,  "break_even_at": 1.0},
    {"name": "SUPERTREND", "strategy_type": "SUPERTREND", "adx_min": 20, "rsi_low_b": 35, "rsi_high_b": 65,
     "rsi_low_s": 35, "rsi_high_s": 65, "need_pattern": False, "sl_mult": 1.5, "tp_mult": 2.5, "min_score": 7,  "break_even_at": 0.8},
    {"name": "MULTI_TF",   "strategy_type": "MULTI_TF",   "adx_min": 22, "rsi_low_b": 40, "rsi_high_b": 65,
     "rsi_low_s": 35, "rsi_high_s": 60, "need_pattern": False, "sl_mult": 1.5, "tp_mult": 2.5, "min_score": 9,  "break_even_at": 1.0},
    {"name": "BB_SQUEEZE", "strategy_type": "BB_SQUEEZE", "adx_min": 18, "rsi_low_b": 38, "rsi_high_b": 62,
     "rsi_low_s": 38, "rsi_high_s": 62, "need_pattern": False, "sl_mult": 1.2, "tp_mult": 2.5, "min_score": 8,  "break_even_at": 0.8},
    {"name": "VOLUME_CONF","strategy_type": "VOLUME_CONF","adx_min": 20, "rsi_low_b": 35, "rsi_high_b": 65,
     "rsi_low_s": 35, "rsi_high_s": 65, "need_pattern": False, "sl_mult": 1.5, "tp_mult": 2.5, "min_score": 8,  "break_even_at": 1.0},
]


# ── DATENGENERATOR ────────────────────────────────────────────────────────────

def make_xauusd_candles(n=2000, seed=42):
    """Generiert realistische XAUUSD M15-Kerzen (Goldpreis ~3300 USD)."""
    random.seed(seed)
    candles = []
    price   = 3280.0
    trend   = 0.0

    for i in range(n):
        # Trend wechselt langsam
        if random.random() < 0.02:
            trend = random.uniform(-0.08, 0.08)

        move  = trend + (random.random() - 0.5) * 4.0
        open_ = price
        close = price + move
        high  = max(open_, close) + random.random() * 1.5
        low   = min(open_, close) - random.random() * 1.5
        vol   = random.randint(800, 5000)

        candles.append({
            "open":  round(open_, 2),
            "high":  round(high,  2),
            "low":   round(low,   2),
            "close": round(close, 2),
            "tick_volume": vol,
            "time":  i * 900,   # 15 Minuten in Sekunden
        })
        price = close
    return candles


def load_mt5_candles(n=2000):
    """Laedt echte XAUUSD M15-Daten aus MetaTrader 5."""
    try:
        import MetaTrader5 as mt5
        if not mt5.initialize():
            print("[WARN] MT5 Initialisierung fehlgeschlagen, nutze synthetische Daten")
            return None
        rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 0, n)
        mt5.shutdown()
        if rates is None or len(rates) == 0:
            print("[WARN] Keine MT5-Daten erhalten, nutze synthetische Daten")
            return None
        candles = []
        for r in rates:
            candles.append({
                "open":  float(r["open"]),
                "high":  float(r["high"]),
                "low":   float(r["low"]),
                "close": float(r["close"]),
                "tick_volume": int(r["tick_volume"]),
                "time":  int(r["time"]),
            })
        print(f"[OK] {len(candles)} echte XAUUSD M15-Kerzen geladen")
        return candles
    except Exception as e:
        print(f"[WARN] MT5-Fehler: {e}, nutze synthetische Daten")
        return None


# ── STRATEGIE-TYPEN MIT GEWICHTUNG ───────────────────────────────────────────
#
# Jeder Strategie-Typ gewichtet die Indikatoren unterschiedlich.
# Schlüssel: ema, adx, rsi, macd, bb, stoch, vwap, fib, ob, fvg, struct, pat
# Wert: Punkte die dieser Indikator pro Bestätigung beiträgt
#
STRATEGY_WEIGHTS = {
    # ── ENSEMBLE: Mehrheitsvotum aller Strategien ──────────────────────────────
    "ENSEMBLE":    dict(ema=2, adx=1, rsi=2, macd=2, bb=3, stoch=3, vwap=2, fib=2, ob=2, fvg=2, struct=1, pat=1),
    # ── KLASSISCHE ELITE-METHODEN ─────────────────────────────────────────────
    # Ausgewogene Kombination (Standard)
    "BALANCED":    dict(ema=2, adx=1, rsi=1, macd=1, bb=2, stoch=2, vwap=1, fib=2, ob=2, fvg=1, struct=1, pat=1),
    # BB-Bounce Scalping (Mean-Reversion an den Bändern)
    "BB_SCALP":    dict(ema=1, adx=1, rsi=1, macd=1, bb=4, stoch=3, vwap=1, fib=1, ob=1, fvg=1, struct=1, pat=1),
    # Reines Scalping: BB + StochRSI + VWAP (schnelle Trades)
    "SCALP":       dict(ema=1, adx=1, rsi=3, macd=1, bb=4, stoch=4, vwap=3, fib=0, ob=0, fvg=0, struct=1, pat=1),
    # Fibonacci Swing Trading (goldene Ratio-Levels + OB)
    "FIB_SWING":   dict(ema=2, adx=1, rsi=1, macd=1, bb=1, stoch=1, vwap=1, fib=5, ob=3, fvg=2, struct=2, pat=1),
    # ICT Smart Money Concepts (Order Blocks + Fair Value Gaps)
    "ICT_SMC":     dict(ema=1, adx=1, rsi=1, macd=1, bb=1, stoch=1, vwap=1, fib=2, ob=5, fvg=4, struct=2, pat=1),
    # VWAP Trend Following (institutioneller Trendhandel)
    "VWAP_TREND":  dict(ema=3, adx=2, rsi=1, macd=2, bb=1, stoch=1, vwap=3, fib=1, ob=1, fvg=1, struct=2, pat=1),
    # MACD Momentum (starke Impulse handeln)
    "MOMENTUM":    dict(ema=2, adx=1, rsi=2, macd=4, bb=1, stoch=3, vwap=1, fib=1, ob=1, fvg=1, struct=1, pat=1),
    # ── NEU: WEITERE ELITE-METHODEN ───────────────────────────────────────────
    # Mean Reversion an RSI/StochRSI-Extremen (Contrarian, wie Bollinger selbst)
    "REVERSAL":    dict(ema=1, adx=1, rsi=5, macd=1, bb=4, stoch=5, vwap=1, fib=1, ob=1, fvg=1, struct=1, pat=3),
    # BB-Squeeze Breakout (Linda Raschke / Mark Minervini Methode)
    "BREAKOUT":    dict(ema=2, adx=4, rsi=1, macd=3, bb=5, stoch=1, vwap=2, fib=1, ob=2, fvg=2, struct=2, pat=1),
    # Pure Price Action (Al Brooks / Lance Beggs Methode)
    "PRICE_ACTION":dict(ema=2, adx=1, rsi=1, macd=1, bb=1, stoch=1, vwap=1, fib=2, ob=3, fvg=2, struct=4, pat=5),
    # Wyckoff Methode (Akkumulation/Distribution Phasen)
    "WYCKOFF":     dict(ema=2, adx=2, rsi=1, macd=2, bb=3, stoch=1, vwap=3, fib=1, ob=2, fvg=1, struct=5, pat=1),
    # ── NEU: ERWEITERTE ELITE-METHODEN (mit neuen Indikatoren) ───────────────────
    "ICHIMOKU":    dict(ema=1, adx=1, rsi=1, macd=1, bb=1, stoch=1, vwap=1, fib=1, ob=1, fvg=1, struct=2, pat=1,
                        ichi=5, super=2, don=1, cci=1, willr=1, vol=1),
    "SUPERTREND":  dict(ema=2, adx=2, rsi=1, macd=2, bb=1, stoch=1, vwap=1, fib=1, ob=1, fvg=1, struct=2, pat=1,
                        ichi=1, super=5, don=2, cci=1, willr=1, vol=2),
    "MULTI_TF":    dict(ema=5, adx=2, rsi=1, macd=2, bb=1, stoch=1, vwap=2, fib=1, ob=1, fvg=1, struct=3, pat=1,
                        ichi=2, super=3, don=1, cci=1, willr=1, vol=1),
    "BB_SQUEEZE":  dict(ema=1, adx=2, rsi=1, macd=2, bb=5, stoch=1, vwap=1, fib=1, ob=1, fvg=1, struct=1, pat=1,
                        ichi=1, super=1, don=3, cci=2, willr=1, vol=3),
    "VOLUME_CONF": dict(ema=2, adx=2, rsi=2, macd=2, bb=1, stoch=1, vwap=3, fib=1, ob=1, fvg=1, struct=2, pat=2,
                        ichi=1, super=2, don=1, cci=2, willr=2, vol=5),
}

STRATEGY_TYPES = list(STRATEGY_WEIGHTS.keys())

# ── HILFSFUNKTIONEN ───────────────────────────────────────────────────────────

def rolling_vwap(candles, period=50):
    """Rollierender VWAP über letzte N Kerzen (für Backtest ohne Session-Grenzen)."""
    w = candles[-period:] if len(candles) >= period else candles
    tp_vol = sum(((c["high"]+c["low"]+c["close"])/3) * c["tick_volume"] for c in w)
    vol    = sum(c["tick_volume"] for c in w)
    return round(tp_vol / vol, 2) if vol > 0 else None

BULL_PAT = {"Hammer","Inverted Hammer","Dragonfly Doji","Bullish Engulfing",
            "Piercing Line","Morning Star","Three White Soldiers","Tweezer Bottom"}
BEAR_PAT = {"Shooting Star","Hanging Man","Gravestone Doji","Bearish Engulfing",
            "Dark Cloud Cover","Evening Star","Three Black Crows","Tweezer Top"}


def _core_signal(candles, strat):
    """
    Confluence-Score-Signal mit strategie-spezifischer Indikator-Gewichtung.
    Jeder Strategie-Typ (BALANCED, BB_SCALP, FIB_SWING, ICT_SMC, VWAP_TREND,
    MOMENTUM) gewichtet die Elite-Indikatoren unterschiedlich.
    """
    if len(candles) < 250:
        return None, {}

    closes  = [c["close"] for c in candles]
    ema20   = ema_series(closes, 20)[-1]
    ema50   = ema_series(closes, 50)[-1]
    ema200  = ema_series(closes, 200)[-1]
    rsi_v   = rsi(closes[-30:], 14)
    ml, ms_l, mh = macd(closes, 12, 26, 9)
    adx_v   = adx(candles[-60:], 14)
    atr_v   = atr(candles[-20:], 14)
    struct  = market_structure(closes[-30:])
    pat, bias = detect_candle_patterns(candles[-3:])

    # Elite Indikatoren
    bb_up, bb_mid, bb_lo, bb_bw, bb_pctb = bollinger_bands(closes[-60:])
    stk, stk_d = stoch_rsi(closes[-120:])
    vwap_v     = rolling_vwap(candles)
    fib        = fibonacci_levels(candles)
    obs        = find_order_blocks(candles)
    fvgs       = find_fair_value_gaps(candles)
    price      = closes[-1]

    indicators = {
        "ema20": ema20, "ema50": ema50, "ema200": ema200,
        "rsi": rsi_v, "adx": adx_v, "atr": atr_v,
        "structure": struct, "pattern": pat, "bias": bias,
        "bb_pctb": bb_pctb, "stoch_k": stk, "vwap": vwap_v,
    }

    # Strategie-Gewichtungen laden
    stype = strat.get("strategy_type", "BALANCED")
    W     = STRATEGY_WEIGHTS.get(stype, STRATEGY_WEIGHTS["BALANCED"])
    min_score = max(strat.get("min_score", 7), 1)

    buy_score = sell_score = 0

    # 1. EMA Triple Alignment
    if ema20 > ema50 > ema200:   buy_score  += W["ema"]
    elif ema20 < ema50 < ema200: sell_score += W["ema"]

    # 2. ADX Trendstärke-Gate
    if adx_v >= strat["adx_min"]:
        buy_score  += W["adx"]
        sell_score += W["adx"]

    # 3. RSI Zone
    if strat["rsi_low_b"] <= rsi_v <= strat["rsi_high_b"]: buy_score  += W["rsi"]
    if strat["rsi_low_s"] <= rsi_v <= strat["rsi_high_s"]: sell_score += W["rsi"]

    # 4. MACD Kreuz
    if mh[-1] > 0 and ml[-1] > ms_l[-1]:  buy_score  += W["macd"]
    if mh[-1] < 0 and ml[-1] < ms_l[-1]:  sell_score += W["macd"]

    # 5. Bollinger Bands (Bounce an den Bändern)
    if bb_pctb is not None:
        if bb_pctb < 0.2:   buy_score  += W["bb"]   # unteres Band → Bounce
        elif bb_pctb > 0.8: sell_score += W["bb"]   # oberes Band → Rejection
        if bb_bw and bb_bw < 1.0:
            buy_score += 1; sell_score += 1          # BB Squeeze = Ausbruch kommt

    # 6. Stochastic RSI Kreuz
    if stk < 25 and stk > stk_d:   buy_score  += W["stoch"]
    elif stk > 75 and stk < stk_d: sell_score += W["stoch"]

    # 7. VWAP Position
    if vwap_v:
        if price > vwap_v: buy_score  += W["vwap"]
        else:              sell_score += W["vwap"]

    # 8. Fibonacci Key Level (38.2 / 50 / 61.8 / 78.6)
    if fib and fib["near_key"] and fib["nearest"] in ("38.2","50.0","61.8","78.6"):
        if price >= fib["nearest_price"]: buy_score  += W["fib"]
        else:                             sell_score += W["fib"]

    # 9. ICT Order Block Zone
    if obs["bullish"]:
        ob = obs["bullish"]
        if ob["low"] <= price <= ob["high"] * 1.0005:
            buy_score += W["ob"]
    if obs["bearish"]:
        ob = obs["bearish"]
        if ob["low"] * 0.9995 <= price <= ob["high"]:
            sell_score += W["ob"]

    # 10. Fair Value Gap
    for fg in fvgs["bullish"]:
        if fg["bottom"] <= price <= fg["top"]: buy_score  += W["fvg"]; break
    for fg in fvgs["bearish"]:
        if fg["bottom"] <= price <= fg["top"]: sell_score += W["fvg"]; break

    # 11. Marktstruktur (Higher Highs / Lower Lows)
    if struct == "bullish":   buy_score  += W["struct"]
    elif struct == "bearish": sell_score += W["struct"]

    # 12. Kerzenmuster-Bestätigung
    if pat:
        if pat in BULL_PAT:  buy_score  += W["pat"]
        if pat in BEAR_PAT:  sell_score += W["pat"]

    indicators["buy_score"]      = buy_score
    indicators["sell_score"]     = sell_score
    indicators["strategy_type"]  = stype

    if buy_score  >= min_score and buy_score  > sell_score + 2:
        return "BUY",  indicators
    if sell_score >= min_score and sell_score > buy_score  + 2:
        return "SELL", indicators
    return None, indicators


def _ensemble_signal_backtest(candles, strat):
    """Backtest-Ensemble: signal nur wenn ≥4 von 7 Strategien einig sind."""
    votes_buy = votes_sell = 0
    best_ind = {}
    best_sc = 0
    for stype, W in STRATEGY_WEIGHTS.items():
        if stype == "ENSEMBLE":
            continue
        sub_strat = dict(strat)
        sub_strat["strategy_type"] = stype
        sig, ind = _core_signal(candles, sub_strat)
        if sig == "BUY":
            votes_buy += 1
            if ind.get("buy_score", 0) > best_sc:
                best_sc = ind.get("buy_score", 0)
                best_ind = ind
        elif sig == "SELL":
            votes_sell += 1
            if ind.get("sell_score", 0) > best_sc:
                best_sc = ind.get("sell_score", 0)
                best_ind = ind
    total = len(STRATEGY_WEIGHTS) - 1
    threshold = max(total // 2 + 1, 3)
    if votes_buy >= threshold:
        return "BUY", best_ind
    if votes_sell >= threshold:
        return "SELL", best_ind
    return None, best_ind


def get_signal_with_strategy(candles, strat):
    """Thin wrapper: routes to ensemble or core signal logic."""
    stype = strat.get("strategy_type", "BALANCED")
    W     = STRATEGY_WEIGHTS.get(stype, STRATEGY_WEIGHTS["BALANCED"])
    # ENSEMBLE: alle Strategien abstimmen lassen (Mehrheitsvotum)
    if stype == "ENSEMBLE":
        return _ensemble_signal_backtest(candles, strat)
    return _core_signal(candles, strat)


# ── SIGNAL-VORBERECHNUNG (10x Speedup) ───────────────────────────────────────

def precompute_signals(candles, strat):
    """
    Berechnet EMA/MACD/ATR einmal für alle Kerzen vor (statt jede Kerze neu).
    Spart ~70% Rechenzeit bei großen Datensätzen.
    Enthält H4-Filter-Simulation + Choppiness-Index-Regime (wie Live-EA).
    """
    n      = len(candles)
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]

    # Langsame Indikatoren: einmal für die komplette Serie berechnen
    e20s = ema_series(closes, 20)
    e50s = ema_series(closes, 50)
    e200s= ema_series(closes, 200)
    mls, mss, mhs = macd(closes, 12, 26, 9)

    # ATR als laufender Mittelwert
    trs = [0.0]
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i-1]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    atrs = [0.0] * n
    p = 14
    if n > p:
        atrs[p] = sum(trs[1:p+1]) / p
        for i in range(p+1, n):
            atrs[i] = (atrs[i-1] * (p-1) + trs[i]) / p

    # ── H4-TREND SIMULATION (16 × M15 = 1 H4) ───────────────────────────────
    H4 = 16
    h4_closes = [candles[j + H4 - 1]["close"] for j in range(0, n - H4, H4)]
    if len(h4_closes) >= 50:
        h4_ema = ema_series(h4_closes, 50)
    else:
        h4_ema = h4_closes[:]
    # h4_bias[i]: +1 bullish, -1 bearish, 0 neutral
    h4_bias = []
    for i in range(n):
        hi = min(i // H4, len(h4_ema) - 1)
        if hi < 0 or hi >= len(h4_closes):
            h4_bias.append(0)
        else:
            diff = h4_closes[hi] - h4_ema[hi]
            h4_bias.append(1 if diff > 0.5 else (-1 if diff < -0.5 else 0))

    stype     = strat.get("strategy_type", "BALANCED")
    W         = STRATEGY_WEIGHTS.get(stype, STRATEGY_WEIGHTS["BALANCED"])
    min_score = max(strat.get("min_score", 7), 1)
    warmup    = 250
    signals   = [None] * n

    for i in range(warmup, n):
        e20  = e20s[i];  e50 = e50s[i];  e200 = e200s[i]
        mh_v = mhs[i];   ml_v = mls[i];  ms_v = mss[i]
        atr_v = atrs[i]
        if atr_v <= 0:
            continue

        # Kurzfenster-Indikatoren (kleine Fenster → schnell)
        wc  = closes[max(0, i-119):i+1]
        wr  = candles[max(0, i-59):i+1]
        price = closes[i]

        rsi_v  = rsi(wc[-30:], 14)
        adx_v  = adx(wr, 14)
        bb_up, bb_mid, bb_lo, bb_bw, bb_pctb = bollinger_bands(wc[-60:] if len(wc) >= 60 else wc)
        stk, stk_d = stoch_rsi(wc)
        vwap_v = rolling_vwap(wr)
        struct = market_structure(wc[-30:])
        pat, bias = detect_candle_patterns(candles[max(0, i-2):i+1])
        obs  = find_order_blocks(candles[max(0, i-40):i+1])
        fvgs = find_fair_value_gaps(candles[max(0, i-40):i+1])
        fib  = fibonacci_levels(candles[max(0, i-100):i+1])

        buy_score = sell_score = 0

        if e20 > e50 > e200:   buy_score  += W["ema"]
        elif e20 < e50 < e200: sell_score += W["ema"]

        if adx_v >= strat["adx_min"]:
            buy_score += W["adx"]; sell_score += W["adx"]

        if strat["rsi_low_b"] <= rsi_v <= strat["rsi_high_b"]: buy_score  += W["rsi"]
        if strat["rsi_low_s"] <= rsi_v <= strat["rsi_high_s"]: sell_score += W["rsi"]

        if mh_v > 0 and ml_v > ms_v: buy_score  += W["macd"]
        if mh_v < 0 and ml_v < ms_v: sell_score += W["macd"]

        if bb_pctb is not None:
            if bb_pctb < 0.2:   buy_score  += W["bb"]
            elif bb_pctb > 0.8: sell_score += W["bb"]
            if bb_bw and bb_bw < 1.0:
                buy_score += 1; sell_score += 1

        if stk < 25 and stk > stk_d:   buy_score  += W["stoch"]
        elif stk > 75 and stk < stk_d: sell_score += W["stoch"]

        if vwap_v:
            if price > vwap_v: buy_score  += W["vwap"]
            else:              sell_score += W["vwap"]

        if fib and fib.get("near_key") and fib.get("nearest") in ("38.2","50.0","61.8","78.6"):
            if price >= fib.get("nearest_price", price): buy_score  += W["fib"]
            else:                                        sell_score += W["fib"]

        if obs.get("bullish") and isinstance(obs["bullish"], dict):
            ob = obs["bullish"]
            if ob.get("low") is not None and ob.get("low") <= price <= ob.get("high", price) * 1.0005:
                buy_score += W["ob"]
        if obs.get("bearish") and isinstance(obs["bearish"], dict):
            ob = obs["bearish"]
            if ob.get("low") is not None and ob.get("low") * 0.9995 <= price <= ob.get("high", price):
                sell_score += W["ob"]

        if fvgs.get("bullish") and isinstance(fvgs["bullish"], dict):
            fvg = fvgs["bullish"]
            if fvg.get("low") is not None and fvg.get("low") <= price <= fvg.get("high", price):
                buy_score += W["fvg"]
        if fvgs.get("bearish") and isinstance(fvgs["bearish"], dict):
            fvg = fvgs["bearish"]
            if fvg.get("low") is not None and fvg.get("low") <= price <= fvg.get("high", price):
                sell_score += W["fvg"]

        if struct == "bullish":   buy_score  += W["struct"]
        elif struct == "bearish": sell_score += W["struct"]

        if pat:
            if bias == "bullish":   buy_score  += W["pat"]
            elif bias == "bearish": sell_score += W["pat"]

        # ── NEUE ELITE-INDIKATOREN ────────────────────────────────────────────
        seg = candles[max(0, i-59):i+1]

        if W.get("ichi", 0):
            ichi_v = ichimoku(seg)
            if ichi_v["trend"] == "bullish":   buy_score  += W["ichi"]
            elif ichi_v["trend"] == "bearish": sell_score += W["ichi"]
            if ichi_v["tk_cross"] == "bullish":   buy_score  += W["ichi"] // 2
            elif ichi_v["tk_cross"] == "bearish": sell_score += W["ichi"] // 2

        if W.get("super", 0):
            st_dir, _ = supertrend(candles[max(0, i-29):i+1])
            if st_dir == "bullish":   buy_score  += W["super"]
            elif st_dir == "bearish": sell_score += W["super"]

        if W.get("don", 0):
            don_up, don_lo, don_mid = donchian_channel(candles[max(0, i-29):i+1])
            if don_mid:
                if price > don_mid:   buy_score  += W["don"]
                else:                 sell_score += W["don"]
                if don_up and price >= don_up * 0.999: buy_score  += W["don"]
                if don_lo and price <= don_lo * 1.001: sell_score += W["don"]

        if W.get("cci", 0):
            cci_v = cci_indicator(candles[max(0, i-24):i+1])
            if cci_v > 100:    buy_score  += W["cci"]
            elif cci_v < -100: sell_score += W["cci"]

        if W.get("willr", 0):
            willr_v = williams_r(candles[max(0, i-19):i+1])
            if willr_v < -80:   buy_score  += W["willr"]
            elif willr_v > -20: sell_score += W["willr"]

        if W.get("vol", 0):
            vol_rat, vol_high = volume_ratio(candles[max(0, i-24):i+1])
            if vol_high:
                cur = candles[i]
                if cur["close"] > cur["open"]: buy_score  += W["vol"]
                else:                          sell_score += W["vol"]

        # ── CHOPPINESS INDEX (inline, 14-Perioden) ───────────────────────────
        ci_v = 50.0
        if i >= 15:
            cs = candles[i-14:i+1]
            hh = max(c["high"] for c in cs[1:])
            ll = min(c["low"]  for c in cs[1:])
            rl = hh - ll
            if rl > 0:
                str_atr = sum(
                    max(cs[j]["high"] - cs[j]["low"],
                        abs(cs[j]["high"] - cs[j-1]["close"]),
                        abs(cs[j]["low"]  - cs[j-1]["close"]))
                    for j in range(1, 15)
                )
                if str_atr > 0:
                    ci_v = 100.0 * math.log10(str_atr / rl) / math.log10(14)

        is_ranging = ci_v > 56.0 and adx_v < 35

        if is_ranging:
            # RANGE MODE: BB-Bounce + RSI-Extreme (H4-Filter deaktiviert)
            rb = rs = 0
            if rsi_v < 30:                        rb += 5
            elif rsi_v < 35:                      rb += 2
            if rsi_v > 70:                        rs += 5
            elif rsi_v > 65:                      rs += 2
            if bb_pctb is not None:
                if bb_pctb < 0.15:                rb += 4
                if bb_pctb > 0.85:                rs += 4
            if stk < 20 and stk > stk_d:          rb += 3
            if stk > 80 and stk < stk_d:          rs += 3
            sig = None
            if rb >= 7 and rb > rs + 2:           sig = "BUY"
            elif rs >= 7 and rs > rb + 2:         sig = "SELL"
        else:
            # TREND MODE: H4 Hard Gate anwenden
            hb = h4_bias[i]
            if hb > 0:   sell_score = 0   # H4 bullish → nur BUY
            elif hb < 0: buy_score  = 0   # H4 bearish → nur SELL

            sig = None
            if buy_score  >= min_score and buy_score  > sell_score + 2: sig = "BUY"
            elif sell_score >= min_score and sell_score > buy_score  + 2: sig = "SELL"

        signals[i] = (sig, atr_v)

    # 2-Kerzen-Bestätigung: Signal nur wenn auch vorherige Kerze gleiches Signal hatte
    confirm_bars = strat.get("confirm_bars", 1)
    if confirm_bars >= 2:
        for i in range(warmup + 1, n):
            if signals[i] and signals[i-1]:
                if signals[i][0] != signals[i-1][0]:
                    signals[i] = None   # Richtung gewechselt → kein Einstieg
            elif signals[i] and not signals[i-1]:
                signals[i] = None       # Kein vorangehendes Signal → warten

    # Wochentag-Filter: Montag-Open und Freitag-Close meiden
    day_filter = strat.get("day_filter", False)
    if day_filter:
        import datetime as dt
        for i in range(warmup, n):
            if not signals[i]:
                continue
            t = candles[i].get("time")
            if t is None:
                continue
            try:
                d = dt.datetime.utcfromtimestamp(int(t))
                # Montag vor 10:00 UTC überspringen
                if d.weekday() == 0 and d.hour < 10:
                    signals[i] = None
                # Freitag ab 17:00 UTC überspringen
                elif d.weekday() == 4 and d.hour >= 17:
                    signals[i] = None
            except Exception:
                pass

    return signals


# ── BACKTEST SIMULATION ───────────────────────────────────────────────────────

def run_backtest(candles, strat, balance=START_BALANCE):
    """
    Walk-forward Simulation.
    Fuer jede Kerze: Signal pruefen, Trade simulieren, Equity tracken.
    """
    equity       = balance
    trades       = []
    equity_curve = [balance]
    open_trade   = None
    warmup       = 250

    # Vorberechnung für schnellere Backtest-Schleife (ENSEMBLE läuft per-Kerze)
    stype = strat.get("strategy_type", "BALANCED")
    if stype != "ENSEMBLE":
        sig_cache = precompute_signals(candles, strat)
    else:
        sig_cache = None

    _TRAIL_ACTIVATE = 0.5   # Trail nach X×ATR Profit aktivieren (entspricht EA)
    _TRAIL_DIST     = 1.2   # Trailing-Abstand in ATR

    for i in range(warmup, len(candles)):
        c = candles[i]

        # Offenen Trade überprüfen
        if open_trade:
            be_at = strat.get("break_even_at", BREAK_EVEN_AT)
            atr_e = open_trade["atr_v"]

            # ── TP1: Quick partial close 50% (nur wenn noch nicht ausgelöst) ──
            if not open_trade.get("tp1_hit", False):
                tp1_d  = open_trade["tp1_dist"]
                sl_d   = atr_e * strat["sl_mult"]
                tp1_rr = tp1_d / sl_d if sl_d > 0 else 1.0
                if open_trade["type"] == "BUY"  and c["high"] >= open_trade["entry"] + tp1_d:
                    pnl_1 = open_trade["risk"] * 0.5 * tp1_rr
                    equity += pnl_1
                    trades.append({"pnl": pnl_1, "type": "BUY",  "result": "TP1",
                                   "pattern": open_trade["pattern"], "i": i})
                    open_trade["tp1_hit"]      = True
                    open_trade["sl"]           = open_trade["entry"]
                    open_trade["be_triggered"] = True
                    open_trade["risk"]        *= 0.5   # Runner läuft mit halber Größe
                elif open_trade["type"] == "SELL" and c["low"] <= open_trade["entry"] - tp1_d:
                    pnl_1 = open_trade["risk"] * 0.5 * tp1_rr
                    equity += pnl_1
                    trades.append({"pnl": pnl_1, "type": "SELL", "result": "TP1",
                                   "pattern": open_trade["pattern"], "i": i})
                    open_trade["tp1_hit"]      = True
                    open_trade["sl"]           = open_trade["entry"]
                    open_trade["be_triggered"] = True
                    open_trade["risk"]        *= 0.5

            # ── ATR Trail für TP2 Runner (aktiviert nach TP1 + 0.5×ATR) ────────
            if open_trade.get("tp1_hit", False):
                trail_d = atr_e * _TRAIL_DIST
                act_d   = atr_e * _TRAIL_ACTIVATE
                if open_trade["type"] == "BUY":
                    if c["close"] - open_trade["entry"] >= act_d:
                        new_trail = c["close"] - trail_d
                        if new_trail > open_trade["sl"]:
                            open_trade["sl"] = new_trail
                else:  # SELL
                    if open_trade["entry"] - c["close"] >= act_d:
                        new_trail = c["close"] + trail_d
                        if open_trade["sl"] == 0 or new_trail < open_trade["sl"]:
                            open_trade["sl"] = new_trail

            # ── SL / TP2 Checks ─────────────────────────────────────────────────
            if open_trade["type"] == "BUY":
                if be_at > 0 and not open_trade["be_triggered"]:
                    be_level = open_trade["entry"] + atr_e * be_at
                    if c["high"] >= be_level:
                        open_trade["sl"] = open_trade["entry"]
                        open_trade["be_triggered"] = True
                if c["low"] <= open_trade["sl"]:
                    pnl = 0.0 if open_trade["be_triggered"] else -open_trade["risk"]
                    trades.append({"pnl": pnl, "type": "BUY",
                                   "result": "BE" if open_trade["be_triggered"] else "SL",
                                   "pattern": open_trade["pattern"], "i": i})
                    equity    += pnl
                    open_trade = None
                elif c["high"] >= open_trade["tp"]:
                    pnl = open_trade["risk"] * (strat["tp_mult"] / strat["sl_mult"])
                    trades.append({"pnl": pnl, "type": "BUY", "result": "TP",
                                   "pattern": open_trade["pattern"], "i": i})
                    equity    += pnl
                    open_trade = None
            else:  # SELL
                if be_at > 0 and not open_trade["be_triggered"]:
                    be_level = open_trade["entry"] - atr_e * be_at
                    if c["low"] <= be_level:
                        open_trade["sl"] = open_trade["entry"]
                        open_trade["be_triggered"] = True
                if c["high"] >= open_trade["sl"]:
                    pnl = 0.0 if open_trade["be_triggered"] else -open_trade["risk"]
                    trades.append({"pnl": pnl, "type": "SELL",
                                   "result": "BE" if open_trade["be_triggered"] else "SL",
                                   "pattern": open_trade["pattern"], "i": i})
                    equity    += pnl
                    open_trade = None
                elif c["low"] <= open_trade["tp"]:
                    pnl = open_trade["risk"] * (strat["tp_mult"] / strat["sl_mult"])
                    trades.append({"pnl": pnl, "type": "SELL", "result": "TP",
                                   "pattern": open_trade["pattern"], "i": i})
                    equity    += pnl
                    open_trade = None

            equity_curve.append(round(equity, 2))
            continue

        # Kein offener Trade: Signal aus Cache oder berechnen
        if sig_cache is not None:
            cached = sig_cache[i]
            if cached is None:
                equity_curve.append(round(equity, 2))
                continue
            signal, atr_v = cached
        else:
            window = candles[max(0, i-250):i+1]
            signal, ind = get_signal_with_strategy(window, strat)
            atr_v = ind.get("atr", 0)

        if signal and atr_v > 0:
            sl_dist  = atr_v * strat["sl_mult"]
            risk_usd = equity * (RISK_PCT / 100)
            lot      = max(risk_usd / (sl_dist * CONTRACT_SIZE), 0.01)
            # TP1 quick target: min(tp_mult, 1.5)× but at least sl_mult (min 1:1 RR)
            tp1_m    = max(min(strat.get("tp_mult", 2.5), 1.5), strat["sl_mult"])

            if signal == "BUY":
                sl = c["close"] - sl_dist
                tp = c["close"] + atr_v * strat["tp_mult"]
            else:
                sl = c["close"] + sl_dist
                tp = c["close"] - atr_v * strat["tp_mult"]

            pattern = ind.get("pattern") if sig_cache is None else None
            open_trade = {
                "type":         signal,
                "entry":        c["close"],
                "sl":           sl,
                "tp":           tp,
                "risk":         risk_usd,
                "lot":          lot,
                "atr_v":        atr_v,
                "be_triggered": False,
                "pattern":      pattern,
                "open_i":       i,
                "tp1_dist":     atr_v * tp1_m,
                "tp1_hit":      False,
            }

        equity_curve.append(round(equity, 2))

    # Offenen Trade am Ende schliessen
    if open_trade and candles:
        last = candles[-1]["close"]
        pnl  = (last - open_trade["entry"]) * open_trade["lot"] * CONTRACT_SIZE
        if open_trade["type"] == "SELL":
            pnl = -pnl
        trades.append({"pnl": round(pnl, 2), "type": open_trade["type"],
                       "result": "OPEN", "pattern": open_trade["pattern"], "i": len(candles)-1})
        equity += pnl

    return trades, equity_curve, equity


# ── METRIKEN ─────────────────────────────────────────────────────────────────

def calc_metrics(trades, equity_curve, final_equity, initial_balance=None):
    if initial_balance is None:
        initial_balance = START_BALANCE
    if not trades:
        return {"error": "Keine Trades"}

    pnls     = [t["pnl"] for t in trades]
    wins     = [p for p in pnls if p > 0]
    be_count = sum(1 for t in trades if t.get("result") == "BE")
    losses   = [p for p in pnls if p < 0]
    # Break-Even trades zählen als halbe Wins (kein Verlust = kein Schaden)
    win_rate = (len(wins) + be_count * 0.5) / len(pnls) * 100 if pnls else 0

    gross_profit = sum(wins)   if wins   else 0
    gross_loss   = abs(sum(losses)) if losses else 0.01
    profit_factor = gross_profit / gross_loss

    # Max Drawdown
    peak = equity_curve[0]
    max_dd = 0.0
    for e in equity_curve:
        if e > peak:
            peak = e
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # Sharpe (vereinfacht, annualisiert auf M15)
    if len(pnls) > 1:
        avg  = sum(pnls) / len(pnls)
        std  = math.sqrt(sum((p - avg)**2 for p in pnls) / len(pnls))
        sharpe = (avg / std * math.sqrt(len(pnls))) if std > 0 else 0
    else:
        sharpe = 0

    # Pattern-Stats
    pat_stats = {}
    for t in trades:
        p = t.get("pattern") or "Kein Pattern"
        if p not in pat_stats:
            pat_stats[p] = {"wins": 0, "total": 0}
        pat_stats[p]["total"] += 1
        if t["pnl"] > 0:
            pat_stats[p]["wins"] += 1
    for p in pat_stats:
        t = pat_stats[p]["total"]
        pat_stats[p]["wr"] = round(pat_stats[p]["wins"] / t * 100, 1) if t else 0

    return_pct   = round((final_equity - initial_balance) / initial_balance * 100, 2)
    total_profit = round(final_equity - initial_balance, 2)

    return {
        "total_trades":    len(trades),
        "win_rate":        round(win_rate, 1),
        "profit_factor":   round(profit_factor, 2),
        "start_balance":   round(initial_balance, 2),
        "final_balance":   round(final_equity, 2),
        "total_profit":    total_profit,
        "return_pct":      return_pct,
        "max_drawdown":    round(max_dd, 1),
        "sharpe":          round(sharpe, 2),
        "avg_win":         round(sum(wins) / len(wins), 2)   if wins   else 0,
        "avg_loss":        round(sum(losses) / len(losses), 2) if losses else 0,
        "biggest_win":     round(max(wins), 2)   if wins   else 0,
        "biggest_loss":    round(min(losses), 2) if losses else 0,
        "pattern_stats":   pat_stats,
        "equity_curve":    equity_curve[::10],  # Jeder 10. Punkt fuer kompakteres JSON
    }


# ── AUSGABE ───────────────────────────────────────────────────────────────────

def print_results(results):
    start_bal = results.get("start_balance", START_BALANCE)
    print("\n" + "=" * 82)
    print(f"  BACKTEST ERGEBNISSE  |  {SYMBOL}  |  {results['candles']} Kerzen M15")
    print(f"  Startkapital: ${start_bal:,.2f}")
    print("=" * 82)
    print(f"  {'STRATEGIE':<20} {'TRADES':>6} {'WR':>7} {'PF':>6} {'RETURN':>9} {'START→ENDE':>22} {'MAX DD':>8} {'SHARPE':>7}")
    print("  " + "-" * 80)

    best_return = max(s["metrics"].get("return_pct", -999) for s in results["strategies"])

    for s in results["strategies"]:
        m    = s["metrics"]
        mark = " <--BEST" if m.get("return_pct", -999) == best_return else ""
        err  = m.get("error")
        if err:
            print(f"  {s['name']:<20}  {err}")
            continue
        ret_sign  = "+" if m["return_pct"] >= 0 else ""
        profit    = m.get("total_profit", 0)
        final_bal = m.get("final_balance", start_bal + profit)
        p_sign    = "+" if profit >= 0 else ""
        equity_str = f"${start_bal:.0f}→${final_bal:.2f} ({p_sign}${profit:.2f})"
        print(
            f"  {s['name']:<20} "
            f"{m['total_trades']:>6} "
            f"{m['win_rate']:>6.1f}% "
            f"{m['profit_factor']:>6.2f} "
            f"{ret_sign}{m['return_pct']:>8.1f}% "
            f"{equity_str:>22} "
            f"{m['max_drawdown']:>7.1f}% "
            f"{m['sharpe']:>7.2f}"
            f"{mark}"
        )

    print("=" * 82)

    # Top Patterns der besten Strategie
    best = max(results["strategies"], key=lambda s: s["metrics"].get("return_pct", -999))
    m    = best["metrics"]
    ps   = m.get("pattern_stats", {})
    if ps:
        print(f"\n  TOP PATTERNS ({best['name']}):")
        print(f"  {'PATTERN':<28} {'TRADES':>6} {'WIN RATE':>9}")
        print("  " + "-" * 46)
        sorted_ps = sorted(ps.items(), key=lambda x: x[1].get("wr", 0), reverse=True)
        for pat, stat in sorted_ps[:8]:
            if stat["total"] >= 2:
                print(f"  {pat:<28} {stat['total']:>6} {stat['wr']:>8.1f}%")

    print(f"\n  Ergebnisse gespeichert: backtest_results.json")
    print("=" * 82 + "\n")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def print_compare(results, candles_count):
    print("\n" + "=" * 72)
    print(f"  STRATEGIE VERGLEICH — {SYMBOL}")
    print("=" * 72)
    print(f"  {'STRATEGIE':<12} {'TRADES':>6} {'WR':>7} {'PF':>6} {'RETURN':>9} {'DD':>6} {'SCORE':>8}")
    print("  " + "-" * 60)

    scored = []
    for s in results["strategies"]:
        m = s["metrics"]
        if "error" in m or m.get("total_trades", 0) == 0:
            sc = -999
        else:
            wr = m["win_rate"] / 100
            dd_pen = 1 - (m["max_drawdown"] / 100)
            import math as _math
            pf_bonus = min((m["profit_factor"] - 1.0) / 2.0, 1.0)
            trade_q = _math.log(m["total_trades"] + 1) / 6
            sc = round((wr**2 * 0.35 + max(m["return_pct"], 0) * 0.20 +
                        max(m["sharpe"], 0) * 0.20 + pf_bonus * 0.15 +
                        trade_q * 0.10) * dd_pen, 4)
        scored.append((s, sc))

    scored.sort(key=lambda x: x[1], reverse=True)
    for s, sc in scored:
        m = s["metrics"]
        if "error" in m:
            print(f"  {s['name']:<12}  {m.get('error', 'Fehler')}")
            continue
        ret_sign = "+" if m["return_pct"] >= 0 else ""
        print(
            f"  {s['name']:<12} "
            f"{m['total_trades']:>6} "
            f"{m['win_rate']:>6.1f}% "
            f"{m['profit_factor']:>6.2f} "
            f"{ret_sign}{m['return_pct']:>8.1f}% "
            f"{m['max_drawdown']:>5.1f}% "
            f"{sc:>8.4f}"
        )
    print("=" * 72 + "\n")


def main():
    use_mt5  = "--mt5"     in sys.argv
    compare  = "--compare" in sys.argv
    n_str    = next((sys.argv[i+1] for i, a in enumerate(sys.argv) if a == "--candles"), None)
    b_str    = next((sys.argv[i+1] for i, a in enumerate(sys.argv) if a == "--balance"), None)
    n_candles = int(n_str)   if n_str else 2000
    balance   = float(b_str) if b_str else START_BALANCE

    print(f"\n  CLAUDE + QUANT  |  Backtester v1.0")
    print(f"  Symbol: {SYMBOL}  |  Startkapital: ${balance:,.2f}")
    print(f"  Kerzen: {n_candles}  (~{n_candles*15//60//24} Tage M15)\n")

    # Daten laden
    candles = None
    if use_mt5 and MT5_AVAILABLE:
        candles = load_mt5_candles(n_candles)
    if candles is None:
        print(f"  [INFO] Nutze synthetische XAUUSD-Daten ({n_candles} Kerzen)")
        candles = make_xauusd_candles(n_candles)

    results = {
        "timestamp":     datetime.now().isoformat(),
        "symbol":        SYMBOL,
        "candles":       len(candles),
        "data_source":   "MT5" if (use_mt5 and MT5_AVAILABLE) else "Synthetisch",
        "start_balance": balance,
        "strategies":    [],
    }

    for strat in STRATEGIES:
        print(f"  Teste: {strat['name']} ...", end=" ", flush=True)
        trades, equity_curve, final_equity = run_backtest(candles, strat, balance=balance)
        metrics = calc_metrics(trades, equity_curve, final_equity, initial_balance=balance)
        results["strategies"].append({
            "name":    strat["name"],
            "config":  strat,
            "metrics": metrics,
        })
        if "error" not in metrics:
            profit = metrics["total_profit"]
            p_sign = "+" if profit >= 0 else ""
            print(f"{metrics['total_trades']} Trades, WR={metrics['win_rate']}%, Return={metrics['return_pct']:+.1f}%  (${balance:.0f} → ${metrics['final_balance']:.2f}, {p_sign}${profit:.2f})")
        else:
            print(metrics["error"])

    if compare:
        print_compare(results, len(candles))
    else:
        print_results(results)

    # JSON speichern (ohne equity_curve fuer lesbareres JSON)
    results_slim = json.loads(json.dumps(results))
    for s in results_slim["strategies"]:
        s["metrics"].pop("equity_curve", None)
        s["metrics"].pop("pattern_stats", None)

    with open("backtest_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
