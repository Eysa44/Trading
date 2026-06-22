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
    ATR_SL_MULT, ATR_TP_MULT, CONTRACT_SIZES, MT5_AVAILABLE
)

# ── KONFIGURATION ─────────────────────────────────────────────────────────────
SYMBOL         = "XAUUSD"
START_BALANCE  = 10000.0   # USD
RISK_PCT       = 1.0       # % pro Trade
CONTRACT_SIZE  = CONTRACT_SIZES.get(SYMBOL, 100)

# Strategien die verglichen werden (Classic + Elite)
STRATEGIES = [
    # ── KLASSISCH ──────────────────────────────────────────────────────────────
    {
        "name":        "Classic Conservative",
        "adx_min":     30,
        "rsi_low_b":   45,  "rsi_high_b": 60,
        "rsi_low_s":   40,  "rsi_high_s": 55,
        "need_pattern": True,
        "sl_mult":     1.5, "tp_mult": 3.0,
        "min_score":   0,   # 0 = alte Logik (alle Bedingungen AND)
    },
    {
        "name":        "Classic Balanced",
        "adx_min":     25,
        "rsi_low_b":   40,  "rsi_high_b": 65,
        "rsi_low_s":   35,  "rsi_high_s": 60,
        "need_pattern": True,
        "sl_mult":     1.5, "tp_mult": 3.0,
        "min_score":   0,
    },
    # ── ELITE CONFLUENCE ───────────────────────────────────────────────────────
    {
        "name":        "Elite Balanced (8 Punkte)",
        "adx_min":     25,
        "rsi_low_b":   40,  "rsi_high_b": 65,
        "rsi_low_s":   35,  "rsi_high_s": 60,
        "need_pattern": True,
        "sl_mult":     1.5, "tp_mult": 3.0,
        "min_score":   8,   # Confluence: mind. 8 Punkte
    },
    {
        "name":        "Elite Conservative (10 Punkte)",
        "adx_min":     28,
        "rsi_low_b":   42,  "rsi_high_b": 62,
        "rsi_low_s":   38,  "rsi_high_s": 58,
        "need_pattern": True,
        "sl_mult":     1.5, "tp_mult": 3.5,
        "min_score":   10,
    },
    {
        "name":        "BB Scalp",
        "adx_min":     22,
        "rsi_low_b":   35,  "rsi_high_b": 65,
        "rsi_low_s":   35,  "rsi_high_s": 65,
        "need_pattern": False,
        "sl_mult":     1.0, "tp_mult": 2.0,
        "min_score":   6,   # Mehr Trades, niedrigerer Score
    },
    {
        "name":        "Fibonacci Swing",
        "adx_min":     20,
        "rsi_low_b":   38,  "rsi_high_b": 68,
        "rsi_low_s":   32,  "rsi_high_s": 62,
        "need_pattern": False,
        "sl_mult":     2.0, "tp_mult": 5.0,
        "min_score":   7,
    },
    {
        "name":        "ICT Smart Money",
        "adx_min":     25,
        "rsi_low_b":   40,  "rsi_high_b": 65,
        "rsi_low_s":   35,  "rsi_high_s": 60,
        "need_pattern": False,
        "sl_mult":     1.5, "tp_mult": 3.0,
        "min_score":   9,   # Strenger Score, nutzt OB+FVG+Fib
    },
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


# ── SIGNAL-LOGIK (Strategie-Parameter injizierbar) ────────────────────────────

BULL_PAT = {"Hammer","Inverted Hammer","Dragonfly Doji","Bullish Engulfing",
            "Piercing Line","Morning Star","Three White Soldiers","Tweezer Bottom"}
BEAR_PAT = {"Shooting Star","Hanging Man","Gravestone Doji","Bearish Engulfing",
            "Dark Cloud Cover","Evening Star","Three Black Crows","Tweezer Top"}


def get_signal_with_strategy(candles, strat):
    """
    Signal basierend auf Strategie-Parametern.
    min_score=0 → alte AND-Logik (alle Bedingungen erforderlich).
    min_score>0 → Confluence-Score-System mit Elite-Indikatoren.
    """
    if len(candles) < 250:
        return None, {}

    closes = [c["close"] for c in candles]
    ema20  = ema_series(closes, 20)[-1]
    ema50  = ema_series(closes, 50)[-1]
    ema200 = ema_series(closes, 200)[-1]
    rsi_v  = rsi(closes[-30:], 14)
    ml, ms_l, mh = macd(closes, 12, 26, 9)
    adx_v  = adx(candles[-60:], 14)
    atr_v  = atr(candles[-20:], 14)
    struct = market_structure(closes[-30:])
    pat, bias = detect_candle_patterns(candles[-3:])

    indicators = {
        "ema20": ema20, "ema50": ema50, "ema200": ema200,
        "rsi": rsi_v, "adx": adx_v, "atr": atr_v,
        "structure": struct, "pattern": pat, "bias": bias,
    }

    min_score = strat.get("min_score", 0)

    # ── ALTE LOGIK (min_score=0): alle Bedingungen AND ─────────────────────────
    if min_score == 0:
        if adx_v < strat["adx_min"]:
            return None, indicators
        if strat["need_pattern"] and pat is None:
            return None, indicators
        if (ema20 > ema50 > ema200
                and strat["rsi_low_b"] <= rsi_v <= strat["rsi_high_b"]
                and struct == "bullish"
                and (not strat["need_pattern"] or bias == "bullish")):
            return "BUY", indicators
        if (ema20 < ema50 < ema200
                and strat["rsi_low_s"] <= rsi_v <= strat["rsi_high_s"]
                and struct == "bearish"
                and (not strat["need_pattern"] or bias == "bearish")):
            return "SELL", indicators
        return None, indicators

    # ── ELITE CONFLUENCE-LOGIK (min_score>0) ──────────────────────────────────
    bb_up, bb_mid, bb_lo, bb_bw, bb_pctb = bollinger_bands(closes[-60:])
    stk, stk_d = stoch_rsi(closes[-120:])
    fib        = fibonacci_levels(candles)
    obs        = find_order_blocks(candles)
    fvgs       = find_fair_value_gaps(candles)
    price      = closes[-1]

    buy_score = sell_score = 0

    # 1. EMA Triple Alignment (2pts)
    if ema20 > ema50 > ema200:   buy_score  += 2
    elif ema20 < ema50 < ema200: sell_score += 2

    # 2. ADX Gate (1pt each when trend strong)
    if adx_v >= strat["adx_min"]:
        buy_score  += 1
        sell_score += 1

    # 3. RSI Zone (1pt)
    if strat["rsi_low_b"] <= rsi_v <= strat["rsi_high_b"]: buy_score  += 1
    if strat["rsi_low_s"] <= rsi_v <= strat["rsi_high_s"]: sell_score += 1

    # 4. MACD (1pt)
    if mh[-1] > 0 and ml[-1] > ms_l[-1]:  buy_score  += 1
    if mh[-1] < 0 and ml[-1] < ms_l[-1]:  sell_score += 1

    # 5. Bollinger Bands (2pts bounce, 1pt squeeze)
    if bb_pctb is not None:
        if bb_pctb < 0.2:   buy_score  += 2
        elif bb_pctb > 0.8: sell_score += 2
        if bb_bw and bb_bw < 1.0:
            buy_score += 1; sell_score += 1

    # 6. Stochastic RSI (2pts)
    if stk < 25 and stk > stk_d:   buy_score  += 2
    elif stk > 75 and stk < stk_d: sell_score += 2

    # 7. Fibonacci Key Level (2pts)
    if fib and fib["near_key"] and fib["nearest"] in ("38.2","50.0","61.8","78.6"):
        if price >= fib["nearest_price"]: buy_score  += 2
        else:                             sell_score += 2

    # 8. Order Block Zone (2pts)
    if obs["bullish"]:
        ob = obs["bullish"]
        if ob["low"] <= price <= ob["high"] * 1.0005:
            buy_score += 2
    if obs["bearish"]:
        ob = obs["bearish"]
        if ob["low"] * 0.9995 <= price <= ob["high"]:
            sell_score += 2

    # 9. Fair Value Gap (1pt)
    for fg in fvgs["bullish"]:
        if fg["bottom"] <= price <= fg["top"]: buy_score  += 1; break
    for fg in fvgs["bearish"]:
        if fg["bottom"] <= price <= fg["top"]: sell_score += 1; break

    # 10. Market Structure (1pt)
    if struct == "bullish":    buy_score  += 1
    elif struct == "bearish":  sell_score += 1

    # 11. Candlestick Pattern (1pt)
    if pat:
        if pat in BULL_PAT:  buy_score  += 1
        if pat in BEAR_PAT:  sell_score += 1

    # ── ENTSCHEIDUNG ──────────────────────────────────────────────────────────
    indicators["buy_score"]  = buy_score
    indicators["sell_score"] = sell_score

    if buy_score  >= min_score and buy_score  > sell_score + 2:
        return "BUY",  indicators
    if sell_score >= min_score and sell_score > buy_score  + 2:
        return "SELL", indicators
    return None, indicators


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
    warmup       = 250          # Kerzen zum Aufwaermen der Indikatoren

    for i in range(warmup, len(candles)):
        window = candles[max(0, i-250):i+1]
        c      = candles[i]

        # Offenen Trade ueberpruefen
        if open_trade:
            if open_trade["type"] == "BUY":
                if c["low"] <= open_trade["sl"]:
                    pnl = -open_trade["risk"]
                    trades.append({"pnl": pnl, "type": "BUY", "result": "SL",
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
                if c["high"] >= open_trade["sl"]:
                    pnl = -open_trade["risk"]
                    trades.append({"pnl": pnl, "type": "SELL", "result": "SL",
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

        # Kein offener Trade: Signal pruefen
        signal, ind = get_signal_with_strategy(window, strat)
        if signal and ind.get("atr", 0) > 0:
            atr_v    = ind["atr"]
            sl_dist  = atr_v * strat["sl_mult"]
            risk_usd = equity * (RISK_PCT / 100)
            lot      = max(risk_usd / (sl_dist * CONTRACT_SIZE), 0.01)

            if signal == "BUY":
                sl = c["close"] - sl_dist
                tp = c["close"] + atr_v * strat["tp_mult"]
            else:
                sl = c["close"] + sl_dist
                tp = c["close"] - atr_v * strat["tp_mult"]

            open_trade = {
                "type":    signal,
                "entry":   c["close"],
                "sl":      sl,
                "tp":      tp,
                "risk":    risk_usd,
                "lot":     lot,
                "pattern": ind.get("pattern"),
                "open_i":  i,
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

def calc_metrics(trades, equity_curve, final_equity):
    if not trades:
        return {"error": "Keine Trades"}

    pnls     = [t["pnl"] for t in trades]
    wins     = [p for p in pnls if p > 0]
    losses   = [p for p in pnls if p < 0]
    win_rate = len(wins) / len(pnls) * 100 if pnls else 0

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

    return {
        "total_trades":    len(trades),
        "win_rate":        round(win_rate, 1),
        "profit_factor":   round(profit_factor, 2),
        "total_profit":    round(final_equity - START_BALANCE, 2),
        "return_pct":      round((final_equity - START_BALANCE) / START_BALANCE * 100, 2),
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
    print("\n" + "=" * 72)
    print(f"  BACKTEST ERGEBNISSE  |  {SYMBOL}  |  {results['candles']} Kerzen M15")
    print("=" * 72)
    print(f"  {'STRATEGIE':<30} {'TRADES':>6} {'WR':>7} {'PF':>6} {'RETURN':>9} {'MAX DD':>8} {'SHARPE':>7}")
    print("  " + "-" * 70)

    best_return = max(s["metrics"].get("return_pct", -999) for s in results["strategies"])

    for s in results["strategies"]:
        m    = s["metrics"]
        mark = " <-- BEST" if m.get("return_pct", -999) == best_return else ""
        err  = m.get("error")
        if err:
            print(f"  {s['name']:<30}  {err}")
            continue
        ret_color = "+" if m["return_pct"] >= 0 else ""
        print(
            f"  {s['name']:<30} "
            f"{m['total_trades']:>6} "
            f"{m['win_rate']:>6.1f}% "
            f"{m['profit_factor']:>6.2f} "
            f"{ret_color}{m['return_pct']:>8.1f}% "
            f"{m['max_drawdown']:>7.1f}% "
            f"{m['sharpe']:>7.2f}"
            f"{mark}"
        )

    print("=" * 72)

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
    print("=" * 72 + "\n")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    use_mt5  = "--mt5" in sys.argv
    n_str    = next((sys.argv[sys.argv.index("--candles") + 1]
                     for i, a in enumerate(sys.argv) if a == "--candles"), None)
    n_candles = int(n_str) if n_str else 2000

    print(f"\n  CLAUDE + QUANT  |  Backtester v1.0")
    print(f"  Symbol: {SYMBOL}  |  Startkapital: ${START_BALANCE:,.0f}")
    print(f"  Kerzen: {n_candles}  (~{n_candles*15//60//24} Tage M15)\n")

    # Daten laden
    candles = None
    if use_mt5 and MT5_AVAILABLE:
        candles = load_mt5_candles(n_candles)
    if candles is None:
        print(f"  [INFO] Nutze synthetische XAUUSD-Daten ({n_candles} Kerzen)")
        candles = make_xauusd_candles(n_candles)

    results = {
        "timestamp": datetime.now().isoformat(),
        "symbol":    SYMBOL,
        "candles":   len(candles),
        "data_source": "MT5" if (use_mt5 and MT5_AVAILABLE) else "Synthetisch",
        "start_balance": START_BALANCE,
        "strategies": [],
    }

    for strat in STRATEGIES:
        print(f"  Teste: {strat['name']} ...", end=" ", flush=True)
        trades, equity_curve, final_equity = run_backtest(candles, strat)
        metrics = calc_metrics(trades, equity_curve, final_equity)
        results["strategies"].append({
            "name":    strat["name"],
            "config":  strat,
            "metrics": metrics,
        })
        if "error" not in metrics:
            print(f"{metrics['total_trades']} Trades, WR={metrics['win_rate']}%, Return={metrics['return_pct']:+.1f}%")
        else:
            print(metrics["error"])

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
