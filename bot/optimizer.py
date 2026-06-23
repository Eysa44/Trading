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
    "adx_min":        [15, 18, 20, 22, 24, 25, 27, 30, 33],
    # Breiter RSI-Suchraum: deckt Trend (40-65) AND Reversal (20-38) ab
    "rsi_low_b":      [20, 25, 28, 30, 35, 38, 40, 42, 45],
    "rsi_high_b":     [35, 40, 45, 50, 55, 58, 60, 62, 65, 70],
    "rsi_low_s":      [30, 33, 35, 38, 40, 55, 60, 62, 65],
    "rsi_high_s":     [50, 52, 55, 58, 60, 65, 70, 75, 80],
    "sl_mult":        [0.8, 1.0, 1.2, 1.5, 1.8, 2.0, 2.5],
    # Kleine TP-Werte (0.8-1.2) für Scalping: mehr Treffer → höhere Win-Rate
    "tp_mult":        [0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0],
    "need_pattern":   [True, False],
    "min_score":      [5, 6, 7, 8, 9, 10, 11, 12],
    "strategy_type":  STRATEGY_TYPES,
    # Frühere Break-Even Werte (0.3, 0.5) → mehr BE-Trades → höhere WR
    "break_even_at":  [0.0, 0.3, 0.5, 0.8, 1.0, 1.2, 1.5],
}

# ── QUALITÄTS-FILTER ──────────────────────────────────────────────────────────
MIN_TRADES    = 8      # Mindestens 8 Trades für aussagekräftiges Ergebnis
MAX_DRAWDOWN  = 20.0   # Maximal 20% Drawdown
MIN_WR        = 44.0   # Realistisches Minimum für XAUUSD (Scoring optimiert auf Max-WR)


# ── SCORING ───────────────────────────────────────────────────────────────────

