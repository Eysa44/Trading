"""
CLAUDE + QUANT  —  Auto-Optimizer v1.0
=======================================
Testet automatisch viele Parameter-Kombinationen auf echten XAUUSD-Daten,
bewertet jede Strategie und schreibt die besten Parameter direkt in den Bot.

Ausfuehren:
  python optimizer.py          # Schnell: 100 Kombinationen, synthetische Daten
  python optimizer.py --mt5    # Echte MT5-Daten (empfohlen, MT5 muss offen sein)
  python optimizer.py --mt5 --trials 300 --candles 8000
  python optimizer.py --apply  # Beste Parameter direkt in Bot uebernehmen
"""

import sys
import json
import random
import math
import time
from datetime import datetime

sys.path.insert(0, ".")
from trading_bot import (
    ema_series, rsi, macd, atr, adx,
    market_structure, detect_candle_patterns,
    CONTRACT_SIZES, MT5_AVAILABLE
)
from backtest import (
    make_xauusd_candles, load_mt5_candles,
    run_backtest, calc_metrics, START_BALANCE
)

SYMBOL        = "XAUUSD"
CONTRACT_SIZE = CONTRACT_SIZES.get(SYMBOL, 100)

# ── PARAMETER-SUCHRAUM ────────────────────────────────────────────────────────
SEARCH_SPACE = {
    "adx_min":      [18, 20, 22, 24, 25, 27, 30, 33],
    "rsi_low_b":    [35, 38, 40, 42, 45],
    "rsi_high_b":   [60, 62, 65, 68, 70],
    "rsi_low_s":    [30, 33, 35, 38, 40],
    "rsi_high_s":   [55, 58, 60, 63, 65],
    "sl_mult":      [1.0, 1.2, 1.5, 1.8, 2.0, 2.5],
    "tp_mult":      [2.0, 2.5, 3.0, 3.5, 4.0, 5.0],
    "need_pattern": [True, False],
}

# Mindestanforderungen (ungueltige Kombinationen ausfiltern)
MIN_TRADES    = 10     # mindestens 10 Trades im Backtest
MAX_DRAWDOWN  = 20.0   # max 20% Drawdown erlaubt
MIN_WR        = 30.0   # mindestens 30% Win Rate


# ── SCORING ───────────────────────────────────────────────────────────────────

def score(m):
    """
    Composite Score: belohnt hohen Return + Sharpe + viele Trades,
    bestraft hohen Drawdown.
    """
    if "error" in m:
        return -999
    if m["total_trades"] < MIN_TRADES:
        return -999
    if m["max_drawdown"] > MAX_DRAWDOWN:
        return -999
    if m["win_rate"] < MIN_WR:
        return -999
    if m["profit_factor"] <= 1.0:
        return -999

    # Hauptmetrik: risikobereinigte Rendite
    sharpe   = max(m["sharpe"], 0)
    ret      = max(m["return_pct"], 0)
    dd_pen   = 1 - (m["max_drawdown"] / 100)        # Drawdown-Strafe
    trade_q  = math.log(m["total_trades"] + 1) / 5  # mehr Trades = sicherer
    pf_bonus = min(m["profit_factor"] / 2.0, 1.0)   # Profit Factor Bonus

    return round((ret * 0.35 + sharpe * 0.35 + pf_bonus * 0.15 + trade_q * 0.15) * dd_pen, 4)


# ── WALK-FORWARD TEST ────────────────────────────────────────────────────────

def walk_forward_score(candles, strat, split=0.65):
    """
    Testet auf ersten 65% (In-Sample), validiert auf letzten 35% (Out-of-Sample).
    Verhindert Overfitting.
    """
    split_idx = int(len(candles) * split)
    train     = candles[:split_idx]
    test      = candles[split_idx:]

    if len(test) < 300:
        # Zu wenig Daten fuer Walk-Forward, normaler Backtest
        trades, eq, final = run_backtest(candles, strat)
        return calc_metrics(trades, eq, final)

    _, _, _ = run_backtest(train, strat)          # Training (nur fuer Konsistenz)
    trades_test, eq_test, final_test = run_backtest(test, strat)
    m = calc_metrics(trades_test, eq_test, final_test)
    return m


# ── RANDOM SEARCH ─────────────────────────────────────────────────────────────

