"""
Bot v2.0 - Lokaler Test (kein MT5 noetig)
Testet: Indikatoren, Candlestick-Patterns, Signal-Logik
Ausfuehren: python test_bot.py
"""

import sys
import random

# Alle Funktionen aus dem Bot importieren
sys.path.insert(0, ".")
from trading_bot import (
    ema_series, rsi, macd, atr, adx, market_structure,
    detect_candle_patterns, get_signal, calc_lot
)

random.seed(42)

print("=" * 60)
print("  CLAUDE + QUANT  |  Bot Test v2.0")
print("=" * 60)


# ── 1. SYNTHETISCHE KERZEN GENERIEREN ────────────────────────
def make_candles(n=250, trend="up"):
    candles = []
    price = 1.08500
    for i in range(n):
        if trend == "up":
            drift = 0.00003
        elif trend == "down":
            drift = -0.00003
        else:
            drift = 0.0
        move  = drift + (random.random() - 0.5) * 0.00080
        open_ = price
        close = price + move
        high  = max(open_, close) + random.random() * 0.00020
        low   = min(open_, close) - random.random() * 0.00020
        candles.append({
            "open":  open_,
            "high":  high,
            "low":   low,
            "close": close,
            "tick_volume": random.randint(100, 1000),
        })
        price = close
    return candles


# ── 2. INDIKATOREN TESTEN ─────────────────────────────────────
print("\n[TEST 1] Indikatoren")
candles_up = make_candles(250, trend="up")
closes = [c["close"] for c in candles_up]

ema20  = ema_series(closes, 20)[-1]
ema50  = ema_series(closes, 50)[-1]
ema200 = ema_series(closes, 200)[-1]
rsi_v  = rsi(closes[-30:], 14)
macd_l, macd_s, macd_h = macd(closes, 12, 26, 9)
atr_v  = atr(candles_up[-20:], 14)
adx_v  = adx(candles_up[-60:], 14)
struct = market_structure(closes[-30:])

print(f"  EMA20  = {ema20:.5f}")
print(f"  EMA50  = {ema50:.5f}")
print(f"  EMA200 = {ema200:.5f}")
print(f"  EMA20 > EMA50 > EMA200 = {ema20 > ema50 > ema200}  <- sollte True sein (Auftrend)")
print(f"  RSI    = {rsi_v:.1f}")
print(f"  MACD   = {macd_l[-1]:.6f}  Hist={macd_h[-1]:.6f}")
print(f"  ATR    = {atr_v:.5f}  ({round(atr_v/0.0001)} Pips)")
print(f"  ADX    = {adx_v:.1f}  (>25 = starker Trend)")
print(f"  Struktur = {struct}")


# ── 3. CANDLESTICK PATTERNS TESTEN ───────────────────────────
print("\n[TEST 2] Candlestick Pattern Erkennung")

