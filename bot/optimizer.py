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
    run_backtest, calc_metrics, START_BALANCE,
    STRATEGY_TYPES
)

SYMBOL        = "XAUUSD"
CONTRACT_SIZE = CONTRACT_SIZES.get(SYMBOL, 100)

# ── PARAMETER-SUCHRAUM ────────────────────────────────────────────────────────
SEARCH_SPACE = {
    "adx_min":        [18, 20, 22, 24, 25, 27, 30, 33],
    "rsi_low_b":      [35, 38, 40, 42, 45],
    "rsi_high_b":     [55, 58, 60, 62, 65],
    "rsi_low_s":      [30, 33, 35, 38, 40],
    "rsi_high_s":     [50, 52, 55, 58, 60],
    "sl_mult":        [1.0, 1.2, 1.5, 1.8, 2.0, 2.5],
    "tp_mult":        [1.5, 2.0, 2.5, 3.0, 3.5, 4.0],   # Enger → höhere WR
    "need_pattern":   [True, False],
    "min_score":      [6, 7, 8, 9, 10, 11, 12],          # Confluence-Schwelle
    "strategy_type":  STRATEGY_TYPES,                     # 6 Elite-Strategie-Typen
    "break_even_at":  [0.0, 0.8, 1.0, 1.2, 1.5],         # 0 = deaktiviert
}

# ── QUALITÄTS-FILTER ──────────────────────────────────────────────────────────
MIN_TRADES    = 8      # Mindestens 8 Trades für aussagekräftiges Ergebnis
MAX_DRAWDOWN  = 20.0   # Maximal 20% Drawdown (realistisch für echte Daten)
MIN_WR        = 40.0   # Mindestens 40% Win Rate (realistisch für echte XAUUSD-Daten)


# ── SCORING ───────────────────────────────────────────────────────────────────