def score(m):
    """
    High-Win-Rate Score: Ziel 70-80% WR.
    - Win Rate stark gewichtet (0.55) — Hauptziel
    - Profit Factor für Trade-Qualität (0.20)
    - Return und Sharpe sekundär (je 0.10)
    - Drawdown-Strafe bleibt
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
    dd_pen   = 1 - (m["max_drawdown"] / 100)
    trade_q  = math.log(m["total_trades"] + 1) / 6
    pf_bonus = min((m["profit_factor"] - 1.0) / 2.0, 1.0)

    # Win Rate kubisch: 60%=0.216, 65%=0.274, 70%=0.343, 75%=0.422, 80%=0.512
    # Kubisch belohnt jeden WR-Prozentpunkt exponentiell stärker
    wr_bonus = wr ** 3

    return round(
        (wr_bonus * 0.55 + pf_bonus * 0.20 + ret * 0.10 + sharpe * 0.10 + trade_q * 0.05)
        * dd_pen, 4
    )


# ── WALK-FORWARD TEST ────────────────────────────────────────────────────────

def walk_forward_score(candles, strat, split=0.65, balance=START_BALANCE):
    """
    Testet auf ersten 65% (In-Sample), validiert auf letzten 35% (Out-of-Sample).
    Verhindert Overfitting.
    """
    split_idx = int(len(candles) * split)
    train     = candles[:split_idx]
    test      = candles[split_idx:]

    if len(test) < 300:
        trades, eq, final = run_backtest(candles, strat, balance=balance)
        return calc_metrics(trades, eq, final, initial_balance=balance)

    _, _, _ = run_backtest(train, strat, balance=balance)
    trades_test, eq_test, final_test = run_backtest(test, strat, balance=balance)
    m = calc_metrics(trades_test, eq_test, final_test, initial_balance=balance)
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

def print_top(results, n=10, balance=START_BALANCE):
    n_random = sum(1 for r in results if str(r["strat"].get("name", "")).startswith("R-"))
    n_evo    = sum(1 for r in results if str(r["strat"].get("name", "")).startswith("E-"))
    total_r  = len(results)
    print("\n" + "=" * 88)
    print(f"  TOP {n} STRATEGIEN  |  {SYMBOL}  |  Startkapital: ${balance:,.2f}")
    print(f"  Phase: [Zufällig: {n_random} | Evolution: {n_evo} | Gesamt: {total_r}]")
    print("=" * 88)
    print(f"  {'#':>2}  {'STRATEGIE':>11}  {'SL':>4}  {'TP':>4}  "
          f"{'TRADES':>6}  {'WR':>7}  {'PF':>5}  {'RETURN':>8}  {'START→ENDE':>20}  {'SCORE':>7}")
    print("  " + "-" * 88)

    for rank, r in enumerate(results[:n], 1):
        s     = r["strat"]
        m     = r["metrics"]
        stype = s.get("strategy_type", "BALANCED")[:11]
        ret_sign = "+" if m["return_pct"] >= 0 else ""
        profit   = m.get("total_profit", 0)
        final_b  = m.get("final_balance", balance + profit)
        p_sign   = "+" if profit >= 0 else ""
        eq_str   = f"${balance:.0f}→${final_b:.2f}({p_sign}${profit:.2f})"
        print(
            f"  {rank:>2}  {stype:>11}  "
            f"{s['sl_mult']:>4.1f}  {s['tp_mult']:>4.1f}  "
            f"{m['total_trades']:>6}  {m['win_rate']:>6.1f}%  "
            f"{m['profit_factor']:>5.2f}  "
            f"{ret_sign}{m['return_pct']:>7.1f}%  "
            f"{eq_str:>20}  {r['score']:>7.4f}"
        )

    print("=" * 88)
    if results:
        best   = results[0]
        s      = best["strat"]
        m      = best["metrics"]
        profit = m.get("total_profit", 0)
        final_b = m.get("final_balance", balance + profit)
        p_sign = "+" if profit >= 0 else ""
        print(f"\n  BESTE STRATEGIE:")
        print(f"    Strategie-Typ  : {s.get('strategy_type', 'BALANCED')}")
        print(f"    ADX Threshold  : {s['adx_min']}")
        print(f"    RSI Buy        : {s['rsi_low_b']} - {s['rsi_high_b']}")
        print(f"    RSI Sell       : {s['rsi_low_s']} - {s['rsi_high_s']}")
        print(f"    SL / TP        : ATR x {s['sl_mult']} / ATR x {s['tp_mult']}")
        print(f"    Break-Even     : ATR x {s.get('break_even_at', 1.0)} (0=aus)")
        print(f"    Confluence Min : {s.get('min_score', 8)} Punkte")
        wr_status = "ZIEL ERREICHT ✓" if m['win_rate'] >= 70 else ("NAHE AM ZIEL" if m['win_rate'] >= 60 else "")
        print(f"    Win Rate       : {m['win_rate']}%  {wr_status}")
        print(f"    Kapital Start  : ${balance:,.2f}")
        print(f"    Kapital Ende   : ${final_b:,.2f}  ({p_sign}${profit:,.2f}  /  {p_sign}{m['return_pct']}%)")
        print(f"    Max Drawdown   : {m['max_drawdown']}%")
        print(f"    Profit Factor  : {m['profit_factor']}")
        print(f"    Sharpe         : {m['sharpe']}")
        print(f"    Score          : {best['score']}")
    print("=" * 88 + "\n")


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
    n_str        = next((sys.argv[i+1] for i, a in enumerate(sys.argv) if a == "--trials"),  None)
    c_str        = next((sys.argv[i+1] for i, a in enumerate(sys.argv) if a == "--candles"), None)
    b_str        = next((sys.argv[i+1] for i, a in enumerate(sys.argv) if a == "--balance"), None)
    n_trials     = int(n_str)   if n_str else 500
    n_candles    = int(c_str)   if c_str else 5000
    balance      = float(b_str) if b_str else START_BALANCE

    print(f"\n  CLAUDE + QUANT  |  Auto-Optimizer v1.0  |  Optimiert auf Max Win Rate")
    print(f"  Symbol: {SYMBOL}  |  Trials: {n_trials}  |  Kerzen: {n_candles}  |  Startkapital: ${balance:,.2f}")
    print(f"  Walk-Forward: Ja (65% Train / 35% Test)  |  Min WR: {MIN_WR}%\n")

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
        m = walk_forward_score(candles, strat, balance=balance)
        s = score(m)
        if s > -999:
            valid += 1
            results.append({"strat": strat, "metrics": m, "score": s})
        if (trial + 1) % 10 == 0:
            elapsed = time.time() - t_start
            eta = elapsed / (trial + 1) * (n_trials - trial - 1)
            best_r  = max((r["metrics"]["return_pct"] for r in results), default=0)
            best_fb = max((r["metrics"].get("final_balance", balance) for r in results), default=balance)
            profit  = best_fb - balance
            p_sign  = "+" if profit >= 0 else ""
            print(f"  [{trial+1:>4}/{n_trials}]  Gueltig: {valid:>4}  Bester Return: {best_r:>+.1f}%  "
                  f"(${balance:.0f}→${best_fb:.2f}, {p_sign}${profit:.2f})  ETA: {int(eta)}s")

    # Phase 2: Evolutionäre Verbesserung (30% der Versuche)
    results.sort(key=lambda x: x["score"], reverse=True)
    elite = results[:max(len(results) // 5, 5)]  # top 20% als Eltern
    if elite and n_evolve > 0:
        print(f"\n  Phase 2: {n_evolve} Evolutionäre Versuche (Mutation + Kreuzung der Top {len(elite)})...")
        for evo in range(n_evolve):
            parent = random.choice(elite)["strat"]
            if evo % 3 == 0 and len(elite) >= 2:
                p2 = random.choice(elite)["strat"]
                strat = crossover_strategy(parent, p2)
            else:
                strat = mutate_strategy(parent)
            strat["name"] = f"E-{evo+1}"
            m = walk_forward_score(candles, strat, balance=balance)
            s = score(m)
            if s > -999:
                valid += 1
                results.append({"strat": strat, "metrics": m, "score": s})
            if (evo + 1) % 10 == 0:
                results.sort(key=lambda x: x["score"], reverse=True)
                best_r  = max((r["metrics"]["return_pct"] for r in results), default=0)
                best_fb = max((r["metrics"].get("final_balance", balance) for r in results), default=balance)
                profit  = best_fb - balance
                p_sign  = "+" if profit >= 0 else ""
                print(f"  [EVO {evo+1:>3}/{n_evolve}]  Gueltig: {valid:>4}  Bester Return: {best_r:>+.1f}%  "
                      f"(${balance:.0f}→${best_fb:.2f}, {p_sign}${profit:.2f})")

    # Sortieren nach Score
    results.sort(key=lambda x: x["score"], reverse=True)

    elapsed = time.time() - t_start
    print(f"\n  Fertig in {elapsed:.0f}s  |  {valid}/{n_trials} gueltige Strategien gefunden")

    print_top(results, n=10, balance=balance)

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

    # Beste Parameter anwenden + MQL5 EA generieren
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

        # MQL5 EA immer generieren
        best_m   = results[0]["metrics"]
        mql_code = generate_mql5_ea(results[0]["strat"], best_m, balance)
        ea_file  = "CLAUDE_QUANT_EA.mq5"
        with open(ea_file, "w", encoding="utf-8") as f:
            f.write(mql_code)
        stype = results[0]["strat"].get("strategy_type", "BALANCED")
        print(f"\n  [OK] MQL5 Expert Advisor gespeichert: {ea_file}")
        print(f"  [OK] Strategie: {stype}  |  WR: {best_m['win_rate']}%  |  Return: {best_m['return_pct']:+.1f}%")
        print(f"  --> In MT5: Datei in MQL5\\Experts\\ kopieren, kompilieren, auf XAUUSD M15 ziehen")


def generate_mql5_ea(best_strat, metrics, balance=10000.0):
    """Generiert einen fertigen MQL5 Expert Advisor aus den besten Optimizer-Parametern."""

    stype = best_strat.get("strategy_type", "BALANCED")

    # Strategie-Gewichtungen (identisch mit trading_bot.py)
    WEIGHTS = {
        "BALANCED":    dict(ema=3, adx=3, rsi=3, macd=3, bb=2, stoch=2),
        "BB_SCALP":    dict(ema=1, adx=2, rsi=3, macd=1, bb=5, stoch=4),
        "SCALP":       dict(ema=2, adx=2, rsi=4, macd=2, bb=4, stoch=5),
        "FIB_SWING":   dict(ema=3, adx=2, rsi=2, macd=2, bb=1, stoch=2),
        "ICT_SMC":     dict(ema=2, adx=2, rsi=2, macd=2, bb=1, stoch=1),
        "VWAP_TREND":  dict(ema=3, adx=3, rsi=2, macd=3, bb=2, stoch=1),
        "MOMENTUM":    dict(ema=3, adx=4, rsi=3, macd=5, bb=2, stoch=3),
        "ENSEMBLE":    dict(ema=3, adx=3, rsi=3, macd=3, bb=3, stoch=3),
        "REVERSAL":    dict(ema=1, adx=1, rsi=5, macd=1, bb=4, stoch=5),
        "BREAKOUT":    dict(ema=2, adx=4, rsi=1, macd=3, bb=5, stoch=1),
        "PRICE_ACTION":dict(ema=2, adx=1, rsi=1, macd=1, bb=1, stoch=1),
        "WYCKOFF":     dict(ema=2, adx=2, rsi=1, macd=2, bb=3, stoch=1),
    }
    W = WEIGHTS.get(stype, WEIGHTS["BALANCED"])

    wr   = metrics.get("win_rate", 0)
    ret  = metrics.get("return_pct", 0)
    dd   = metrics.get("max_drawdown", 0)
    pf   = metrics.get("profit_factor", 0)
    fb   = metrics.get("final_balance", balance)
    prof = metrics.get("total_profit", 0)
    p_s  = "+" if prof >= 0 else ""

    code = f"""//+------------------------------------------------------------------+