test_patterns = [
    # (Name, Kerzen [c2, c1, c0])
    ("Hammer", [
        {"open": 1.0850, "close": 1.0840, "high": 1.0855, "low": 1.0835},  # c2
        {"open": 1.0840, "close": 1.0830, "high": 1.0845, "low": 1.0825},  # c1 bearisch
        {"open": 1.0828, "close": 1.0845, "high": 1.0848, "low": 1.0800},  # c0 Hammer (langer unterer Schatten)
    ]),
    ("Bullish Engulfing", [
        {"open": 1.0860, "close": 1.0850, "high": 1.0865, "low": 1.0845},
        {"open": 1.0850, "close": 1.0830, "high": 1.0852, "low": 1.0828},  # c1 bearisch
        {"open": 1.0825, "close": 1.0860, "high": 1.0862, "low": 1.0823},  # c0 bullisch, groesser
    ]),
    ("Shooting Star", [
        {"open": 1.0840, "close": 1.0850, "high": 1.0855, "low": 1.0838},
        {"open": 1.0850, "close": 1.0860, "high": 1.0865, "low": 1.0848},  # c1 bullisch
        {"open": 1.0862, "close": 1.0845, "high": 1.0900, "low": 1.0843},  # c0 Shooting Star
    ]),
    ("Bearish Engulfing", [
        {"open": 1.0840, "close": 1.0850, "high": 1.0855, "low": 1.0838},
        {"open": 1.0850, "close": 1.0870, "high": 1.0872, "low": 1.0848},  # c1 bullisch
        {"open": 1.0875, "close": 1.0840, "high": 1.0877, "low": 1.0838},  # c0 bearisch, groesser
    ]),
    ("Doji", [
        {"open": 1.0850, "close": 1.0840, "high": 1.0855, "low": 1.0835},
        {"open": 1.0840, "close": 1.0850, "high": 1.0855, "low": 1.0838},
        {"open": 1.0850, "close": 1.0850, "high": 1.0870, "low": 1.0830},  # Doji (open=close)
    ]),
    ("Morning Star", [
        {"open": 1.0870, "close": 1.0840, "high": 1.0872, "low": 1.0838},  # c2 bearisch gross
        {"open": 1.0838, "close": 1.0836, "high": 1.0840, "low": 1.0834},  # c1 kleiner Stern
        {"open": 1.0838, "close": 1.0862, "high": 1.0864, "low": 1.0836},  # c0 bullisch > 50%
    ]),
]

for name, kerzen in test_patterns:
    detected, bias = detect_candle_patterns(kerzen)
    ok = "OK" if detected is not None else "?"
    print(f"  [{ok}] Erwartet: {name:25s} -> Erkannt: {detected or 'keins':25s} ({bias})")


# ── 4. VOLLSTAENDIGER SIGNAL-TEST ────────────────────────────
print("\n[TEST 3] Signal-Engine")

# Aufwaerts-Trend: sollte BUY Signal ausloesen
signal_up, ind_up = get_signal(candles_up)
print(f"  Aufwaertstrend-Test:")
print(f"    EMA20>50>200 = {ind_up.get('ema20',0) > ind_up.get('ema50',0) > ind_up.get('ema200',0)}")
print(f"    ADX          = {ind_up.get('adx',0):.1f}  (braucht > 25)")
print(f"    RSI          = {ind_up.get('rsi',0):.1f}  (braucht 40-65)")
print(f"    Struktur     = {ind_up.get('structure','?')}")
print(f"    Kerze        = {ind_up.get('candle_pattern','keins')} ({ind_up.get('candle_bias','?')})")
print(f"    Signal       = {signal_up or 'WARTE (Kerzen-Confirmation fehlt mit zufaelligen Daten)'}")

# Abwaerts-Trend
candles_down = make_candles(250, trend="down")
signal_down, ind_down = get_signal(candles_down)
print(f"  Abwaertstrend-Test:")
print(f"    EMA20<50<200 = {ind_down.get('ema20',0) < ind_down.get('ema50',0) < ind_down.get('ema200',0)}")
print(f"    ADX          = {ind_down.get('adx',0):.1f}")
print(f"    RSI          = {ind_down.get('rsi',0):.1f}")
print(f"    Struktur     = {ind_down.get('structure','?')}")
print(f"    Signal       = {signal_down or 'WARTE'}")


# ── 5. POSITION SIZING TEST ───────────────────────────────────
print("\n[TEST 4] Position Sizing (1% Risiko)")
for balance in [1000, 5000, 10000]:
    # ATR ca. 15 Pips = 0.0015 fuer EURUSD
    sl_dist = 0.0015
    lot = calc_lot(balance, sl_dist, "EURUSD")
    risk_usd = sl_dist * lot * 100000
    print(f"  Konto ${balance:>7}  ->  {lot:.2f} Lots  Risiko=${risk_usd:.2f}  (1% = ${balance*0.01:.2f})")


print("\n" + "=" * 60)
print("  Alle Tests abgeschlossen.")
print("  Naecsher Schritt: START.bat starten wenn MT5 offen ist")
print("=" * 60)