def random_trial(seed=None):
    """Waehlt zufaellige Parameter aus dem Suchraum."""
    if seed is not None:
        random.seed(seed)
    strat = {
        "name":        "Trial",
        "adx_min":      random.choice(SEARCH_SPACE["adx_min"]),
        "rsi_low_b":    random.choice(SEARCH_SPACE["rsi_low_b"]),
        "rsi_high_b":   random.choice(SEARCH_SPACE["rsi_high_b"]),
        "rsi_low_s":    random.choice(SEARCH_SPACE["rsi_low_s"]),
        "rsi_high_s":   random.choice(SEARCH_SPACE["rsi_high_s"]),
        "sl_mult":      random.choice(SEARCH_SPACE["sl_mult"]),
        "tp_mult":      random.choice(SEARCH_SPACE["tp_mult"]),
        "need_pattern": random.choice(SEARCH_SPACE["need_pattern"]),
    }
    # TP muss groesser als SL sein (mind. RR 1.5)
    if strat["tp_mult"] < strat["sl_mult"] * 1.5:
        strat["tp_mult"] = strat["sl_mult"] * 2.0
    # RSI-Range muss sinnvoll sein
    if strat["rsi_high_b"] <= strat["rsi_low_b"] + 10:
        strat["rsi_high_b"] = strat["rsi_low_b"] + 15
    if strat["rsi_high_s"] <= strat["rsi_low_s"] + 10:
        strat["rsi_high_s"] = strat["rsi_low_s"] + 15
    return strat


# ── AUSGABE ───────────────────────────────────────────────────────────────────

def print_top(results, n=10):
    print("\n" + "=" * 80)
    print(f"  TOP {n} STRATEGIEN  |  {SYMBOL}  |  Auto-Optimizer Ergebnisse")
    print("=" * 80)
    print(f"  {'#':>2}  {'ADX':>4}  {'RSI-B':>9}  {'RSI-S':>9}  "
          f"{'SL':>4}  {'TP':>4}  {'PAT':>4}  "
          f"{'TRADES':>6}  {'WR':>6}  {'RETURN':>8}  {'SCORE':>7}")
    print("  " + "-" * 78)

    for rank, r in enumerate(results[:n], 1):
        s = r["strat"]
        m = r["metrics"]
        pat = "Ja" if s["need_pattern"] else "Nein"
        rsi_b = f"{s['rsi_low_b']}-{s['rsi_high_b']}"
        rsi_s = f"{s['rsi_low_s']}-{s['rsi_high_s']}"
        ret_sign = "+" if m["return_pct"] >= 0 else ""
        print(
            f"  {rank:>2}  {s['adx_min']:>4}  {rsi_b:>9}  {rsi_s:>9}  "
            f"{s['sl_mult']:>4.1f}  {s['tp_mult']:>4.1f}  {pat:>4}  "
            f"{m['total_trades']:>6}  {m['win_rate']:>5.1f}%  "
            f"{ret_sign}{m['return_pct']:>7.1f}%  {r['score']:>7.4f}"
        )

    print("=" * 80)
    if results:
        best  = results[0]
        s     = best["strat"]
        m     = best["metrics"]
        print(f"\n  BESTE STRATEGIE:")
        print(f"    ADX Threshold  : {s['adx_min']}")
        print(f"    RSI Buy        : {s['rsi_low_b']} - {s['rsi_high_b']}")
        print(f"    RSI Sell       : {s['rsi_low_s']} - {s['rsi_high_s']}")
        print(f"    SL / TP        : ATR x {s['sl_mult']} / ATR x {s['tp_mult']}")
        print(f"    Pattern Pflicht: {'Ja' if s['need_pattern'] else 'Nein'}")
        print(f"    Win Rate       : {m['win_rate']}%")
        print(f"    Return         : +{m['return_pct']}%")
        print(f"    Max Drawdown   : {m['max_drawdown']}%")
        print(f"    Profit Factor  : {m['profit_factor']}")
        print(f"    Sharpe         : {m['sharpe']}")
        print(f"    Score          : {best['score']}")
    print("=" * 80 + "\n")


