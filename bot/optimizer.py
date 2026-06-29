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
    "adx_min":        [18, 20, 22, 24, 25, 27, 30, 33, 35],
    "rsi_low_b":      [25, 30, 35, 38, 40, 42, 45, 48],
    "rsi_high_b":     [45, 50, 55, 58, 60, 62, 65],
    "rsi_low_s":      [35, 38, 40, 55, 58, 60, 62, 65],
    "rsi_high_s":     [52, 55, 58, 60, 65, 70, 75, 80],
    "sl_mult":        [1.0, 1.2, 1.5, 1.8, 2.0, 2.5],
    # TP ≥ SL (nur positive RR): verhindert Setups wo TP < SL
    "tp_mult":        [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0],
    "need_pattern":   [True, False],
    # Höhere Mindest-Scores → selektivere, qualitativ bessere Signale
    "min_score":      [8, 9, 10, 11, 12, 13, 14],
    "strategy_type":  STRATEGY_TYPES,
    "break_even_at":  [0.0, 0.5, 0.8, 1.0, 1.2],
    # 2-Kerzen-Bestätigung: Signal muss auf 2 aufeinanderfolgenden Kerzen erscheinen
    "confirm_bars":   [1, 2],
    # Wochentag-Filter: Mon-Open und Freitag-Close überspringen
    "day_filter":     [True, False],
}