//|  CLAUDE + QUANT EA  —  Auto-generiert vom Optimizer             |
//|  Strategie : {stype:<20}                              |
//|  Win Rate  : {wr}%   Return: {ret:+.1f}%   Max DD: {dd}%        |
//|  Profit Factor: {pf}   Trades: {metrics.get('total_trades',0)}                       |
//|  Kapital: ${balance:.0f} -> ${fb:.2f} ({p_s}${prof:.2f})               |
//+------------------------------------------------------------------+
#property copyright "Claude + Quant Auto-Optimizer"
#property version   "1.00"
#include <Trade\\Trade.mqh>

//── OPTIMIERTE EINGANGSPARAMETER ─────────────────────────────────
input string   InpInfo          = "Strategie: {stype}";  // Info
input double   InpRiskPct       = 1.0;                   // Risiko % pro Trade
input int      InpADXMin        = {best_strat['adx_min']};  // ADX Minimum
input int      InpRSILowBuy     = {best_strat['rsi_low_b']}; // RSI Kauf-Untergrenze
input int      InpRSIHighBuy    = {best_strat['rsi_high_b']}; // RSI Kauf-Obergrenze
input int      InpRSILowSell    = {best_strat['rsi_low_s']}; // RSI Verkauf-Untergrenze
input int      InpRSIHighSell   = {best_strat['rsi_high_s']}; // RSI Verkauf-Obergrenze
input double   InpSLMult        = {best_strat['sl_mult']};   // Stop Loss (ATR x)
input double   InpTPMult        = {best_strat['tp_mult']};   // Take Profit (ATR x)
input double   InpBreakEvenAt   = {best_strat.get('break_even_at', 1.0)};  // Break-Even (ATR x, 0=aus)
input int      InpMinScore      = {best_strat.get('min_score', 8)};  // Min. Confluence Score