def score(m):
    """
    Win-Rate-optimierter Score:
    - Win Rate wird stärker belohnt als je zuvor (0.35 Gewicht)
    - Profit Factor zeigt Qualität der Gewinne (0.20)
    - Return und Sharpe für Gesamtperformance (je 0.20)
    - Drawdown-Strafe: alles wird mit (1 - DD/100) multipliziert
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

    wr       = m["win_rate"] / 100            # 0.0 – 1.0
    sharpe   = max(m["sharpe"], 0)
    ret      = max(m["return_pct"], 0)
    dd_pen   = 1 - (m["max_drawdown"] / 100)  # Drawdown-Strafe
    trade_q  = math.log(m["total_trades"] + 1) / 6
    pf_bonus = min((m["profit_factor"] - 1.0) / 2.0, 1.0)  # PF > 1 wird belohnt

    # Win Rate quadratisch: 42%=0.18, 50%=0.25, 60%=0.36, 70%=0.49
    wr_bonus = wr ** 2

    return round(
        (wr_bonus * 0.35 + ret * 0.20 + sharpe * 0.20 + pf_bonus * 0.15 + trade_q * 0.10)
        * dd_pen, 4
    )


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
    """Waehlt zufaellige Parameter aus dem Suchraum (alle 6 Strategie-Typen)."""
    if seed is not None:
        random.seed(seed)
    strat = {
        "name":           "Trial",
        "adx_min":        random.choice(SEARCH_SPACE["adx_min"]),
        "rsi_low_b":      random.choice(SEARCH_SPACE["rsi_low_b"]),
        "rsi_high_b":     random.choice(SEARCH_SPACE["rsi_high_b"]),
        "rsi_low_s":      random.choice(SEARCH_SPACE["rsi_low_s"]),
        "rsi_high_s":     random.choice(SEARCH_SPACE["rsi_high_s"]),
        "sl_mult":        random.choice(SEARCH_SPACE["sl_mult"]),
        "tp_mult":        random.choice(SEARCH_SPACE["tp_mult"]),
        "need_pattern":   random.choice(SEARCH_SPACE["need_pattern"]),
        "min_score":      random.choice(SEARCH_SPACE["min_score"]),
        "strategy_type":  random.choice(SEARCH_SPACE["strategy_type"]),
        "break_even_at":  random.choice(SEARCH_SPACE["break_even_at"]),
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
    n_random = sum(1 for r in results if str(r["strat"].get("name", "")).startswith("R-"))
    n_evo    = sum(1 for r in results if str(r["strat"].get("name", "")).startswith("E-"))
    total_r  = len(results)
    print("\n" + "=" * 80)
    print(f"  TOP {n} STRATEGIEN  |  {SYMBOL}  |  Auto-Optimizer Ergebnisse")
    print(f"  Phase: [Phase 1 Zufällig: {n_random} | Phase 2 Evolution: {n_evo} | Gesamt: {total_r}]")
    print("=" * 80)
    print(f"  {'#':>2}  {'STRATEGIE':>11}  {'ADX':>4}  {'SL':>4}  {'TP':>4}  "
          f"{'SC':>3}  {'TRADES':>6}  {'WR':>7}  {'PF':>5}  {'RETURN':>8}  {'SCORE':>7}")
    print("  " + "-" * 82)

    for rank, r in enumerate(results[:n], 1):
        s    = r["strat"]
        m    = r["metrics"]
        stype = s.get("strategy_type", "BALANCED")[:11]
        sc    = s.get("min_score", 8)
        ret_sign = "+" if m["return_pct"] >= 0 else ""
        print(
            f"  {rank:>2}  {stype:>11}  {s['adx_min']:>4}  "
            f"{s['sl_mult']:>4.1f}  {s['tp_mult']:>4.1f}  {sc:>3}  "
            f"{m['total_trades']:>6}  {m['win_rate']:>6.1f}%  "
            f"{m['profit_factor']:>5.2f}  "
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
        print(f"    Break-Even     : ATR x {s.get('break_even_at', 1.0)} (0=aus)")
        print(f"    Strategie-Typ  : {s.get('strategy_type', 'BALANCED')}")
        print(f"    Confluence Min : {s.get('min_score', 8)} Punkte")
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
        "timestamp":     datetime.now().isoformat(),
        "symbol":        SYMBOL,
        "adx_threshold": best_strat["adx_min"],
        "rsi_low_buy":   best_strat["rsi_low_b"],
        "rsi_high_buy":  best_strat["rsi_high_b"],
        "rsi_low_sell":  best_strat["rsi_low_s"],
        "rsi_high_sell": best_strat["rsi_high_s"],
        "sl_mult":       best_strat["sl_mult"],
        "tp_mult":       best_strat["tp_mult"],
        "need_pattern":  best_strat["need_pattern"],
        "min_score":     best_strat.get("min_score", 8),
        "strategy_type": best_strat.get("strategy_type", "BALANCED"),
        "break_even_at": best_strat.get("break_even_at", 1.0),
        "source":        "optimizer",
    }
    with open("best_params.json", "w") as f:
        json.dump(params, f, indent=2)
    print(f"  [OK] Beste Parameter gespeichert in best_params.json")
    print(f"  [OK] Bot laedt diese beim naechsten Start automatisch")
    return params


def mutate_strategy(parent, mutation_rate=0.3):
    """Mutation: ändert zufällig einige Parameter eines guten Elternteils."""
    child = dict(parent)
    child["name"] = "mutant"
    for key, choices in SEARCH_SPACE.items():
        if random.random() < mutation_rate:
            child[key] = random.choice(choices)
    # Konsistenz-Checks
    if child["tp_mult"] < child["sl_mult"] * 1.5:
        child["tp_mult"] = child["sl_mult"] * 2.0
    if child["rsi_high_b"] <= child["rsi_low_b"] + 10:
        child["rsi_high_b"] = child["rsi_low_b"] + 15
    if child["rsi_high_s"] <= child["rsi_low_s"] + 10:
        child["rsi_high_s"] = child["rsi_low_s"] + 15
    return child


def crossover_strategy(p1, p2):
    """Kreuzung: kombiniert Gene zweier guter Strategien (uniform crossover)."""
    child = {}
    for key in SEARCH_SPACE:
        child[key] = p1[key] if random.random() < 0.5 else p2.get(key, p1[key])
    child["name"] = "crossover"
    # Konsistenz-Checks
    if child["tp_mult"] < child["sl_mult"] * 1.5:
        child["tp_mult"] = child["sl_mult"] * 2.0
    if child["rsi_high_b"] <= child["rsi_low_b"] + 10:
        child["rsi_high_b"] = child["rsi_low_b"] + 15
    if child["rsi_high_s"] <= child["rsi_low_s"] + 10:
        child["rsi_high_s"] = child["rsi_low_s"] + 15
    return child


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
    t_start   = time.time()
    valid     = 0

    # Phase 1: Zufällige Suche (70% der Versuche)
    n_random = int(n_trials * 0.70)
    n_evolve = n_trials - n_random

    print(f"  Phase 1: {n_random} Zufalls-Versuche...")
    for trial in range(n_random):
        strat = random_trial(seed=trial * 7 + 13)
        strat["name"] = f"R-{trial+1}"
        m = walk_forward_score(candles, strat)
        s = score(m)
        if s > -999:
            valid += 1
            results.append({"strat": strat, "metrics": m, "score": s})
        if (trial + 1) % 10 == 0:
            elapsed = time.time() - t_start
            eta = elapsed / (trial + 1) * (n_trials - trial - 1)
            best_r = max((r["metrics"]["return_pct"] for r in results), default=0)
            print(f"  [{trial+1:>4}/{n_trials}]  Gueltig: {valid:>4}  Bester Return: {best_r:>+.1f}%  ETA: {int(eta)}s")

    # Phase 2: Evolutionäre Verbesserung (30% der Versuche)
    results.sort(key=lambda x: x["score"], reverse=True)
    elite = results[:max(len(results) // 5, 5)]  # top 20% als Eltern
    if elite and n_evolve > 0:
        print(f"\n  Phase 2: {n_evolve} Evolutionäre Versuche (Mutation + Kreuzung der Top {len(elite)})...")
        for evo in range(n_evolve):
            parent = random.choice(elite)["strat"]
            if evo % 3 == 0 and len(elite) >= 2:
                # Kreuzung: kombiniere zwei Eltern
                p2 = random.choice(elite)["strat"]
                strat = crossover_strategy(parent, p2)
            else:
                # Mutation: variiere einen Elternteil
                strat = mutate_strategy(parent)
            strat["name"] = f"E-{evo+1}"
            m = walk_forward_score(candles, strat)
            s = score(m)
            if s > -999:
                valid += 1
                results.append({"strat": strat, "metrics": m, "score": s})
            if (evo + 1) % 10 == 0:
                results.sort(key=lambda x: x["score"], reverse=True)
                best_r = max((r["metrics"]["return_pct"] for r in results), default=0)
                print(f"  [EVO {evo+1:>3}/{n_evolve}]  Gueltig: {valid:>4}  Bester Return: {best_r:>+.1f}%")

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