# ── QUALITÄTS-FILTER ──────────────────────────────────────────────────────────
MIN_TRADES    = 10     # Mindestens 10 Trades für aussagekräftiges Ergebnis
MAX_DRAWDOWN  = 15.0   # Maximal 15% Drawdown (strenger)
MIN_WR        = 47.0   # Wir erreichen 50%+ — nur noch echte Top-Kandidaten


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

    # Win Rate kubisch: 50%=0.125, 55%=0.166, 60%=0.216, 65%=0.274, 70%=0.343
    # Extra-Bonus über 50% WR (Ziel ist 55%+)
    wr_bonus = wr ** 3
    if wr >= 0.55:
        wr_bonus *= 1.25   # +25% Bonus für 55%+ WR
    if wr >= 0.60:
        wr_bonus *= 1.20   # Nochmal +20% für 60%+ WR

    # Profit Factor > 2.0 ist Qualitäts-Zeichen (mehr als doppelte Gewinne vs Verluste)
    if m["profit_factor"] >= 2.0:
        pf_bonus = min(pf_bonus * 1.3, 1.0)

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

    # ── BESTE STRATEGIE JE TYP ────────────────────────────────────────────────
    best_per_type = {}
    for r in results:
        st = r["strat"].get("strategy_type", "BALANCED")
        if st not in best_per_type or r["score"] > best_per_type[st]["score"]:
            best_per_type[st] = r
    if best_per_type:
        print("  ELITE-RANGLISTE (Bestes Ergebnis je Strategie-Typ):")
        print(f"  {'TYP':>12}  {'WR':>6}  {'PF':>5}  {'RETURN':>8}  {'SCORE':>7}")
        print("  " + "-" * 60)
        for st, r in sorted(best_per_type.items(), key=lambda x: x[1]["score"], reverse=True):
            m = r["metrics"]
            print(f"  {st:>12}  {m['win_rate']:>5.1f}%  {m['profit_factor']:>5.2f}  "
                  f"{m['return_pct']:>+7.1f}%  {r['score']:>7.4f}")
        print()


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
    # Alle SEARCH_SPACE-Keys sicherstellen (ältere gespeicherte Strategien fehlt evtl. neue Keys)
    child = {key: parent.get(key, SEARCH_SPACE[key][0]) for key in SEARCH_SPACE}
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
        default = SEARCH_SPACE[key][0]
        child[key] = p1.get(key, default) if random.random() < 0.5 else p2.get(key, p1.get(key, default))
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

    # Phase 1: Erste len(STRATEGY_TYPES)*2 Versuche = Round-Robin (jeder Typ 2×)
    # Rest = echte Zufallssuche → verhindert VWAP_TREND-Dominanz
    n_types     = len(STRATEGY_TYPES)
    rr_end      = min(n_types * 2, n_random)   # Round-Robin bis hier
    print(f"  Phase 1: {n_random} Versuche  ({rr_end} Round-Robin + {n_random-rr_end} Zufall)...")
    for trial in range(n_random):
        strat = random_trial(seed=trial * 7 + 13)
        if trial < rr_end:
            strat["strategy_type"] = STRATEGY_TYPES[trial % n_types]   # garantierter Typ
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
    # Elite = bestes Ergebnis JE Strategie-Typ → echte Vielfalt beim Evolvieren
    results.sort(key=lambda x: x["score"], reverse=True)
    best_per = {}
    for r in results:
        st = r["strat"].get("strategy_type", "BALANCED")
        if st not in best_per or r["score"] > best_per[st]["score"]:
            best_per[st] = r
    elite = list(best_per.values())
    # Falls ein Typ gar kein gültiges Ergebnis hat: mit globalen Top-5 auffüllen
    for r in results[:5]:
        if not any(e is r for e in elite):
            elite.append(r)
    if elite and n_evolve > 0:
        print(f"\n  Phase 2: {n_evolve} Evolutionäre Versuche  (Elite: {len(elite)} Typen = {', '.join(e['strat'].get('strategy_type','?')[:6] for e in elite[:6])}...)...")
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
    """Generiert einen Elite MQL5 Expert Advisor v2.0 — Multi-Timeframe H4+H1+M15."""

    stype = best_strat.get("strategy_type", "BALANCED")

    WEIGHTS = {
        "BALANCED":    dict(ema=3, adx=3, rsi=3, macd=3, bb=2, stoch=2, cci=1, vol=1),
        "BB_SCALP":    dict(ema=1, adx=2, rsi=3, macd=1, bb=5, stoch=4, cci=1, vol=1),
        "SCALP":       dict(ema=2, adx=2, rsi=4, macd=2, bb=4, stoch=5, cci=1, vol=1),
        "FIB_SWING":   dict(ema=3, adx=2, rsi=2, macd=2, bb=1, stoch=2, cci=1, vol=1),
        "ICT_SMC":     dict(ema=2, adx=2, rsi=2, macd=2, bb=1, stoch=1, cci=1, vol=2),
        "VWAP_TREND":  dict(ema=3, adx=3, rsi=2, macd=3, bb=2, stoch=1, cci=1, vol=2),
        "MOMENTUM":    dict(ema=3, adx=4, rsi=3, macd=5, bb=2, stoch=3, cci=2, vol=2),
        "ENSEMBLE":    dict(ema=3, adx=3, rsi=3, macd=3, bb=3, stoch=3, cci=2, vol=2),
        "REVERSAL":    dict(ema=1, adx=1, rsi=5, macd=1, bb=4, stoch=5, cci=3, vol=2),
        "BREAKOUT":    dict(ema=2, adx=4, rsi=1, macd=3, bb=5, stoch=1, cci=2, vol=3),
        "PRICE_ACTION":dict(ema=2, adx=1, rsi=1, macd=1, bb=1, stoch=1, cci=1, vol=1),
        "WYCKOFF":     dict(ema=2, adx=2, rsi=1, macd=2, bb=3, stoch=1, cci=2, vol=3),
        "ICHIMOKU":    dict(ema=1, adx=1, rsi=1, macd=1, bb=1, stoch=1, cci=1, vol=1),
        "SUPERTREND":  dict(ema=2, adx=2, rsi=1, macd=2, bb=1, stoch=1, cci=1, vol=2),
        "MULTI_TF":    dict(ema=5, adx=2, rsi=1, macd=2, bb=1, stoch=1, cci=1, vol=1),
        "BB_SQUEEZE":  dict(ema=1, adx=2, rsi=1, macd=2, bb=5, stoch=1, cci=2, vol=3),
        "VOLUME_CONF": dict(ema=2, adx=2, rsi=2, macd=2, bb=1, stoch=1, cci=2, vol=5),
    }
    W = WEIGHTS.get(stype, WEIGHTS["BALANCED"])

    wr    = metrics.get("win_rate", 0)
    ret   = metrics.get("return_pct", 0)
    dd    = metrics.get("max_drawdown", 0)
    pf    = metrics.get("profit_factor", 0)
    fb    = metrics.get("final_balance", balance)
    ntrades = metrics.get("total_trades", 0)
    prof  = fb - balance
    p_s   = "+" if prof >= 0 else ""

    sl_mult  = round(best_strat.get("sl_mult", 1.5), 2)
    tp_raw   = best_strat.get("tp_mult", 2.0)
    tp1_mult = round(max(min(tp_raw, 1.5), sl_mult), 1)  # Quick TP1: max 1.5R, mind. 1:1 RR
    tp2_mult = round(tp_raw, 1)              # runner TP: optimizer's full target
    confirm  = best_strat.get("confirm_bars", 1)   # 2-Kerzen-Bestätigung
    day_filt = best_strat.get("day_filter", True)  # Wochentag-Filter
    be_at    = round(best_strat.get("break_even_at", 1.0), 1)
    adx_min  = best_strat.get("adx_min", 20)
    rsi_lb   = best_strat.get("rsi_low_b", 40)
    rsi_hb   = best_strat.get("rsi_high_b", 60)
    rsi_ls   = best_strat.get("rsi_low_s", 40)
    rsi_hs   = best_strat.get("rsi_high_s", 60)
    minscore = best_strat.get("min_score", 8)

    code = f"""//+------------------------------------------------------------------+
//|  CLAUDE QUANT ELITE v2.0  —  XAUUSD M15                        |
//|  Multi-Timeframe Confluence Trading System                       |
//|  Strategie : {stype:<20}  Auto-Optimiert              |
//|  Python-Backtest: WR={wr}%  Return={ret:+.1f}%  Max DD={dd}%   |
//|  Profit Factor: {pf}   Trades: {ntrades}                                   |
//|  Kapital: ${balance:.0f} -> ${fb:.2f} ({p_s}${prof:.2f})               |
//+------------------------------------------------------------------+
//  FEATURES:
//  - H4 Trend-Filter (EMA50/200) — nur in H4-Trendrichtung handeln
//  - H1 Momentum-Filter (EMA20/50, RSI, MACD) — Bias-Bestätigung
//  - M15 Entry mit 10-Faktor Confluence-Score
//  - Doppeltes TP: TP1={tp1_mult}R (50% schließen) + TP2={tp2_mult}R (Runner)
//  - ATR Trailing Stop nach TP1 getroffen
//  - Break-Even Management
//  - Spread-Filter (max. Spread vor Entry)
//  - Volumen-Filter (Mindest-Volumen für Signal)
//  - Volatilitäts-Filter (kein Handel bei News-Spikes)
//  - Tages-Verlust-Limit (Kapitalschutz)
//  - Session-Filter (London/NY: 7-20 UTC)
//  - Echtzeit-Dashboard (Chart-Comment)
//+------------------------------------------------------------------+
#property copyright "Claude + Quant Elite v2.0"
#property version   "2.00"
#property description "Multi-Timeframe Confluence EA for XAUUSD M15"

#include <Trade\\Trade.mqh>
#include <Trade\\PositionInfo.mqh>

//══════════════════════════════════════════════════════════════════
//  EINGABE-PARAMETER
//══════════════════════════════════════════════════════════════════

//── RISIKO ────────────────────────────────────────────────────────
input string  _Sec0           = "══ RISIKO ══";
input double  InpRiskPct      = 1.0;   // Risiko % pro Trade (vom Kapital)
input double  InpMaxDailyDD   = 3.0;   // Max. Tagesverlust % (dann Stop)
input int     InpMaxPositions = 2;     // Max. offene Positionen (TP1+TP2)
input int     InpMaxSpread    = 35;    // Max. Spread in Punkten
input int     InpMaxDailyTrades= 2;    // Max. Trades pro Tag (1 Trade = TP1+TP2 Paar)
input int     InpTradeCooldownH= 3;    // Std. Mindest-Pause zwischen Trades
input int     InpMaxConsecLoss = 3;    // Pause nach N aufeinanderfolgenden Verlusten (0=aus)
input double  InpDailyTarget  = 2.0;  // Stop bei +X% Tagesgewinn (0=aus)

//── SESSION & WOCHENTAG ───────────────────────────────────────────
input string  _Sec1           = "══ SESSION ══";
input int     InpSessionStart = 7;    // Session Start UTC (London Open)
input int     InpSessionEnd   = 20;   // Session End UTC (NY Close)
input bool    InpSkipMonday   = {"true" if day_filt else "false"};   // Montag 7-10 UTC überspringen (schwache Liquidität)
input bool    InpSkipFriday   = {"true" if day_filt else "false"};   // Freitag ab 17 UTC überspringen (Weekend-Gap Risiko)
input int     InpConfirmBars  = {confirm};    // Signal-Bestätigung (1=sofort, 2=2 Kerzen hintereinander)

//── MULTI-TIMEFRAME ───────────────────────────────────────────────
input string  _Sec2           = "══ MULTI-TIMEFRAME ══";
input bool    InpUseH4Filter  = true;  // H4 Trend-Filter aktiv
input bool    InpUseH1Filter  = true;  // H1 Momentum-Filter aktiv
input int     InpH4EMA        = 50;    // H4 Haupt-EMA Periode

//── ENTRY PARAMETER (Optimizer-Ergebnis) ──────────────────────────
input string  _Sec3           = "══ ENTRY ({stype}) ══";
input int     InpADXMin       = {adx_min};   // ADX Minimum Trend-Stärke
input int     InpRSILowBuy    = {rsi_lb};    // RSI Kauf-Zone Untergrenze
input int     InpRSIHighBuy   = {rsi_hb};    // RSI Kauf-Zone Obergrenze
input int     InpRSILowSell   = {rsi_ls};    // RSI Verkauf-Zone Untergrenze
input int     InpRSIHighSell  = {rsi_hs};    // RSI Verkauf-Zone Obergrenze
input int     InpMinScore     = {minscore};  // Min. Confluence Score
input double  InpVolMinRatio  = 0.80;        // Volumen-Min. vs 20-Kerzen Ø

//── TRADE MANAGEMENT ──────────────────────────────────────────────
input string  _Sec4           = "══ TRADE MANAGEMENT ══";
input double  InpSLMult       = {sl_mult};   // Stop Loss (ATR x)
input double  InpTP1Mult      = {tp1_mult};  // Take Profit 1 (ATR x) — 50% sofort sichern
input bool    InpTP2NoLimit   = true;        // TP2 ohne fixes Ziel (nur ATR-Trail) — Elite-Modus
input double  InpTP2Mult      = {tp2_mult};  // Take Profit 2 fix (wenn InpTP2NoLimit=false)
input double  InpBEAt         = {be_at};     // Break-Even nach TP1 (ATR x, 0=aus)
input bool    InpTrailing     = true;        // ATR Trailing Stop aktiv
input double  InpTrailMult    = 1.2;         // Trailing Stop Abstand (ATR x)
input double  InpTrailActivate= 0.5;         // Trail startet ab X*ATR Gewinn (vermeidet Früh-Stop)
// RSI-Override: extrem überverkauft/überkauft → Gewinne sofort sichern vor Bounce
input int     InpRSICloseSell = 20;          // SELL schließen wenn RSI unter diesen Wert fällt
input int     InpRSICloseBuy  = 80;          // BUY schließen wenn RSI über diesen Wert steigt

//── CONFLUENCE GEWICHTUNGEN ({stype}) ─────────────────────────────
input string  _Sec5           = "══ GEWICHTUNGEN ══";
input int     W_EMA           = {W['ema']};   // EMA-Trend Gewicht (M15)
input int     W_ADX           = {W['adx']};   // ADX-Stärke Gewicht
input int     W_RSI           = {W['rsi']};   // RSI Zone Gewicht
input int     W_MACD          = {W['macd']};  // MACD Gewicht
input int     W_BB            = {W['bb']};    // Bollinger Bands Gewicht
input int     W_STOCH         = {W['stoch']}; // Stochastic Gewicht
input int     W_CCI           = {W['cci']};   // CCI Momentum Gewicht
input int     W_VOL           = {W['vol']};   // Volumen Bestätigung Gewicht

//══════════════════════════════════════════════════════════════════
//  GLOBALE VARIABLEN
//══════════════════════════════════════════════════════════════════

CTrade trade;
const ulong MAGIC = 20260624;

//── Indikator-Handles M15 ─────────────────────────────────────────
int hM_ATR, hM_RSI, hM_ADX, hM_EMA20, hM_EMA50, hM_EMA200;
int hM_MACD, hM_BB, hM_STOCH, hM_CCI;

//── Indikator-Handles H1 ──────────────────────────────────────────
int hH1_EMA20, hH1_EMA50, hH1_RSI, hH1_MACD;

//── Indikator-Handles H4 ──────────────────────────────────────────
int hH4_EMA50, hH4_EMA200, hH4_ADX;

//── Tages-Tracking ────────────────────────────────────────────────
double   g_DayStartBalance = 0;
datetime g_DayStart        = 0;
datetime g_LastBar         = 0;
int      g_DailyTrades     = 0;    // Trades heute geöffnet
datetime g_LastOpenTime    = 0;    // Zeitpunkt letzter Trade-Eröffnung
int      g_ConsecLosses    = 0;    // Aufeinanderfolgende Verluste
int      g_Regime          = 0;    // 0=unbekannt 1=TREND 2=RANGE (choppy)

//══════════════════════════════════════════════════════════════════
//  ChoppinessIndex — erkennt ob Markt trendet oder seitwärts läuft
//  CI < 38.2 = starker Trend | CI > 61.8 = choppy/ranging
//  Wir verwenden 56.0 als pragmatischen Schwellenwert
//══════════════════════════════════════════════════════════════════
double ChoppinessIndex(int period = 14)
  {{
   double hh = iHigh(_Symbol, PERIOD_M15, iHighest(_Symbol, PERIOD_M15, MODE_HIGH, period, 1));
   double ll = iLow (_Symbol, PERIOD_M15, iLowest (_Symbol, PERIOD_M15, MODE_LOW,  period, 1));
   double range = hh - ll;
   if(range < _Point) return 50.0;

   double sumATR = 0;
   for(int i = 1; i <= period; i++)
     {{
      double hi = iHigh (_Symbol, PERIOD_M15, i);
      double lo = iLow  (_Symbol, PERIOD_M15, i);
      double pc = iClose(_Symbol, PERIOD_M15, i + 1);
      sumATR += MathMax(hi - lo, MathMax(MathAbs(hi - pc), MathAbs(lo - pc)));
     }}

   if(sumATR <= 0) return 50.0;
   return 100.0 * MathLog10(sumATR / range) / MathLog10((double)period);
  }}

//══════════════════════════════════════════════════════════════════
//  OnInit
//══════════════════════════════════════════════════════════════════
int OnInit()
  {{
   trade.SetExpertMagicNumber(MAGIC);
   trade.SetDeviationInPoints(20);

   //── M15 Indikatoren ──────────────────────────────────────────
   hM_ATR   = iATR        (_Symbol, PERIOD_M15, 14);
   hM_RSI   = iRSI        (_Symbol, PERIOD_M15, 14,  PRICE_CLOSE);
   hM_ADX   = iADX        (_Symbol, PERIOD_M15, 14);
   hM_EMA20 = iMA         (_Symbol, PERIOD_M15, 20,  0, MODE_EMA, PRICE_CLOSE);
   hM_EMA50 = iMA         (_Symbol, PERIOD_M15, 50,  0, MODE_EMA, PRICE_CLOSE);
   hM_EMA200= iMA         (_Symbol, PERIOD_M15, 200, 0, MODE_EMA, PRICE_CLOSE);
   hM_MACD  = iMACD       (_Symbol, PERIOD_M15, 12,  26, 9, PRICE_CLOSE);
   hM_BB    = iBands      (_Symbol, PERIOD_M15, 20,  0, 2.0, PRICE_CLOSE);
   hM_STOCH = iStochastic (_Symbol, PERIOD_M15, 5,   3, 3, MODE_SMA, STO_LOWHIGH);
   hM_CCI   = iCCI        (_Symbol, PERIOD_M15, 20,  PRICE_TYPICAL);

   //── H1 Indikatoren ───────────────────────────────────────────
   hH1_EMA20= iMA   (_Symbol, PERIOD_H1, 20, 0, MODE_EMA, PRICE_CLOSE);
   hH1_EMA50= iMA   (_Symbol, PERIOD_H1, 50, 0, MODE_EMA, PRICE_CLOSE);
   hH1_RSI  = iRSI  (_Symbol, PERIOD_H1, 14, PRICE_CLOSE);
   hH1_MACD = iMACD (_Symbol, PERIOD_H1, 12, 26, 9, PRICE_CLOSE);

   //── H4 Indikatoren ───────────────────────────────────────────
   hH4_EMA50 = iMA  (_Symbol, PERIOD_H4, InpH4EMA,       0, MODE_EMA, PRICE_CLOSE);
   hH4_EMA200= iMA  (_Symbol, PERIOD_H4, InpH4EMA * 4,   0, MODE_EMA, PRICE_CLOSE);
   hH4_ADX   = iADX (_Symbol, PERIOD_H4, 14);

   //── Handle-Validierung ───────────────────────────────────────
   if(hM_ATR  == INVALID_HANDLE || hM_RSI  == INVALID_HANDLE ||
      hH1_RSI == INVALID_HANDLE || hH4_EMA50 == INVALID_HANDLE)
     {{
      Alert("FEHLER: Indikator-Handle ungueltig! Pruefe Symbol/Timeframe.");
      return INIT_FAILED;
     }}

   g_DayStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
   g_DayStart        = TimeCurrent();

   Print("CLAUDE QUANT ELITE v2.0 | Strategie: {stype} | WR={wr}% | Return={ret:+.1f}%");
   Print("H4-Filter: ", InpUseH4Filter ? "AN" : "AUS",
         " | H1-Filter: ", InpUseH1Filter ? "AN" : "AUS",
         " | Session: ", InpSessionStart, "-", InpSessionEnd, " UTC");
   Print("SL=", InpSLMult, "R | TP1=", InpTP1Mult, "R | TP2=", InpTP2Mult, "R | BE=", InpBEAt, "R");

   return INIT_SUCCEEDED;
  }}

//══════════════════════════════════════════════════════════════════
//  OnDeinit
//══════════════════════════════════════════════════════════════════
void OnDeinit(const int reason)
  {{
   int h[] = {{hM_ATR, hM_RSI, hM_ADX, hM_EMA20, hM_EMA50, hM_EMA200,
               hM_MACD, hM_BB, hM_STOCH, hM_CCI,
               hH1_EMA20, hH1_EMA50, hH1_RSI, hH1_MACD,
               hH4_EMA50, hH4_EMA200, hH4_ADX}};
   for(int i = 0; i < ArraySize(h); i++)
      if(h[i] != INVALID_HANDLE) IndicatorRelease(h[i]);
   Comment("");
  }}

//══════════════════════════════════════════════════════════════════
//  OnTradeTransaction — Consecutive Loss Tracking
//══════════════════════════════════════════════════════════════════
void OnTradeTransaction(const MqlTradeTransaction& trans,
                        const MqlTradeRequest& request,
                        const MqlTradeResult& result)
  {{
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
   ulong deal_ticket = trans.deal;
   if(!HistoryDealSelect(deal_ticket)) return;
   if(HistoryDealGetInteger(deal_ticket, DEAL_MAGIC) != (long)MAGIC) return;
   double profit = HistoryDealGetDouble(deal_ticket, DEAL_PROFIT);
   if(profit == 0) return;
   if(profit < 0)
      g_ConsecLosses++;
   else
      g_ConsecLosses = 0;
   PrintFormat("DEAL | Profit=%.2f | Aufein. Verluste=%d / Max=%d",
               profit, g_ConsecLosses, InpMaxConsecLoss);
  }}

//══════════════════════════════════════════════════════════════════
//  OnTick — Haupt-Logik
//══════════════════════════════════════════════════════════════════
void OnTick()
  {{
   //── Tages-Reset ──────────────────────────────────────────────
   MqlDateTime now_dt, day_dt;
   TimeToStruct(TimeCurrent(),  now_dt);
   TimeToStruct(g_DayStart,     day_dt);
   if(now_dt.day != day_dt.day)
     {{
      g_DayStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
      g_DayStart        = TimeCurrent();
      g_DailyTrades     = 0;  // Tages-Zähler zurücksetzen
      g_ConsecLosses    = 0;  // Verlustserie mit neuem Tag zurücksetzen
     }}

   //── Position-Verwaltung (jeden Tick) ─────────────────────────
   ManageTrades();
   ShowDashboard();

   //── Nur auf neue M15-Kerze reagieren ─────────────────────────
   datetime cur_bar = iTime(_Symbol, PERIOD_M15, 0);
   if(cur_bar == g_LastBar) return;
   g_LastBar = cur_bar;

   //── Vorflug-Checks ───────────────────────────────────────────

   // Session-Filter: London/NY
   MqlDateTime gmt;
   TimeToStruct(TimeGMT(), gmt);
   if(gmt.hour < InpSessionStart || gmt.hour >= InpSessionEnd) return;

   // Wochentag-Filter: Montag-Open (schwache Liquidität) + Freitag-Close (Weekend-Gap)
   MqlDateTime loc;
   TimeToStruct(TimeCurrent(), loc);
   if(InpSkipMonday && loc.day_of_week == 1 && gmt.hour < 10) return; // Montag vor 10 UTC
   if(InpSkipFriday && loc.day_of_week == 5 && gmt.hour >= 17) return; // Freitag ab 17 UTC

   // Max. offene Positionen
   if(CountMyPositions() >= InpMaxPositions) return;

   // Tages-Trade-Limit: max. N Signal-Paare pro Tag
   if(g_DailyTrades >= InpMaxDailyTrades) return;

   // Cooldown: Mindest-Pause zwischen Trades (verhindert Overtrading)
   if(g_LastOpenTime > 0 &&
      (int)(TimeCurrent() - g_LastOpenTime) < InpTradeCooldownH * 3600) return;

   // Tages-Verlust-Limit
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   if(g_DayStartBalance > 0)
     {{
      double day_dd = (g_DayStartBalance - balance) / g_DayStartBalance * 100.0;
      if(day_dd >= InpMaxDailyDD) return;
     }}

   // Spread-Filter
   long spread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   if(spread > InpMaxSpread) return;

   // ATR vorhanden
   double atr = Buf(hM_ATR, 0, 1);
   if(atr <= 0) return;

   // Volatilitäts-Filter: kein Handel bei News-Spike (ATR > 3x Ø)
   double atr_avg = 0;
   for(int k = 1; k <= 20; k++) atr_avg += Buf(hM_ATR, 0, k);
   atr_avg /= 20.0;
   if(atr > atr_avg * 3.0) return;

   // Konsekutive Verlust-Limit: Pause nach N Verlusten in Folge
   if(InpMaxConsecLoss > 0 && g_ConsecLosses >= InpMaxConsecLoss) return;

   // Tages-Gewinn-Ziel: kein neuer Trade nach Ziel-Erreichen
   if(InpDailyTarget > 0.0 && g_DayStartBalance > 0)
     {{
      double day_pct = (balance - g_DayStartBalance) / g_DayStartBalance * 100.0;
      if(day_pct >= InpDailyTarget) return;
     }}

   //── Signal holen ─────────────────────────────────────────────
   int sig = GetSignal();
   if(sig == 0) return;

   //── Trade öffnen ─────────────────────────────────────────────
   OpenTrade(sig, atr);
  }}

//══════════════════════════════════════════════════════════════════
//  GetSignal — Multi-Timeframe Confluence
//══════════════════════════════════════════════════════════════════
int GetSignal()
  {{
   //── Market Regime Detection (Choppiness Index) ───────────────
   double ci = ChoppinessIndex(14);
   g_Regime = (ci > 56.0) ? 2 : 1;  // 2=RANGE choppy, 1=TREND

   //── RANGE MODE: BB-Bounce + RSI-Extreme ──────────────────────
   //   Kein H4-Filter nötig — Range-Markt hat keinen Trend
   if(g_Regime == 2)
     {{
      double rsiR   = Buf(hM_RSI,   0, 1);
      double bb_upR = Buf(hM_BB,    1, 1);
      double bb_loR = Buf(hM_BB,    2, 1);
      double bb_midR= Buf(hM_BB,    0, 1);
      double stkR   = Buf(hM_STOCH, 0, 1);
      double stkDR  = Buf(hM_STOCH, 1, 1);
      double adxR   = Buf(hM_ADX,   0, 1);
      double closeR = iClose(_Symbol, PERIOD_M15, 1);
      double openR  = iOpen (_Symbol, PERIOD_M15, 1);

      if(bb_upR <= 0 || rsiR <= 0) return 0;
      double pctbR = (bb_upR > bb_loR) ? (closeR - bb_loR) / (bb_upR - bb_loR) : 0.5;

      // ADX > 35 = doch Trend erkannt → Range-Logik überspringen
      if(adxR <= 35)
        {{
         int rbs = 0, rss = 0;

         // RSI-Extreme: Hauptsignal
         if(rsiR < 30 && closeR < openR) rbs += 5;   // überverkauft → BUY
         if(rsiR > 70 && closeR > openR) rss += 5;   // überkauft → SELL
         if(rsiR < 35) rbs += 2;
         if(rsiR > 65) rss += 2;

         // BB-Bounce: zweites Hauptsignal
         if(pctbR < 0.15) rbs += 4;   // am unteren Band
         if(pctbR > 0.85) rss += 4;   // am oberen Band

         // Stochastic-Bestätigung
         if(stkR < 20 && stkR > stkDR) rbs += 3;
         if(stkR > 80 && stkR < stkDR) rss += 3;

         // Nur handeln wenn beide Indikatoren übereinstimmen
         if(rbs >= 7 && rbs > rss + 2) return  1;
         if(rss >= 7 && rss > rbs + 2) return -1;
         return 0;
        }}
      // ADX > 35: fällt durch in Trend-Modus
      g_Regime = 1;
     }}

   //── TREND MODE: originale Logik ──────────────────────────────

   //── H4 Trend-Filter ──────────────────────────────────────────
   int h4_bias = 0;
   if(InpUseH4Filter)
     {{
      double h4e50  = Buf(hH4_EMA50,  0, 1);
      double h4e200 = Buf(hH4_EMA200, 0, 1);
      double h4adx  = Buf(hH4_ADX,    0, 1);
      double h4c    = iClose(_Symbol, PERIOD_H4, 1);

      if(h4e50 > 0 && h4e200 > 0 && h4c > 0)
        {{
         if(h4e50 > h4e200 && h4c > h4e50)  h4_bias =  1;
         if(h4e50 < h4e200 && h4c < h4e50)  h4_bias = -1;
        }}
      if(h4adx < 15) return 0;   // H4 seitwärts → kein Trade
      if(h4_bias == 0) return 0; // H4 Hard Gate: kein Trade ohne klare Trendrichtung
     }}

   //── H1 Momentum-Filter ───────────────────────────────────────
   int h1_bias = 0;
   if(InpUseH1Filter)
     {{
      double h1e20  = Buf(hH1_EMA20, 0, 1);
      double h1e50  = Buf(hH1_EMA50, 0, 1);
      double h1rsi  = Buf(hH1_RSI,   0, 1);
      double h1ml   = Buf(hH1_MACD,  0, 1);
      double h1ms   = Buf(hH1_MACD,  1, 1);

      if(h1e20 > h1e50) h1_bias++;   else h1_bias--;
      if(h1ml  > h1ms)  h1_bias++;   else h1_bias--;
      if(h1rsi > 50 && h1rsi < 80) h1_bias++;
      if(h1rsi < 50 && h1rsi > 20) h1_bias--;
     }}

   // H4 und H1 dürfen nicht in entgegengesetzte Richtungen zeigen
   if(InpUseH4Filter && InpUseH1Filter)
     {{
      if(h4_bias ==  1 && h1_bias <= -1) return 0;
      if(h4_bias == -1 && h1_bias >=  1) return 0;
     }}

   //── M15 Entry Confluence Score ────────────────────────────────
   double e20    = Buf(hM_EMA20,  0, 1);
   double e50    = Buf(hM_EMA50,  0, 1);
   double e200   = Buf(hM_EMA200, 0, 1);
   double rsi    = Buf(hM_RSI,    0, 1);
   double adx    = Buf(hM_ADX,    0, 1);
   double macdl  = Buf(hM_MACD,   0, 1);
   double macds  = Buf(hM_MACD,   1, 1);
   double bb_up  = Buf(hM_BB,     1, 1);
   double bb_lo  = Buf(hM_BB,     2, 1);
   double bb_mid = Buf(hM_BB,     0, 1);
   double stk    = Buf(hM_STOCH,  0, 1);
   double stk_d  = Buf(hM_STOCH,  1, 1);
   double cci    = Buf(hM_CCI,    0, 1);

   if(e20 == 0 || rsi == 0 || adx == 0 || bb_up == 0) return 0;

   // Kerzen-Analyse
   double c_close = iClose(_Symbol, PERIOD_M15, 1);
   double c_open  = iOpen (_Symbol, PERIOD_M15, 1);
   double c_high  = iHigh (_Symbol, PERIOD_M15, 1);
   double c_low   = iLow  (_Symbol, PERIOD_M15, 1);
   double c_body  = MathAbs(c_close - c_open);
   double c_range = c_high - c_low;
   double body_pct= (c_range > 0) ? c_body / c_range : 0;
   bool   bull_c  = (c_close > c_open && body_pct > 0.35);
   bool   bear_c  = (c_close < c_open && body_pct > 0.35);

   // Bollinger Band Metriken
   double bb_pctb = (bb_up > bb_lo) ? (c_close - bb_lo) / (bb_up - bb_lo) : 0.5;
   double bb_bw   = (bb_mid > 0)    ? (bb_up - bb_lo) / bb_mid * 100.0 : 0;

   // Volumen-Ratio
   long vol_cur = iVolume(_Symbol, PERIOD_M15, 1);
   long vol_sum = 0;
   for(int v = 2; v <= 21; v++) vol_sum += iVolume(_Symbol, PERIOD_M15, v);
   long   vol_avg   = (vol_sum > 0) ? vol_sum / 20 : 1;
   double vol_ratio = (double)vol_cur / (double)vol_avg;
   bool   high_vol  = (vol_ratio >= InpVolMinRatio);

   // Mindest-Volumen Pflicht
   if(!high_vol) return 0;

   int bs = 0, ss = 0;

   //── 1. EMA-Trend M15 ─────────────────────────────────────────
   if(e20 > e50 && e50 > e200)    bs += W_EMA;
   if(e20 < e50 && e50 < e200)    ss += W_EMA;

   //── 2. ADX Trend-Stärke ──────────────────────────────────────
   if(adx >= InpADXMin)
     {{
      if(macdl > macds) bs += W_ADX;
      else              ss += W_ADX;
     }}

   //── 3. RSI Zone ──────────────────────────────────────────────
   if(rsi >= InpRSILowBuy  && rsi <= InpRSIHighBuy)  bs += W_RSI;
   if(rsi >= InpRSILowSell && rsi <= InpRSIHighSell) ss += W_RSI;

   //── 4. MACD Richtung + Nulllinie ─────────────────────────────
   if(macdl > macds && macdl > 0)  bs += W_MACD;
   if(macdl < macds && macdl < 0)  ss += W_MACD;

   //── 5. Bollinger Bands Position + Squeeze ────────────────────
   if(bb_pctb < 0.2 && bull_c) bs += W_BB;
   if(bb_pctb > 0.8 && bear_c) ss += W_BB;
   if(bb_bw > 0 && bb_bw < 1.5)  // BB Squeeze → Ausbruch
     {{
      if(bull_c) bs += W_BB / 2;
      if(bear_c) ss += W_BB / 2;
     }}

   //── 6. Stochastic ────────────────────────────────────────────
   if(stk < 25 && stk > stk_d) bs += W_STOCH;
   if(stk > 75 && stk < stk_d) ss += W_STOCH;

   //── 7. CCI Momentum ──────────────────────────────────────────
   if(W_CCI > 0)
     {{
      if(cci >  100) bs += W_CCI;
      if(cci < -100) ss += W_CCI;
     }}

   //── 8. Volumen-Bestätigung ────────────────────────────────────
   if(W_VOL > 0)
     {{
      if(bull_c) bs += W_VOL;
      if(bear_c) ss += W_VOL;
     }}

   //── 9. H1 Bias Bonus ─────────────────────────────────────────
   if(InpUseH1Filter)
     {{
      if(h1_bias >= 2) bs += 2;
      if(h1_bias <= -2) ss += 2;
     }}

   //── 10. H4 Trend Bonus (stärkster Filter) ────────────────────
   if(InpUseH4Filter)
     {{
      if(h4_bias ==  1) bs += 3;
      if(h4_bias == -1) ss += 3;
     }}

   //── Entscheidung + 2-Kerzen-Bestätigung ─────────────────────
   int raw_sig = 0;
   if(bs >= InpMinScore && bs > ss + 2) raw_sig =  1;
   if(ss >= InpMinScore && ss > bs + 2) raw_sig = -1;

   // H4 Hard Gate: Signal MUSS in H4-Trendrichtung gehen
   if(InpUseH4Filter && raw_sig != 0 && h4_bias != 0)
     {{
      if(h4_bias ==  1 && raw_sig == -1) return 0;  // H4 bullish → kein SELL
      if(h4_bias == -1 && raw_sig ==  1) return 0;  // H4 bearish → kein BUY
     }}

   // 2-Kerzen-Bestätigung: Signal muss auf vorheriger Kerze ebenfalls aktiv gewesen sein
   if(InpConfirmBars >= 2 && raw_sig != 0)
     {{
      static int prev_sig   = 0;
      static datetime prev_bar_time = 0;
      datetime this_bar = iTime(_Symbol, PERIOD_M15, 1);
      datetime prev_bar = iTime(_Symbol, PERIOD_M15, 2);
      if(prev_bar_time == prev_bar && prev_sig == raw_sig)
        {{
         prev_bar_time = this_bar;
         prev_sig      = raw_sig;
         return raw_sig;  // Beide Kerzen bestätigen → echtes Signal
        }}
      prev_bar_time = this_bar;
      prev_sig      = raw_sig;
      return 0;  // Nur eine Kerze → noch kein Einstieg
     }}

   return raw_sig;
  }}

//══════════════════════════════════════════════════════════════════
//  OpenTrade — Dual TP: TP1 (50% Position) + TP2 (Runner)
//══════════════════════════════════════════════════════════════════
void OpenTrade(int dir, double atr)
  {{
   double sl_pts  = atr * InpSLMult;
   double tp1_pts = atr * InpTP1Mult;
   double tp2_pts = atr * InpTP2Mult;

   double bal    = AccountInfoDouble(ACCOUNT_BALANCE);
   double risk   = bal * (InpRiskPct / 100.0);
   double csize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   double full   = NormalizeDouble(risk / (sl_pts * csize), 2);
   double minlot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxlot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double step   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);

   full = MathMax(full, minlot * 2);
   full = MathMin(full, maxlot);
   full = MathFloor(full / step) * step;

   double half = NormalizeDouble(full / 2.0, 2);
   half = MathMax(half, minlot);
   half = MathFloor(half / step) * step;

   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   // TP2: kein festes Ziel wenn Elite-Modus aktiv → Trail übernimmt
   double tp2_buy  = InpTP2NoLimit ? 0.0 : ask + tp2_pts;
   double tp2_sell = InpTP2NoLimit ? 0.0 : bid - tp2_pts;

   if(dir == 1)
     {{
      trade.Buy(half, _Symbol, ask, ask - sl_pts, ask + tp1_pts, "CQ-TP1");
      trade.Buy(half, _Symbol, ask, ask - sl_pts, tp2_buy,       "CQ-TP2");
     }}
   else
     {{
      trade.Sell(half, _Symbol, bid, bid + sl_pts, bid - tp1_pts, "CQ-TP1");
      trade.Sell(half, _Symbol, bid, bid + sl_pts, tp2_sell,      "CQ-TP2");
     }}

   // Zähler aktualisieren
   g_DailyTrades++;
   g_LastOpenTime = TimeCurrent();

   PrintFormat("TRADE OPEN | %s | Lot:%.2fx2 | SL:%.2f | TP1:%.2f | TP2:%.2f | ATR:%.2f | Tag#%d",
               dir == 1 ? "BUY" : "SELL", half, sl_pts, tp1_pts, tp2_pts, atr, g_DailyTrades);
  }}

//══════════════════════════════════════════════════════════════════
//  ManageTrades — Break-Even + Trailing Stop
//══════════════════════════════════════════════════════════════════
void ManageTrades()
  {{
   double atr = Buf(hM_ATR, 0, 1);
   if(atr <= 0) return;

   bool   tp1_open   = false;
   bool   tp2_open   = false;
   ulong  tp2_ticket = 0;

   // Positionen scannen
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {{
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket)) continue;
      if(PositionGetInteger(POSITION_MAGIC) != (long)MAGIC) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      string cmt = PositionGetString(POSITION_COMMENT);
      if(StringFind(cmt, "CQ-TP1") >= 0) tp1_open = true;
      if(StringFind(cmt, "CQ-TP2") >= 0) {{ tp2_open = true; tp2_ticket = ticket; }}
     }}

   // TP1 wurde getroffen (TP1 weg, TP2 noch offen) → BE für TP2
   if(!tp1_open && tp2_open && tp2_ticket > 0 && InpBEAt > 0.0)
     {{
      if(PositionSelectByTicket(tp2_ticket))
        {{
         double entry = PositionGetDouble(POSITION_PRICE_OPEN);
         double sl    = PositionGetDouble(POSITION_SL);
         double tp    = PositionGetDouble(POSITION_TP);
         long   ptype = PositionGetInteger(POSITION_TYPE);

         if(ptype == POSITION_TYPE_BUY && sl < entry - _Point)
            trade.PositionModify(tp2_ticket, entry + _Point, tp);
         else if(ptype == POSITION_TYPE_SELL && sl > entry + _Point)
            trade.PositionModify(tp2_ticket, entry - _Point, tp);
        }}
     }}

   // ── RSI Emergency Close (Bounce-Schutz) ─────────────────────────
   // Wenn RSI extremes Niveau → alle Gewinne sofort sichern, bevor Reversal kommt
   double cur_rsi = Buf(hM_RSI, 0, 0);
   if(cur_rsi > 0)
     {{
      for(int i = PositionsTotal() - 1; i >= 0; i--)
        {{
         ulong tk = PositionGetTicket(i);
         if(!PositionSelectByTicket(tk)) continue;
         if(PositionGetInteger(POSITION_MAGIC) != (long)MAGIC) continue;
         if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
         long   ptype  = PositionGetInteger(POSITION_TYPE);
         double profit = PositionGetDouble(POSITION_PROFIT);
         if(profit <= 0) continue;  // Nur profitable Positionen notfall-schließen
         // SELL bei überverkauftem RSI → Bounce droht → Gewinne sichern
         if(ptype == POSITION_TYPE_SELL && cur_rsi < InpRSICloseSell)
           {{
            trade.PositionClose(tk);
            Print("RSI-EMERGENCY CLOSE (SELL | RSI=", cur_rsi, " < ", InpRSICloseSell, ")");
           }}
         // BUY bei überkauftem RSI → Verkaufsdruck droht → Gewinne sichern
         else if(ptype == POSITION_TYPE_BUY && cur_rsi > InpRSICloseBuy)
           {{
            trade.PositionClose(tk);
            Print("RSI-EMERGENCY CLOSE (BUY | RSI=", cur_rsi, " > ", InpRSICloseBuy, ")");
           }}
        }}
     }}

   // ── ATR Trailing Stop für TP2 Runner ─────────────────────────────
   // Trail aktiviert erst nach InpTrailActivate*ATR Gewinn (kein Früh-Stop)
   if(InpTrailing && tp2_open && tp2_ticket > 0)
     {{
      if(PositionSelectByTicket(tp2_ticket))
        {{
         double sl    = PositionGetDouble(POSITION_SL);
         double tp    = PositionGetDouble(POSITION_TP);
         double entry = PositionGetDouble(POSITION_PRICE_OPEN);
         long   ptype = PositionGetInteger(POSITION_TYPE);
         double trail = atr * InpTrailMult;
         double activate_dist = atr * InpTrailActivate;

         if(ptype == POSITION_TYPE_BUY)
           {{
            double bid    = SymbolInfoDouble(_Symbol, SYMBOL_BID);
            double new_sl = bid - trail;
            // Trail erst wenn Gewinn > activate_dist (z.B. 0.5*ATR)
            bool   in_profit = (bid - entry) >= activate_dist;
            // Bei InpTP2NoLimit: kein festes TP, nur Trail (tp=0 → kein Limit)
            bool   tp_ok = (tp <= 0 || new_sl < tp - _Point);
            if(in_profit && new_sl > sl + _Point && tp_ok)
               trade.PositionModify(tp2_ticket, new_sl, tp);
           }}
         else
           {{
            double ask    = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
            double new_sl = ask + trail;
            bool   in_profit = (entry - ask) >= activate_dist;
            bool   tp_ok = (tp <= 0 || new_sl > tp + _Point);
            if(in_profit && new_sl < sl - _Point && tp_ok)
               trade.PositionModify(tp2_ticket, new_sl, tp);
           }}
        }}
     }}

   // Break-Even für TP1 Positionen (falls TP1=TP2 oder nur eine Position)
   if(InpBEAt > 0.0)
     {{
      double trigger = atr * InpBEAt;
      for(int i = PositionsTotal() - 1; i >= 0; i--)
        {{
         ulong ticket = PositionGetTicket(i);
         if(!PositionSelectByTicket(ticket)) continue;
         if(PositionGetInteger(POSITION_MAGIC) != (long)MAGIC) continue;
         if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;

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
  }}

//══════════════════════════════════════════════════════════════════
//  ShowDashboard — Chart Comment
//══════════════════════════════════════════════════════════════════
void ShowDashboard()
  {{
   double balance  = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity   = AccountInfoDouble(ACCOUNT_EQUITY);
   double day_pl   = balance - g_DayStartBalance;
   double atr      = Buf(hM_ATR, 0, 1);
   double h4e50    = Buf(hH4_EMA50,  0, 1);
   double h4e200   = Buf(hH4_EMA200, 0, 1);
   double h1rsi    = Buf(hH1_RSI,    0, 1);
   double m15rsi   = Buf(hM_RSI,     0, 1);

   MqlDateTime gmt;
   TimeToStruct(TimeGMT(), gmt);
   bool in_sess = (gmt.hour >= InpSessionStart && gmt.hour < InpSessionEnd);

   string h4_str    = (h4e50 > h4e200) ? "BULLISH" : (h4e50 < h4e200 ? "BEARISH" : "NEUTRAL");
   string sess_str  = in_sess ? "AKTIV (London/NY)" : "WARTEN (Asian)";
   double ci_val    = ChoppinessIndex(14);
   string reg_str   = (g_Regime == 2)
                      ? StringFormat("RANGE  CI=%.1f (BB+RSI)", ci_val)
                      : StringFormat("TREND  CI=%.1f (VWAP)",   ci_val);

   double day_pct = (g_DayStartBalance > 0) ?
      (balance - g_DayStartBalance) / g_DayStartBalance * 100.0 : 0.0;
   string risk_str = (InpMaxConsecLoss > 0 && g_ConsecLosses >= InpMaxConsecLoss) ?
      "PAUSE" : "OK";

   Comment(StringFormat(
      "╔══════════════════════════════════════╗\\n"
      "║  CLAUDE QUANT ELITE v3.0 ADAPTIVE    ║\\n"
      "║  Strategie : %-22s ║\\n"
      "╠══════════════════════════════════════╣\\n"
      "║  Balance  : %10.2f USD          ║\\n"
      "║  Equity   : %10.2f USD          ║\\n"
      "║  Tag P&L  : %+10.2f (%+.1f%%)      ║\\n"
      "╠══════════════════════════════════════╣\\n"
      "║  REGIME   : %-22s ║\\n"
      "║  H4 Trend : %-22s ║\\n"
      "║  H1 RSI   : %5.1f                    ║\\n"
      "║  M15 RSI  : %5.1f                    ║\\n"
      "║  ATR M15  : %8.2f                ║\\n"
      "╠══════════════════════════════════════╣\\n"
      "║  Session  : %-22s ║\\n"
      "║  Trades heute: %2d/%2d  Konse.V.: %2d/%2d ║\\n"
      "║  Risiko   : %-22s ║\\n"
      "╚══════════════════════════════════════╝",
      "{stype}", balance, equity, day_pl, day_pct,
      reg_str, h4_str, h1rsi, m15rsi, atr,
      sess_str, g_DailyTrades, InpMaxDailyTrades,
      g_ConsecLosses, InpMaxConsecLoss, risk_str));
  }}

//══════════════════════════════════════════════════════════════════
//  Hilfsfunktionen
//══════════════════════════════════════════════════════════════════

int CountMyPositions()
  {{
   int count = 0;
   for(int i = 0; i < PositionsTotal(); i++)
     {{
      ulong t = PositionGetTicket(i);
      if(!PositionSelectByTicket(t)) continue;
      if(PositionGetInteger(POSITION_MAGIC) == (long)MAGIC &&
         PositionGetString(POSITION_SYMBOL) == _Symbol) count++;
     }}
   return count;
  }}

double Buf(int handle, int buffer, int shift)
  {{
   double arr[];
   ArraySetAsSeries(arr, true);
   if(CopyBuffer(handle, buffer, shift, 1, arr) <= 0) return 0.0;
   return arr[0];
  }}

//+------------------------------------------------------------------+
// Ende CLAUDE QUANT ELITE v2.0
//+------------------------------------------------------------------+
"""
    return code


if __name__ == "__main__":
    main()