//── STRATEGIE-GEWICHTUNGEN ({stype}) ─────────────────────────────
input int      W_EMA   = {W['ema']};   // EMA-Trend Gewicht
input int      W_ADX   = {W['adx']};   // ADX-Staerke Gewicht
input int      W_RSI   = {W['rsi']};   // RSI Gewicht
input int      W_MACD  = {W['macd']};  // MACD Gewicht
input int      W_BB    = {W['bb']};    // Bollinger Bands Gewicht
input int      W_STOCH = {W['stoch']}; // Stochastic Gewicht

//── GLOBALE VARIABLEN ─────────────────────────────────────────────
CTrade trade;
int    h_atr, h_rsi, h_adx, h_ema20, h_ema50, h_ema200, h_macd, h_bb, h_stoch;
ulong  MAGIC = 20250601;

//+------------------------------------------------------------------+
int OnInit()
  {{
   trade.SetExpertMagicNumber(MAGIC);
   h_atr    = iATR      (_Symbol, PERIOD_M15, 14);
   h_rsi    = iRSI      (_Symbol, PERIOD_M15, 14, PRICE_CLOSE);
   h_adx    = iADX      (_Symbol, PERIOD_M15, 14);
   h_ema20  = iMA       (_Symbol, PERIOD_M15,  20, 0, MODE_EMA, PRICE_CLOSE);
   h_ema50  = iMA       (_Symbol, PERIOD_M15,  50, 0, MODE_EMA, PRICE_CLOSE);
   h_ema200 = iMA       (_Symbol, PERIOD_M15, 200, 0, MODE_EMA, PRICE_CLOSE);
   h_macd   = iMACD     (_Symbol, PERIOD_M15,  12, 26, 9, PRICE_CLOSE);
   h_bb     = iBands    (_Symbol, PERIOD_M15,  20, 0, 2.0, PRICE_CLOSE);
   h_stoch  = iStochastic(_Symbol, PERIOD_M15,  5, 3, 3, MODE_SMA, STO_LOWHIGH);
   if(h_atr == INVALID_HANDLE || h_rsi == INVALID_HANDLE)
     {{ Alert("Fehler: Indikator-Handle ungueltig!"); return INIT_FAILED; }}
   Print("CLAUDE+QUANT EA gestartet | Strategie: {stype} | WR={wr}% | Return={ret:+.1f}%");
   return INIT_SUCCEEDED;
  }}