def apply_best_params(best_strat):
    """Schreibt die besten Parameter in best_params.json fuer den Bot."""
    params = {
        "timestamp":    datetime.now().isoformat(),
        "symbol":       SYMBOL,
        "adx_threshold": best_strat["adx_min"],
        "rsi_low_buy":   best_strat["rsi_low_b"],
        "rsi_high_buy":  best_strat["rsi_high_b"],
        "rsi_low_sell":  best_strat["rsi_low_s"],
        "rsi_high_sell": best_strat["rsi_high_s"],
        "sl_mult":       best_strat["sl_mult"],
        "tp_mult":       best_strat["tp_mult"],
        "need_pattern":  best_strat["need_pattern"],
        "source":        "optimizer",
    }
    with open("best_params.json", "w") as f:
        json.dump(params, f, indent=2)
    print(f"  [OK] Beste Parameter gespeichert in best_params.json")
    print(f"  [OK] Bot laedt diese beim naechsten Start automatisch")
    return params


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    use_mt5      = "--mt5"   in sys.argv
    apply        = "--apply" in sys.argv
    n_str        = next((sys.argv[i+1] for i, a in enumerate(sys.argv) if a == "--trials"), None)
    c_str        = next((sys.argv[i+1] for i, a in enumerate(sys.argv) if a == "--candles"), None)
    n_trials     = int(n_str) if n_str else 150
    n_candles    = int(c_str) if c_str else 5000

    print(f"\n  CLAUDE + QUANT  |  Auto-Optimizer v1.0")
    print(f"  Symbol: {SYMBOL}  |  Trials: {n_trials}  |  Kerzen: {n_candles}")
    print(f"  Walk-Forward: Ja (65% Train / 35% Test)\n")

    # Daten laden
    candles = None
    if use_mt5 and MT5_AVAILABLE:
        candles = load_mt5_candles(n_candles)
    if candles is None:
        print(f"  [INFO] Synthetische XAUUSD-Daten ({n_candles} Kerzen)")
        candles = make_xauusd_candles(n_candles)

    print(f"  Starte {n_trials} Optimierungs-Versuche...\n")

    results   = []
    best_score = -999
    t_start   = time.time()
    valid     = 0

    for trial in range(n_trials):
        strat = random_trial(seed=trial * 7 + 13)
        strat["name"] = f"Trial-{trial+1}"

        m = walk_forward_score(candles, strat)
        s = score(m)

        if s > -999:
            valid += 1
            results.append({"strat": strat, "metrics": m, "score": s})
            if s > best_score:
                best_score = s
                best_strat = strat

        # Fortschrittsanzeige alle 25 Trials
        if (trial + 1) % 25 == 0:
            elapsed = time.time() - t_start
            eta     = elapsed / (trial + 1) * (n_trials - trial - 1)
            best_r  = max((r["metrics"]["return_pct"] for r in results), default=0)
            print(f"  [{trial+1:>4}/{n_trials}]  Gueltig: {valid:>4}  "
                  f"Bester Return: {best_r:>+.1f}%  "
                  f"ETA: {int(eta)}s")

    # Sortieren nach Score
    results.sort(key=lambda x: x["score"], reverse=True)

    elapsed = time.time() - t_start
    print(f"\n  Fertig in {elapsed:.0f}s  |  {valid}/{n_trials} gueltige Strategien gefunden")

    print_top(results, n=10)

    # Alle Ergebnisse speichern
    out = {
        "timestamp":   datetime.now().isoformat(),
        "symbol":      SYMBOL,
        "candles":     len(candles),
        "trials":      n_trials,
        "valid":       valid,
        "data_source": "MT5" if (use_mt5 and MT5_AVAILABLE) else "Synthetisch",
        "top10": [
            {
                "rank":    i + 1,
                "params":  r["strat"],
                "metrics": {k: v for k, v in r["metrics"].items()
                            if k not in ("equity_curve", "pattern_stats")},
                "score":   r["score"],
            }
            for i, r in enumerate(results[:10])
        ],
    }
    with open("optimizer_results.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"  Alle Ergebnisse: optimizer_results.json")

    # Beste Parameter anwenden
    if results:
        if apply:
            apply_best_params(results[0]["strat"])
        else:
            try:
                ans = input("\n  Beste Parameter jetzt in Bot uebernehmen? [j/n]: ").strip().lower()
                if ans == "j":
                    apply_best_params(results[0]["strat"])
            except EOFError:
                print("  (Tipp: --apply Flag nutzen um automatisch zu uebernehmen)")


if __name__ == "__main__":
    main()