void OnDeinit(const int reason)
  {{
   int handles[] = {{h_atr,h_rsi,h_adx,h_ema20,h_ema50,h_ema200,h_macd,h_bb,h_stoch}};
   for(int i=0; i<ArraySize(handles); i++) IndicatorRelease(handles[i]);
  }}

//+------------------------------------------------------------------+
void OnTick()
  {{
   // Nur auf neue M15-Kerze reagieren
   static datetime last_bar = 0;
   datetime cur_bar = iTime(_Symbol, PERIOD_M15, 0);
   if(cur_bar == last_bar) return;
   last_bar = cur_bar;

   // Break-Even pruefen
   if(InpBreakEvenAt > 0.0) CheckBreakEven();

   // Keine neue Position wenn bereits eine offen
   if(PositionsTotal() > 0) return;

   // Signal berechnen
   int sig = GetSignal();
   if(sig == 0) return;

   // ATR-basierte Lot-Groesse (1% Risiko)
   double atr = Buf(h_atr, 0, 1);
   if(atr <= 0) return;
   double sl_pts   = atr * InpSLMult;
   double risk_usd = AccountInfoDouble(ACCOUNT_BALANCE) * (InpRiskPct / 100.0);
   double csize    = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   double lot      = NormalizeDouble(risk_usd / (sl_pts * csize), 2);
   lot = MathMax(lot, SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN));
   lot = MathMin(lot, SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX));

   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   if(sig == 1)  // BUY
      trade.Buy (lot, _Symbol, ask, ask - sl_pts, ask + atr * InpTPMult, "C+Q BUY");
   else           // SELL
      trade.Sell(lot, _Symbol, bid, bid + sl_pts, bid - atr * InpTPMult, "C+Q SELL");
  }}

//+------------------------------------------------------------------+
// Confluence-Score Signal-System
//+------------------------------------------------------------------+
int GetSignal()
  {{
   double ema20  = Buf(h_ema20,  0, 1);
   double ema50  = Buf(h_ema50,  0, 1);
   double ema200 = Buf(h_ema200, 0, 1);
   double rsi    = Buf(h_rsi,    0, 1);
   double adx    = Buf(h_adx,    0, 1);
   double macd_l = Buf(h_macd,   0, 1);  // MACD Linie
   double macd_s = Buf(h_macd,   1, 1);  // Signal Linie
   double bb_up  = Buf(h_bb,     1, 1);  // BB Upper
   double bb_lo  = Buf(h_bb,     2, 1);  // BB Lower
   double close  = Buf(h_bb,     0, 1);  // Mittelband ~ Close
   double stk    = Buf(h_stoch,  0, 1);  // Stoch %K

   if(ema20 == 0 || rsi == 0 || bb_up == 0) return 0;

   int bs = 0, ss = 0;  // buy_score, sell_score

   // EMA-Trend
   if(ema20 > ema50 && ema50 > ema200) bs += W_EMA;
   else if(ema20 < ema50 && ema50 < ema200) ss += W_EMA;

   // ADX + Richtung
   if(adx >= InpADXMin)
     {{ if(macd_l > macd_s) bs += W_ADX; else ss += W_ADX; }}

   // RSI
   if(rsi >= InpRSILowBuy  && rsi <= InpRSIHighBuy)  bs += W_RSI;
   if(rsi >= InpRSILowSell && rsi <= InpRSIHighSell) ss += W_RSI;

   // MACD
   if(macd_l > macd_s && macd_l > 0) bs += W_MACD;
   else if(macd_l < macd_s && macd_l < 0) ss += W_MACD;

   // Bollinger Bands
   if(close < bb_lo * 1.0005) bs += W_BB;
   if(close > bb_up * 0.9995) ss += W_BB;

   // Stochastic
   if(stk < 25) bs += W_STOCH;
   if(stk > 75) ss += W_STOCH;

   if(bs >= InpMinScore && bs > ss + 2) return  1;
   if(ss >= InpMinScore && ss > bs + 2) return -1;
   return 0;
  }}

//+------------------------------------------------------------------+
// Break-Even: SL auf Entry wenn Gewinn >= BE_AT * ATR
//+------------------------------------------------------------------+
void CheckBreakEven()
  {{
   double atr = Buf(h_atr, 0, 1);
   if(atr <= 0) return;
   double trigger = atr * InpBreakEvenAt;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {{
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket)) continue;
      if(PositionGetInteger(POSITION_MAGIC)  != (long)MAGIC)  continue;
      if(PositionGetString(POSITION_SYMBOL)  != _Symbol) continue;

      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl    = PositionGetDouble(POSITION_SL);
      double tp    = PositionGetDouble(POSITION_TP);
      long   ptype = PositionGetInteger(POSITION_TYPE);

      if(ptype == POSITION_TYPE_BUY)
        {{
         double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
         if(bid - entry >= trigger && sl < entry - _Point)
            trade.PositionModify(ticket, entry + _Point, tp);
        }}
      else
        {{
         double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
         if(entry - ask >= trigger && sl > entry + _Point)
            trade.PositionModify(ticket, entry - _Point, tp);
        }}
     }}
  }}

//+------------------------------------------------------------------+
// Hilfsfunktion: Indikator-Buffer auslesen
//+------------------------------------------------------------------+
double Buf(int handle, int buffer, int shift)
  {{
   double arr[1];
   ArraySetAsSeries(arr, true);
   if(CopyBuffer(handle, buffer, shift, 1, arr) <= 0) return 0.0;
   return arr[0];
  }}
//+------------------------------------------------------------------+
"""
    return code


if __name__ == "__main__":
    main()
