"""
CLAUDE + QUANT Trading Bot  v3.0  —  Full Feature Build
=======================================================
MODES (umschaltbar per Dashboard oder API):
  SELF-LEARN  : ATR-basiertes Sizing + adaptive Schwellenwerte (lernt!)
  KELLY       : Kelly Criterion Lot-Groesse (optimal basierend auf Win-Rate)
  RANDOM      : Zufaelliges Sizing 0.25-1.75% (nur fuer Tests)

SELF-LEARNING:
  - Verfolgt Ergebnis jedes Candlestick-Patterns (Hammer, Engulfing, ...)
  - Nach je 10 Trades: passt ADX-Schwelle + RSI-Bereich automatisch an
  - Win-Rate < 38% -> Filter strenger (ADX hoch, RSI-Bereich enger)
  - Win-Rate > 65% -> Filter lockerer (ADX runter, mehr Trades)
  - Schlechteste Patterns werden ignoriert (WR < 30% nach 5+ Trades)

KELLY CRITERION:
  - f* = (p*b - q) / b  (p=Win-Rate, b=Win/Loss-Ratio, q=1-p)
  - Maximum 5% pro Trade fuer Sicherheit
  - Passt sich automatisch an aktuelle Performance an

API ENDPOINTS:
  GET /data        -> Alle Bot-Daten als JSON
  GET /mode?set=X  -> Wechsle Modus (RANDOM | KELLY | SELF-LEARN)
"""

import os
import time
import json
import math
import random as _random
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    print("[WARN] MetaTrader5 nicht gefunden. pip install MetaTrader5")

# ── CONFIG ────────────────────────────────────────────────────────────────────
SYMBOL       = "XAUUSD"    # Gold (XAU/USD)
TIMEFRAME    = 16385       # mt5.TIMEFRAME_M15
RISK_PCT     = 1.0         # % des Kontos (SELF-LEARN Modus)
ATR_SL_MULT    = 1.5
ATR_TP_MULT    = 2.5         # Enger TP → höhere Win Rate
BREAK_EVEN_AT  = 0.0         # 0 = deaktiviert (TP1-Teilschluss schützt automatisch)
EMA_FAST     = 20
EMA_MID      = 50
EMA_SLOW     = 200
MACD_FAST    = 12
MACD_SLOW    = 26
MACD_SIGNAL  = 9
RSI_PERIOD   = 14
ADX_PERIOD   = 14
ADX_MIN      = 25
ATR_PERIOD   = 14
MAGIC        = 234567
CHECK_EVERY  = 15
SERVER_PORT  = 5000
LEARN_EVERY  = 10          # Trades zwischen Anpassungen
TIMEFRAME_H4 = 16388       # mt5.TIMEFRAME_H4
SESSION_START = 7          # UTC-Stunde: London Open
SESSION_END   = 17         # UTC-Stunde: NY Close

# ── ELITE RISK MANAGEMENT ─────────────────────────────────────────────────────
MAX_CONSEC_LOSSES    = 3     # Pause nach N aufeinanderfolgenden Verlusten
DAILY_PROFIT_TARGET  = 2.0   # Stop bei +2% Tagesgewinn (0 = deaktiviert)
TRAIL_ACTIVATE_ATR   = 1.0   # ATR Trail aktiviert nach X×ATR Profit (nach TP1 = mehr Luft)
TRAIL_DIST_ATR       = 2.0   # Trailing-Abstand in ATR-Einheiten (TP2-Runner laeuft weiter)
RSI_CLOSE_SELL       = 20    # SELL-Positionen schließen wenn RSI < X (Bounce-Schutz)
RSI_CLOSE_BUY        = 80    # BUY-Positionen schließen wenn RSI > X (Reversal-Schutz)

# Contract sizes fuer Lot-Berechnung (Fallback wenn MT5 nicht verfuegbar)
CONTRACT_SIZES = {
    "XAUUSD": 100,         # 1 Lot = 100 Unzen Gold
    "XAGUSD": 5000,
    "EURUSD": 100000,
    "GBPUSD": 100000,
    "USDJPY": 100000,
    "GBPJPY": 100000,
    "BTCUSD": 1,
}
# ─────────────────────────────────────────────────────────────────────────────


# ── LEARNING STATE ────────────────────────────────────────────────────────────
_learn = {
    "mode":            "SELF-LEARN",
    "adx_threshold":   ADX_MIN,
    "rsi_low_buy":     40,
    "rsi_high_buy":    65,
    "rsi_low_sell":    35,
    "rsi_high_sell":   60,
    "min_score":       8,          # Confluence-Punkte (optimierbar)
    "strategy_type":   "BALANCED", # Strategie-Typ (optimierbar)
    "adjustments":     0,
    "last_adjust":     "Noch keine Anpassung",
    "recent_wr":       0.0,
    "recent_trades":   0,
    "kelly_f":         0.0,
    "blocked_patterns": [],
    "pattern_stats":   {},
    "strategy_perf":   {},         # {type: {trades,wins,wr}} — Strategie-Performance
    "best_strategy":   "ENSEMBLE", # Auto-gewählte beste Strategie
}
_last_trade_count = 0

_risk_state = {
    "daily_start_balance": 0.0,
    "daily_date":          "",
    "consec_losses":       0,
}


def load_best_params():
    """Laedt optimierte Parameter aus best_params.json (falls vorhanden)."""
    try:
        import os
        path = os.path.join(os.path.dirname(__file__), "best_params.json")
        if not os.path.exists(path):
            return
        with open(path, "r") as f:
            p = json.load(f)
        _learn["adx_threshold"] = p.get("adx_threshold", ADX_MIN)
        _learn["rsi_low_buy"]   = p.get("rsi_low_buy",   40)
        _learn["rsi_high_buy"]  = p.get("rsi_high_buy",  65)
        _learn["rsi_low_sell"]  = p.get("rsi_low_sell",  35)
        _learn["rsi_high_sell"] = p.get("rsi_high_sell", 60)
        _learn["min_score"]     = p.get("min_score",     8)
        _learn["strategy_type"] = p.get("strategy_type", "BALANCED")
        if "break_even_at" in p:
            global BREAK_EVEN_AT
            BREAK_EVEN_AT = float(p["break_even_at"])
        if "sl_mult" in p:
            global ATR_SL_MULT
            ATR_SL_MULT = float(p["sl_mult"])
        if "tp_mult" in p:
            global ATR_TP_MULT
            ATR_TP_MULT = float(p["tp_mult"])
        print(f"[OPT] Optimierte Parameter geladen: ADX={p['adx_threshold']} "
              f"RSI-B={p['rsi_low_buy']}-{p['rsi_high_buy']} "
              f"Typ={p.get('strategy_type','BALANCED')} Score={p.get('min_score',8)} "
              f"BE={p.get('break_even_at',1.0)}")
    except Exception as e:
        print(f"[WARN] best_params.json konnte nicht geladen werden: {e}")


if not os.environ.get("OPTIMIZER_MODE"):
    load_best_params()


# ── SHARED STATE ──────────────────────────────────────────────────────────────
_lock = threading.Lock()
_state = {
    "status":         "starting",
    "connected":      False,
    "symbol":         SYMBOL,
    "account":        {"balance": 0, "equity": 0, "profit": 0,
                       "currency": "USD", "leverage": 100, "server": "", "name": ""},
    "positions":      [],
    "last_signal":    None,
    "indicators":     {},
    "candle_pattern": None,
    "stats":          {"trades_total": 0, "trades_win": 0, "win_rate": 0.0,
                       "total_profit": 0.0, "biggest_win": 0.0, "biggest_loss": 0.0},
    "price":          {"bid": 0, "ask": 0, "spread": 0},
    "learn":          _learn,
    "last_update":    "",
}


class _SafeEncoder(json.JSONEncoder):
    """Handles numpy scalars that MT5 returns in structured arrays."""
    def default(self, obj):
        try:
            if hasattr(obj, 'item'):      # numpy scalar → Python native
                return obj.item()
        except Exception:
            pass
        return super().default(obj)


def state_get():
    with _lock:
        return json.loads(json.dumps(_state, cls=_SafeEncoder))


def state_set(key, value):
    with _lock:
        _state[key] = value
        _state["last_update"] = datetime.now(timezone.utc).isoformat()


# ── INDICATOR ENGINE ──────────────────────────────────────────────────────────

def ema_series(prices, period):
    k = 2 / (period + 1)
    result = [prices[0]]
    for p in prices[1:]:
        result.append(p * k + result[-1] * (1 - k))
    return result


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    return 100 - (100 / (1 + ag / al))


def macd(closes, fast=12, slow=26, signal=9):
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    ml = [f - s for f, s in zip(ef, es)]
    sl = ema_series(ml, signal)
    return ml, sl, [m - s for m, s in zip(ml, sl)]


def atr(rates, period=14):
    trs = []
    for i in range(1, len(rates)):
        h, l, pc = rates[i]["high"], rates[i]["low"], rates[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.001
    if len(trs) < period:
        return trs[-1]
    v = sum(trs[:period]) / period
    for tr in trs[period:]:
        v = (v * (period - 1) + tr) / period
    return v


def adx(rates, period=14):
    if len(rates) < period + 2:
        return 0.0
    pdm, mdm, trs = [], [], []
    for i in range(1, len(rates)):
        h, l   = rates[i]["high"],   rates[i]["low"]
        ph, pl = rates[i-1]["high"], rates[i-1]["low"]
        pc     = rates[i-1]["close"]
        up, dn = h - ph, pl - l
        pdm.append(up if up > dn and up > 0 else 0)
        mdm.append(dn if dn > up and dn > 0 else 0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    def smooth(arr, p):
        s = sum(arr[:p]); r = [s]
        for v in arr[p:]: s = s - s/p + v; r.append(s)
        return r

    tr_s = smooth(trs, period)
    ps   = smooth(pdm, period)
    ms   = smooth(mdm, period)
    pdi  = [100*p/t if t else 0 for p, t in zip(ps, tr_s)]
    mdi  = [100*m/t if t else 0 for m, t in zip(ms, tr_s)]
    dx   = [100*abs(p-m)/(p+m) if (p+m) else 0 for p, m in zip(pdi, mdi)]
    if len(dx) < period:
        return sum(dx) / len(dx) if dx else 0.0
    v = sum(dx[:period]) / period
    for d in dx[period:]:
        v = (v * (period - 1) + d) / period
    return v


def market_structure(closes, lookback=10):
    if len(closes) < lookback * 2:
        return "neutral"
    mid = len(closes) // 2
    fh, sh = closes[:mid], closes[mid:]
    if max(sh) > max(fh) and min(sh) > min(fh):
        return "bullish"
    if max(sh) < max(fh) and min(sh) < min(fh):
        return "bearish"
    return "neutral"


# ── CANDLESTICK PATTERN ENGINE ────────────────────────────────────────────────

def _body(c):   return abs(c["close"] - c["open"])
def _range(c):  return c["high"] - c["low"] or 0.00001
def _us(c):     return c["high"] - max(c["open"], c["close"])
def _ls(c):     return min(c["open"], c["close"]) - c["low"]
def _bull(c):   return c["close"] > c["open"]
def _bear(c):   return c["close"] < c["open"]


def detect_candle_patterns(rates):
    if len(rates) < 3:
        return None, "neutral"
    c0, c1, c2 = rates[-1], rates[-2], rates[-3]
    b0, b1 = _body(c0), _body(c1)
    r0     = _range(c0)
    us0, ls0 = _us(c0), _ls(c0)
    us1, ls1 = _us(c1), _ls(c1)
    avg_b  = (b0 + b1 + _body(c2)) / 3

    # Bullish
    if _bull(c0) and ls0 >= b0*2 and us0 <= b0*0.3 and b0 < r0*0.4:
        return "Hammer", "bullish"
    if _bull(c0) and us0 >= b0*2 and ls0 <= b0*0.3 and b0 < r0*0.4:
        return "Inverted Hammer", "bullish"
    if b0 <= r0*0.05 and ls0 >= r0*0.6 and us0 <= r0*0.1:
        return "Dragonfly Doji", "bullish"
    if (_bear(c1) and _bull(c0) and c0["open"] < c1["close"]
            and c0["close"] > c1["open"] and b0 > b1):
        return "Bullish Engulfing", "bullish"
    if (_bear(c1) and _bull(c0) and c0["open"] < c1["low"]
            and c0["close"] > (c1["open"]+c1["close"])/2
            and c0["close"] < c1["open"]):
        return "Piercing Line", "bullish"
    if (_bear(c2) and _body(c1) < avg_b*0.3 and _bull(c0)
            and c0["close"] > (c2["open"]+c2["close"])/2):
        return "Morning Star", "bullish"
    if (_bull(c0) and _bull(c1) and _bull(c2)
            and c0["close"] > c1["close"] > c2["close"]
            and c0["open"]  > c1["open"]  > c2["open"]
            and us0 < b0*0.3 and us1 < b1*0.3):
        return "Three White Soldiers", "bullish"
    if _bear(c1) and _bull(c0) and abs(c0["low"]-c1["low"]) < r0*0.05:
        return "Tweezer Bottom", "bullish"

    # Bearish
    if _bear(c0) and us0 >= b0*2 and ls0 <= b0*0.3 and b0 < r0*0.4:
        return "Shooting Star", "bearish"
    if (_bear(c0) and ls0 >= b0*2 and us0 <= b0*0.3
            and b0 < r0*0.4 and _bull(c1)):
        return "Hanging Man", "bearish"
    if b0 <= r0*0.05 and us0 >= r0*0.6 and ls0 <= r0*0.1:
        return "Gravestone Doji", "bearish"
    if (_bull(c1) and _bear(c0) and c0["open"] > c1["close"]
            and c0["close"] < c1["open"] and b0 > b1):
        return "Bearish Engulfing", "bearish"
    if (_bull(c1) and _bear(c0) and c0["open"] > c1["high"]
            and c0["close"] < (c1["open"]+c1["close"])/2
            and c0["close"] > c1["open"]):
        return "Dark Cloud Cover", "bearish"
    if (_bull(c2) and _body(c1) < avg_b*0.3 and _bear(c0)
            and c0["close"] < (c2["open"]+c2["close"])/2):
        return "Evening Star", "bearish"
    if (_bear(c0) and _bear(c1) and _bear(c2)
            and c0["close"] < c1["close"] < c2["close"]
            and c0["open"]  < c1["open"]  < c2["open"]
            and ls0 < b0*0.3 and ls1 < b1*0.3):
        return "Three Black Crows", "bearish"
    if _bull(c1) and _bear(c0) and abs(c0["high"]-c1["high"]) < r0*0.05:
        return "Tweezer Top", "bearish"

    # Neutral
    if b0 <= r0*0.05:
        return "Doji", "neutral"
    if b0 < avg_b*0.3 and us0 > b0 and ls0 > b0:
        return "Spinning Top", "neutral"

    return None, "neutral"


# ── POSITION SIZING ───────────────────────────────────────────────────────────

def get_contract_size(symbol):
    """Gibt die Kontraktgroesse fuer ein Symbol zurueck."""
    if MT5_AVAILABLE:
        info = mt5.symbol_info(symbol)
        if info:
            return info.trade_contract_size
    return CONTRACT_SIZES.get(symbol, 100000)


def pip_value(symbol):
    """Gibt den Pip-Wert fuer Spread/SL-Anzeige zurueck."""
    if MT5_AVAILABLE:
        info = mt5.symbol_info(symbol)
        if info:
            return info.point * 10
    if "XAU" in symbol or "XAG" in symbol:
        return 0.1
    if "JPY" in symbol:
        return 0.01
    return 0.0001


def calc_lot_selflearn(balance, sl_dist, symbol):
    """SELF-LEARN: 1% ATR-basiert, symbol-korrekt."""
    risk = balance * (RISK_PCT / 100)
    cs   = get_contract_size(symbol)
    return round(max(risk / (sl_dist * cs), 0.01), 2)


def calc_lot_kelly(balance, sl_dist, symbol):
    """KELLY CRITERION: f* = (p*b - q) / b, max 5%."""
    with _lock:
        st = _state["stats"]
        wr = st.get("win_rate", 50.0)
    p = wr / 100
    q = 1 - p
    b = ATR_TP_MULT / ATR_SL_MULT
    kelly_f = max(0.0, (p * b - q) / b) if b > 0 else 0.01
    kelly_f = min(kelly_f, 0.05)
    with _lock:
        _learn["kelly_f"] = round(kelly_f * 100, 2)
    risk = balance * kelly_f
    cs   = get_contract_size(symbol)
    return round(max(risk / (sl_dist * cs), 0.01), 2)


def calc_lot_random(balance, sl_dist, symbol):
    """RANDOM: 0.25%–1.75% zufaelliges Risiko."""
    pct  = 0.25 + _random.random() * 1.5
    risk = balance * (pct / 100)
    cs   = get_contract_size(symbol)
    return round(max(risk / (sl_dist * cs), 0.01), 2)


def get_lot(balance, sl_dist, symbol):
    with _lock:
        mode = _learn["mode"]
    if mode == "KELLY":
        return calc_lot_kelly(balance, sl_dist, symbol)
    if mode == "RANDOM":
        return calc_lot_random(balance, sl_dist, symbol)
    return calc_lot_selflearn(balance, sl_dist, symbol)


# ── SELF-LEARNING ENGINE ──────────────────────────────────────────────────────

def record_pattern_result(pattern, profit):
    """Verfolgt Gewinn/Verlust pro Candlestick-Pattern."""
    if not pattern:
        return
    with _lock:
        ps = _learn["pattern_stats"]
        if pattern not in ps:
            ps[pattern] = {"wins": 0, "total": 0, "wr": 0.0}
        ps[pattern]["total"] += 1
        if profit > 0:
            ps[pattern]["wins"] += 1
        t = ps[pattern]["total"]
        ps[pattern]["wr"] = round(ps[pattern]["wins"] / t * 100, 1)
        # Blockiere Patterns mit WR < 30% nach mindestens 5 Trades
        blocked = [p for p, s in ps.items() if s["total"] >= 5 and s["wr"] < 30]
        _learn["blocked_patterns"] = blocked


def record_strategy_result(strategy_type, profit):
    """Verfolgt Gewinn/Verlust pro Strategie-Typ für automatischen Switch."""
    if not strategy_type:
        return
    with _lock:
        sp = _learn["strategy_perf"]
        if strategy_type not in sp:
            sp[strategy_type] = {"trades": 0, "wins": 0, "wr": 0.0}
        sp[strategy_type]["trades"] += 1
        if profit > 0:
            sp[strategy_type]["wins"] += 1
        t = sp[strategy_type]["trades"]
        sp[strategy_type]["wr"] = round(sp[strategy_type]["wins"] / t * 100, 1)


def self_adjust():
    """
    Analysiert die letzten 20 Trades und passt Schwellenwerte an.
    Laeuft nur im SELF-LEARN Modus.
    """
    global _last_trade_count
    if not MT5_AVAILABLE:
        return

    deals = mt5.history_deals_get(
        datetime(2000, 1, 1, tzinfo=timezone.utc),
        datetime.now(timezone.utc)
    )
    if not deals:
        return

    our = [d for d in deals if d.magic == MAGIC and d.profit != 0]
    total = len(our)

    with _lock:
        mode = _learn["mode"]
        _learn["recent_trades"] = total

    if total == _last_trade_count:
        return  # Nichts Neues
    if total < 5:
        _last_trade_count = total
        return

    # Neuen Trade erkennen — Pattern-Tracking
    if total > _last_trade_count:
        new_deals = sorted(our, key=lambda d: d.time)[_last_trade_count:]
        with _lock:
            last_pattern = _state.get("candle_pattern") or {}
            pname = last_pattern.get("name") if isinstance(last_pattern, dict) else None
        for d in new_deals:
            record_pattern_result(pname, d.profit)
            with _lock:
                stype = _state.get("indicators", {}).get("strategy_type", "BALANCED")
            record_strategy_result(stype, d.profit)
            # Konsekutive Verlust-Tracking
            with _lock:
                if d.profit < 0:
                    _risk_state["consec_losses"] += 1
                elif d.profit > 0:
                    _risk_state["consec_losses"] = 0

    _last_trade_count = total

    if mode != "SELF-LEARN":
        return  # Nur im SELF-LEARN Modus automatisch anpassen

    # Lernzyklus nur alle LEARN_EVERY neuen Trades
    if total % LEARN_EVERY != 0:
        return

    recent  = sorted(our, key=lambda d: d.time)[-20:]
    profits = [d.profit for d in recent]
    wins    = [p for p in profits if p > 0]
    wr      = len(wins) / len(profits) * 100 if profits else 50.0

    with _lock:
        _learn["recent_wr"] = round(wr, 1)
        old_adx = _learn["adx_threshold"]
        msg = ""

        if wr < 35:
            # Schlechte Phase — deutlich strenger
            _learn["adx_threshold"]  = min(old_adx + 4, 45)
            _learn["rsi_low_buy"]    = min(_learn["rsi_low_buy"]  + 3, 52)
            _learn["rsi_high_buy"]   = max(_learn["rsi_high_buy"] - 3, 58)
            _learn["rsi_low_sell"]   = min(_learn["rsi_low_sell"] + 3, 48)
            _learn["rsi_high_sell"]  = max(_learn["rsi_high_sell"]- 3, 53)
            _learn["adjustments"]   += 1
            msg = f"FILTER STRENGER: ADX {old_adx}->{_learn['adx_threshold']} (WR={wr:.0f}%)"

        elif wr < 45:
            # Unterdurchschnittlich — ADX erhoehen
            _learn["adx_threshold"]  = min(old_adx + 2, 40)
            _learn["adjustments"]   += 1
            msg = f"ADX ERHOET: {old_adx}->{_learn['adx_threshold']} (WR={wr:.0f}%)"

        elif wr > 65 and old_adx > ADX_MIN:
            # Gute Phase — etwas lockerer werden
            _learn["adx_threshold"]  = max(old_adx - 1, ADX_MIN)
            _learn["adjustments"]   += 1
            msg = f"ADX GESENKT: {old_adx}->{_learn['adx_threshold']} (WR={wr:.0f}%)"

        # Automatisch zur besten Strategie wechseln (mind. 5 Trades pro Typ)
        sp = _learn["strategy_perf"]
        qualified = {k: v for k, v in sp.items() if v["trades"] >= 5}
        if qualified:
            best_type = max(qualified, key=lambda k: (qualified[k]["wr"], qualified[k]["trades"]))
            if best_type != _learn["strategy_type"]:
                old_type = _learn["strategy_type"]
                _learn["strategy_type"] = best_type
                _learn["best_strategy"] = best_type
                msg_type = f"STRATEGIE SWITCH: {old_type}→{best_type} (WR={qualified[best_type]['wr']:.0f}%)"
                print(f"[LEARN] {msg_type}")

        if msg:
            _learn["last_adjust"] = msg
            print(f"[LEARN] {msg}")

        _state["learn"] = json.loads(json.dumps(_learn))


# ── FILTER: SESSION / H4-TREND / VOLUMEN ─────────────────────────────────────

def in_session():
    """Nur waehrend London + NY Session traden (07-17 UTC). Bestes Zeitfenster fuer XAUUSD."""
    h = datetime.now(timezone.utc).hour
    return SESSION_START <= h < SESSION_END


def get_h4_trend():
    """H4 Trend: EMA20 vs EMA50 auf Stundenkerzen. Gibt 'bullish'/'bearish'/'neutral' zurueck."""
    if not MT5_AVAILABLE:
        return "neutral"
    rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME_H4, 0, 60)
    if rates is None or len(rates) < 55:
        return "neutral"
    closes = [r["close"] for r in rates]
    e20 = ema_series(closes, 20)[-1]
    e50 = ema_series(closes, 50)[-1]
    if e20 > e50 * 1.0002:
        return "bullish"
    if e20 < e50 * 0.9998:
        return "bearish"
    return "neutral"


def vol_confirmed(rates):
    """Volumen der letzten Kerze > 110% des 20-Kerzen-Durchschnitts."""
    if len(rates) < 21:
        return True
    vols = [r["tick_volume"] for r in rates[-21:-1]]
    avg  = sum(vols) / len(vols)
    return rates[-1]["tick_volume"] >= avg * 1.10


# ── STRATEGIE-GEWICHTUNGEN ────────────────────────────────────────────────────
# Jeder Typ betont andere Indikatoren — vom Optimizer gefunden.
STRATEGY_WEIGHTS = {
    # ── ENSEMBLE: Mehrheitsvotum aller Strategien ──────────────────────────────
    "ENSEMBLE":    dict(ema=2, adx=1, rsi=2, macd=2, bb=3, stoch=3, vwap=2, fib=2, ob=2, fvg=2, struct=1, pat=1),
    # ── KLASSISCHE ELITE-METHODEN ─────────────────────────────────────────────
    "BALANCED":    dict(ema=2, adx=1, rsi=1, macd=1, bb=2, stoch=2, vwap=1, fib=2, ob=2, fvg=1, struct=1, pat=1),
    "BB_SCALP":    dict(ema=1, adx=1, rsi=1, macd=1, bb=4, stoch=3, vwap=1, fib=1, ob=1, fvg=1, struct=1, pat=1),
    "SCALP":       dict(ema=1, adx=1, rsi=3, macd=1, bb=4, stoch=4, vwap=3, fib=0, ob=0, fvg=0, struct=1, pat=1),
    "FIB_SWING":   dict(ema=2, adx=1, rsi=1, macd=1, bb=1, stoch=1, vwap=1, fib=5, ob=3, fvg=2, struct=2, pat=1),
    "ICT_SMC":     dict(ema=1, adx=1, rsi=1, macd=1, bb=1, stoch=1, vwap=1, fib=2, ob=5, fvg=4, struct=2, pat=1),
    "VWAP_TREND":  dict(ema=3, adx=2, rsi=1, macd=2, bb=1, stoch=1, vwap=3, fib=1, ob=1, fvg=1, struct=2, pat=1),
    "MOMENTUM":    dict(ema=2, adx=1, rsi=2, macd=4, bb=1, stoch=3, vwap=1, fib=1, ob=1, fvg=1, struct=1, pat=1),
    # ── NEU: WEITERE ELITE-METHODEN ───────────────────────────────────────────
    # Mean Reversion an RSI/StochRSI-Extremen (Contrarian)
    "REVERSAL":    dict(ema=1, adx=1, rsi=5, macd=1, bb=4, stoch=5, vwap=1, fib=1, ob=1, fvg=1, struct=1, pat=3),
    # BB-Squeeze Breakout (Linda Raschke / Mark Minervini)
    "BREAKOUT":    dict(ema=2, adx=4, rsi=1, macd=3, bb=5, stoch=1, vwap=2, fib=1, ob=2, fvg=2, struct=2, pat=1),
    # Pure Price Action (Al Brooks / Lance Beggs)
    "PRICE_ACTION":dict(ema=2, adx=1, rsi=1, macd=1, bb=1, stoch=1, vwap=1, fib=2, ob=3, fvg=2, struct=4, pat=5),
    # Wyckoff Methode (Akkumulation/Distribution Phasen)
    "WYCKOFF":     dict(ema=2, adx=2, rsi=1, macd=2, bb=3, stoch=1, vwap=3, fib=1, ob=2, fvg=1, struct=5, pat=1),
    # ── NEU: ERWEITERTE ELITE-METHODEN ──────────────────────────────────────────
    # Ichimoku Cloud System (Hosoda — japanische Profis)
    "ICHIMOKU":    dict(ema=1, adx=1, rsi=1, macd=1, bb=1, stoch=1, vwap=1, fib=1, ob=1, fvg=1, struct=2, pat=1,
                        ichi=5, super=2, don=1, cci=1, willr=1, vol=1),
    # Supertrend Trendfolge (ATR-basiert, sehr zuverlässig)
    "SUPERTREND":  dict(ema=2, adx=2, rsi=1, macd=2, bb=1, stoch=1, vwap=1, fib=1, ob=1, fvg=1, struct=2, pat=1,
                        ichi=1, super=5, don=2, cci=1, willr=1, vol=2),
    # Multi-Timeframe (lange EMAs simulieren H1/H4 Trend)
    "MULTI_TF":    dict(ema=5, adx=2, rsi=1, macd=2, bb=1, stoch=1, vwap=2, fib=1, ob=1, fvg=1, struct=3, pat=1,
                        ichi=2, super=3, don=1, cci=1, willr=1, vol=1),
    # Bollinger Squeeze (BB-Kompression → explosiver Ausbruch)
    "BB_SQUEEZE":  dict(ema=1, adx=2, rsi=1, macd=2, bb=5, stoch=1, vwap=1, fib=1, ob=1, fvg=1, struct=1, pat=1,
                        ichi=1, super=1, don=3, cci=2, willr=1, vol=3),
    # Volumen-bestätigt (kein Trade ohne Volumen-Bestätigung)
    "VOLUME_CONF": dict(ema=2, adx=2, rsi=2, macd=2, bb=1, stoch=1, vwap=3, fib=1, ob=1, fvg=1, struct=2, pat=2,
                        ichi=1, super=2, don=1, cci=2, willr=2, vol=5),
}

# ── ELITE INDICATORS ─────────────────────────────────────────────────────────

def bollinger_bands(closes, period=20, std_dev=2.0):
    """Bollinger Bands: (upper, mid, lower, bandwidth_pct, %B 0-1)."""
    if len(closes) < period:
        return None, None, None, None, None
    w   = closes[-period:]
    mid = sum(w) / period
    std = (sum((x - mid) ** 2 for x in w) / period) ** 0.5
    if std == 0:
        return mid, mid, mid, 0.0, 0.5
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    bw    = (upper - lower) / mid * 100
    pctb  = max(0.0, min(1.0, (closes[-1] - lower) / (upper - lower)))
    return round(upper, 2), round(mid, 2), round(lower, 2), round(bw, 2), round(pctb, 3)


def stoch_rsi(closes, period=14, smooth_k=3, smooth_d=3):
    """Stochastic RSI — returns (%K, %D) on 0-100 scale."""
    if len(closes) < period * 3 + smooth_k + smooth_d:
        return 50.0, 50.0
    rsi_vals = []
    for i in range(period, len(closes)):
        seg = closes[i - period: i + 1]
        g = [max(seg[j]-seg[j-1], 0)   for j in range(1, len(seg))]
        l = [abs(min(seg[j]-seg[j-1], 0)) for j in range(1, len(seg))]
        ag, al = sum(g)/period, sum(l)/period
        rs = ag / al if al > 0 else 999
        rsi_vals.append(100 - 100 / (1 + rs))
    if len(rsi_vals) < period + smooth_k:
        return 50.0, 50.0
    stoch = []
    for i in range(period - 1, len(rsi_vals)):
        w = rsi_vals[i - period + 1: i + 1]
        lo, hi = min(w), max(w)
        stoch.append((rsi_vals[i] - lo) / (hi - lo) * 100 if hi > lo else 50.0)
    def sma(arr, n): return sum(arr[-n:]) / n
    k_sm = [sma(stoch[:i+1], smooth_k) for i in range(smooth_k - 1, len(stoch))]
    if not k_sm:
        return 50.0, 50.0
    d_line = sma(k_sm, smooth_d) if len(k_sm) >= smooth_d else k_sm[-1]
    return round(k_sm[-1], 1), round(d_line, 1)


def vwap_calc(rates):
    """Intraday VWAP — resets at SESSION_START UTC."""
    if not rates or len(rates) < 2:
        return None
    session = []
    for r in reversed(rates):
        t = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc)
        session.insert(0, r)
        if t.hour == SESSION_START and t.minute < 16:
            break
    if not session:
        session = list(rates)
    tp_vol = sum(((r["high"]+r["low"]+r["close"])/3) * r["tick_volume"] for r in session)
    vol    = sum(r["tick_volume"] for r in session)
    return round(tp_vol / vol, 2) if vol > 0 else None


def fibonacci_levels(rates, lookback=55):
    """Auto swing-high/low + Fibonacci retracement levels (0–100%)."""
    w = list(rates[-lookback:]) if len(rates) >= lookback else list(rates)
    if len(w) < 10:
        return None
    sh    = float(max(r["high"] for r in w))
    sl    = float(min(r["low"]  for r in w))
    diff  = sh - sl
    if diff < 0.5:
        return None
    price = float(rates[-1]["close"])
    lvls  = {
        "0.0":   sh,
        "23.6":  sh - 0.236 * diff,
        "38.2":  sh - 0.382 * diff,
        "50.0":  sh - 0.500 * diff,
        "61.8":  sh - 0.618 * diff,
        "78.6":  sh - 0.786 * diff,
        "100.0": sl,
    }
    near = min(lvls.items(), key=lambda x: abs(x[1] - price))
    dist = abs(near[1] - price) / price * 100
    return {
        "swing_high":    round(sh, 2),
        "swing_low":     round(sl, 2),
        "levels":        {k: round(float(v), 2) for k, v in lvls.items()},
        "nearest":       near[0],
        "nearest_price": round(float(near[1]), 2),
        "dist_pct":      round(float(dist), 2),
        "near_key":      bool(dist < 0.25),
    }


def find_order_blocks(rates, lookback=40):
    """
    ICT Order Block: last opposing candle before 3-bar impulse move.
    Bullish OB = last bearish bar before 3 consecutive bullish bars.
    Bearish OB = last bullish bar before 3 consecutive bearish bars.
    """
    w = list(rates[-lookback:]) if len(rates) >= lookback else list(rates)
    bull_ob = bear_ob = None
    for i in range(2, len(w) - 3):
        c    = w[i]
        nxt3 = w[i+1:i+4]
        if len(nxt3) < 3:
            break
        if c["close"] < c["open"] and all(x["close"] > x["open"] for x in nxt3):
            bull_ob = {"high": round(c["high"], 2), "low": round(c["low"], 2)}
        if c["close"] > c["open"] and all(x["close"] < x["open"] for x in nxt3):
            bear_ob = {"high": round(c["high"], 2), "low": round(c["low"], 2)}
    return {"bullish": bull_ob, "bearish": bear_ob}


def find_fair_value_gaps(rates, lookback=30):
    """
    Fair Value Gap (FVG / Imbalance): 3-bar price gap.
    Bullish FVG: bar[i].low > bar[i-2].high  (unfilled gap up).
    Bearish FVG: bar[i].high < bar[i-2].low  (unfilled gap down).
    """
    w = list(rates[-lookback:]) if len(rates) >= lookback else list(rates)
    bull_fvgs, bear_fvgs = [], []
    for i in range(2, len(w)):
        c0, c2 = w[i-2], w[i]
        if c2["low"] > c0["high"]:
            bull_fvgs.append({"top": round(c2["low"],2), "bottom": round(c0["high"],2)})
        if c2["high"] < c0["low"]:
            bear_fvgs.append({"top": round(c0["low"],2), "bottom": round(c2["high"],2)})
    return {"bullish": bull_fvgs[-3:], "bearish": bear_fvgs[-3:]}


# ── NEU: ERWEITERTE ELITE-INDIKATOREN ────────────────────────────────────────

def ichimoku(candles, tenkan=9, kijun=26, senkou_b=52):
    """Ichimoku Cloud: Tenkan/Kijun Cross + Price vs Cloud."""
    if len(candles) < senkou_b:
        return {"trend": "neutral", "tk_cross": "neutral", "cloud_pos": "neutral"}
    highs = [c["high"] for c in candles]
    lows  = [c["low"]  for c in candles]

    def midpt(n):
        return (max(highs[-n:]) + min(lows[-n:])) / 2

    tenkan_v   = midpt(tenkan)
    kijun_v    = midpt(kijun)
    senkou_a   = (tenkan_v + kijun_v) / 2
    senkou_b_v = midpt(senkou_b)
    cloud_top  = max(senkou_a, senkou_b_v)
    cloud_bot  = min(senkou_a, senkou_b_v)
    price      = candles[-1]["close"]

    cloud_pos = "bullish" if price > cloud_top else ("bearish" if price < cloud_bot else "neutral")
    tk_cross  = "bullish" if tenkan_v > kijun_v else ("bearish" if tenkan_v < kijun_v else "neutral")
    trend     = "bullish" if cloud_pos == "bullish" and tk_cross == "bullish" else \
                "bearish" if cloud_pos == "bearish" and tk_cross == "bearish" else "neutral"
    return {"trend": trend, "tk_cross": tk_cross, "cloud_pos": cloud_pos,
            "tenkan": round(tenkan_v, 2), "kijun": round(kijun_v, 2)}


def supertrend(candles, period=10, multiplier=3.0):
    """Supertrend: ATR-basierter Trendfolger (bullish/bearish)."""
    if len(candles) < period + 2:
        return "neutral", 0.0
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    closes = [c["close"] for c in candles]
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
           for i in range(1, len(candles))]
    atr_avg = sum(trs[-period:]) / period
    mid     = (highs[-1] + lows[-1]) / 2
    upper   = mid + multiplier * atr_avg
    lower   = mid - multiplier * atr_avg
    close   = closes[-1]
    direction = "bullish" if close > mid else "bearish"
    band = lower if direction == "bullish" else upper
    return direction, round(band, 2)


def donchian_channel(candles, period=20):
    """Donchian Channel: Höchstes Hoch / Tiefstes Tief der letzten N Kerzen."""
    if len(candles) < period:
        return None, None, None
    seg   = candles[-period:]
    upper = max(c["high"] for c in seg)
    lower = min(c["low"]  for c in seg)
    mid   = (upper + lower) / 2
    return round(upper, 2), round(lower, 2), round(mid, 2)


def cci_indicator(candles, period=20):
    """Commodity Channel Index (CCI)."""
    if len(candles) < period:
        return 0.0
    tps  = [(c["high"] + c["low"] + c["close"]) / 3 for c in candles[-period:]]
    avg  = sum(tps) / period
    mdev = sum(abs(tp - avg) for tp in tps) / period
    return round((tps[-1] - avg) / (0.015 * mdev), 1) if mdev > 0 else 0.0


def williams_r(candles, period=14):
    """Williams %R: Überkauft/Überverkauft Oszillator (-100 bis 0)."""
    if len(candles) < period:
        return -50.0
    seg   = candles[-period:]
    h     = max(c["high"]  for c in seg)
    l     = min(c["low"]   for c in seg)
    close = candles[-1]["close"]
    return round(-100 * (h - close) / (h - l), 1) if h != l else -50.0


def volume_ratio(candles, period=20):
    """Volumen-Ratio: aktuell vs. Durchschnitt. Gibt (ratio, is_high_vol) zurück."""
    if len(candles) < period + 1:
        return 1.0, False
    vols = []
    for c in candles:
        # Kompatibel mit dict UND numpy.void (MT5-Daten)
        try:
            v = float(c["tick_volume"])
        except (KeyError, TypeError):
            try:
                v = float(c["volume"])
            except (KeyError, TypeError):
                v = 1.0
        vols.append(v)
    avg  = sum(vols[-(period + 1):-1]) / period
    cur  = vols[-1]
    ratio = round(cur / avg, 2) if avg > 0 else 1.0
    return ratio, ratio >= 1.3


def choppiness_index(rates, period=14):
    """
    Choppiness Index: misst ob der Markt trendet oder seitwärts läuft.
    CI < 38.2 = starker Trend | CI > 61.8 = choppy/ranging
    Wir verwenden 56.0 als Schwellenwert (konservativ).
    """
    if len(rates) < period + 2:
        return 50.0
    seg = list(rates)[-(period + 1):]
    hh = max(r["high"]  for r in seg[1:])
    ll = min(r["low"]   for r in seg[1:])
    range_hl = hh - ll
    if range_hl <= 0:
        return 50.0
    sum_tr = sum(
        max(seg[i]["high"] - seg[i]["low"],
            abs(seg[i]["high"] - seg[i-1]["close"]),
            abs(seg[i]["low"]  - seg[i-1]["close"]))
        for i in range(1, len(seg))
    )
    if sum_tr <= 0:
        return 50.0
    return round(100.0 * math.log10(sum_tr / range_hl) / math.log10(period), 1)


# ── SIGNAL ENGINE ─────────────────────────────────────────────────────────────

def get_bars(symbol, timeframe, n=250):
    if not MT5_AVAILABLE:
        return None
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n)
    if rates is None or len(rates) == 0:
        return None
    return rates


def _single_strategy_signal(rates, strat=None):
    """
    Core confluence signal engine — called by get_signal() and _ensemble_signal().
    If strat is provided, its values override _learn for adx_min, rsi ranges, etc.
    """
    closes = [r["close"] for r in rates]
    if len(closes) < EMA_SLOW + 10:
        return None, {}

    with _lock:
        adx_min      = _learn["adx_threshold"]
        rsi_lo_buy   = _learn["rsi_low_buy"]
        rsi_hi_buy   = _learn["rsi_high_buy"]
        rsi_lo_sell  = _learn["rsi_low_sell"]
        rsi_hi_sell  = _learn["rsi_high_sell"]
        blocked      = list(_learn["blocked_patterns"])
        min_score    = _learn.get("min_score", 8)
        stype        = _learn.get("strategy_type", "BALANCED")

    if strat is not None:
        adx_min    = strat.get("adx_min",    adx_min)
        rsi_lo_buy = strat.get("rsi_low_b",  rsi_lo_buy)
        rsi_hi_buy = strat.get("rsi_high_b", rsi_hi_buy)
        rsi_lo_sell= strat.get("rsi_low_s",  rsi_lo_sell)
        rsi_hi_sell= strat.get("rsi_high_s", rsi_hi_sell)
        min_score  = strat.get("min_score",  min_score)
        stype      = strat.get("strategy_type", stype)

    W = STRATEGY_WEIGHTS.get(stype, STRATEGY_WEIGHTS["BALANCED"])

    # ── CLASSIC INDICATORS ────────────────────────────────────────────────────
    ef    = ema_series(closes, EMA_FAST)
    em    = ema_series(closes, EMA_MID)
    es    = ema_series(closes, EMA_SLOW)
    rsi_v = rsi(closes[-RSI_PERIOD - 10:], RSI_PERIOD)
    ml, ms_l, mh = macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    atr_v  = atr(list(rates)[-ATR_PERIOD - 5:], ATR_PERIOD)
    adx_v  = adx(list(rates)[-ADX_PERIOD * 3:], ADX_PERIOD)
    struct = market_structure(closes[-30:])
    pname, pbias = detect_candle_patterns(list(rates)[-3:])

    # ── ELITE INDICATORS ──────────────────────────────────────────────────────
    bb_up, bb_mid, bb_lo, bb_bw, bb_pctb = bollinger_bands(closes[-60:])
    stk, stk_d = stoch_rsi(closes[-120:])
    vwap_v     = vwap_calc(list(rates))
    fib        = fibonacci_levels(list(rates))
    obs        = find_order_blocks(list(rates))
    fvgs       = find_fair_value_gaps(list(rates))

    price    = closes[-1]
    h4_trend = get_h4_trend()
    session  = in_session()
    vol_ok   = vol_confirmed(list(rates))

    # ── MARKET REGIME DETECTION (Choppiness Index) ───────────────────────────
    ci_val     = choppiness_index(list(rates), 14)
    is_ranging = ci_val > 56.0 and adx_v < 35  # choppy & kein Trend

    if is_ranging:
        # RANGE MODE: BB-Bounce + RSI-Extreme (H4-Filter nicht nötig)
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

        range_sig = None
        if session and vol_ok:
            if rb >= 7 and rb > rs + 2:       range_sig = "BUY"
            elif rs >= 7 and rs > rb + 2:     range_sig = "SELL"

        range_ind = {
            "ema20": round(ef[-1], 2), "ema50": round(em[-1], 2),
            "ema200": round(es[-1], 2), "rsi": round(rsi_v, 1),
            "atr": round(atr_v, 2), "adx": round(adx_v, 1),
            "macd_hist": round(mh[-1], 5), "structure": struct,
            "bb_pctb": round(bb_pctb, 3) if bb_pctb is not None else None,
            "bb_bandwidth": round(bb_bw, 2) if bb_bw is not None else None,
            "stoch_k": round(stk, 1), "stoch_d": round(stk_d, 1),
            "h4_trend": h4_trend, "session": session,
            "buy_score": rb, "sell_score": rs, "min_score": 7,
            "regime": "RANGE", "ci": ci_val, "strategy_type": stype,
        }
        return range_sig, range_ind

    # ── CONFLUENCE SCORING (Gewichtung aus Strategie-Typ) ────────────────────
    buy_score = sell_score = 0

    # 1. EMA Triple Alignment
    if ef[-1] > em[-1] > es[-1]:   buy_score  += W["ema"]
    elif ef[-1] < em[-1] < es[-1]: sell_score += W["ema"]

    # 2. ADX Trendstärke
    if adx_v >= adx_min:
        buy_score  += W["adx"]
        sell_score += W["adx"]

    # 3. RSI Zone
    if rsi_lo_buy  <= rsi_v <= rsi_hi_buy:  buy_score  += W["rsi"]
    if rsi_lo_sell <= rsi_v <= rsi_hi_sell: sell_score += W["rsi"]

    # 4. MACD Kreuz
    if mh[-1] > 0 and ml[-1] > ms_l[-1]:  buy_score  += W["macd"]
    if mh[-1] < 0 and ml[-1] < ms_l[-1]:  sell_score += W["macd"]

    # 5. Bollinger Bands (Bounce + Squeeze)
    if bb_pctb is not None:
        if bb_pctb < 0.2:   buy_score  += W["bb"]
        elif bb_pctb > 0.8: sell_score += W["bb"]
        if bb_bw is not None and bb_bw < 1.0:
            buy_score += 1; sell_score += 1

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

    # 11. Marktstruktur
    if struct == "bullish":    buy_score  += W["struct"]
    elif struct == "bearish":  sell_score += W["struct"]

    # 12. H4 Higher Timeframe (immer 2pt — wichtigster Filter)
    if h4_trend == "bullish":   buy_score  += 2
    elif h4_trend == "bearish": sell_score += 2

    # 13. Kerzenmuster
    BULL_PAT = {"Hammer","Inverted Hammer","Dragonfly Doji","Bullish Engulfing",
                "Piercing Line","Morning Star","Three White Soldiers","Tweezer Bottom"}
    BEAR_PAT = {"Shooting Star","Hanging Man","Gravestone Doji","Bearish Engulfing",
                "Dark Cloud Cover","Evening Star","Three Black Crows","Tweezer Top"}
    if pname and pname not in blocked:
        if pname in BULL_PAT:  buy_score  += W["pat"]
        if pname in BEAR_PAT:  sell_score += W["pat"]

    # ── NEUE ELITE-INDIKATOREN (mit .get() → Gewicht 0 = nicht aktiv) ─────────

    # 14. Ichimoku Cloud
    if W.get("ichi", 0):
        ichi_v = ichimoku(list(rates)[-60:])
        if ichi_v["trend"] == "bullish":   buy_score  += W["ichi"]
        elif ichi_v["trend"] == "bearish": sell_score += W["ichi"]
        if ichi_v["tk_cross"] == "bullish":   buy_score  += W["ichi"] // 2
        elif ichi_v["tk_cross"] == "bearish": sell_score += W["ichi"] // 2

    # 15. Supertrend
    if W.get("super", 0):
        st_dir, _ = supertrend(list(rates)[-30:])
        if st_dir == "bullish":   buy_score  += W["super"]
        elif st_dir == "bearish": sell_score += W["super"]

    # 16. Donchian Channel
    if W.get("don", 0):
        don_up, don_lo, don_mid = donchian_channel(list(rates)[-30:])
        if don_mid:
            if price > don_mid:   buy_score  += W["don"]
            else:                 sell_score += W["don"]
            if don_up and price >= don_up * 0.999: buy_score  += W["don"]
            if don_lo and price <= don_lo * 1.001: sell_score += W["don"]

    # 17. CCI
    if W.get("cci", 0):
        cci_v = cci_indicator(list(rates)[-25:])
        if cci_v > 100:    buy_score  += W["cci"]
        elif cci_v < -100: sell_score += W["cci"]

    # 18. Williams %R
    if W.get("willr", 0):
        willr_v = williams_r(list(rates)[-20:])
        if willr_v < -80:   buy_score  += W["willr"]  # überverkauft
        elif willr_v > -20: sell_score += W["willr"]  # überkauft

    # 19. Volumen-Bestätigung
    if W.get("vol", 0):
        vol_rat, vol_high = volume_ratio(list(rates)[-25:])
        if vol_high:
            cur = list(rates)[-1]
            if cur["close"] > cur["open"]: buy_score  += W["vol"]
            else:                          sell_score += W["vol"]

    # ── ENTSCHEIDUNG ──────────────────────────────────────────────────────────
    MIN_SCORE = min_score
    signal = None
    if session and vol_ok:
        if buy_score  >= MIN_SCORE and buy_score  > sell_score + 2:
            signal = "BUY"
        elif sell_score >= MIN_SCORE and sell_score > buy_score + 2:
            signal = "SELL"

    indicators = {
        # Classic
        "ema20":          round(ef[-1], 2),
        "ema50":          round(em[-1], 2),
        "ema200":         round(es[-1], 2),
        "rsi":            round(rsi_v, 1),
        "macd":           round(ml[-1], 5),
        "macd_sig":       round(ms_l[-1], 5),
        "macd_hist":      round(mh[-1], 5),
        "atr":            round(atr_v, 2),
        "adx":            round(adx_v, 1),
        "adx_min":        adx_min,
        "structure":      struct,
        "candle_pattern": pname,
        "candle_bias":    pbias,
        # Elite
        "bb_upper":       bb_up,
        "bb_mid":         bb_mid,
        "bb_lower":       bb_lo,
        "bb_bandwidth":   bb_bw,
        "bb_pctb":        bb_pctb,
        "stoch_k":        stk,
        "stoch_d":        stk_d,
        "vwap":           vwap_v,
        "fib_nearest":    fib["nearest"]       if fib else None,
        "fib_price":      fib["nearest_price"] if fib else None,
        "fib_near_key":   fib["near_key"]      if fib else False,
        "fib_swing_high": fib["swing_high"]    if fib else None,
        "fib_swing_low":  fib["swing_low"]     if fib else None,
        "ob_bull_high":   obs["bullish"]["high"] if obs["bullish"] else None,
        "ob_bull_low":    obs["bullish"]["low"]  if obs["bullish"] else None,
        "ob_bear_high":   obs["bearish"]["high"] if obs["bearish"] else None,
        "ob_bear_low":    obs["bearish"]["low"]  if obs["bearish"] else None,
        "fvg_bull_count": len(fvgs["bullish"]),
        "fvg_bear_count": len(fvgs["bearish"]),
        "h4_trend":       h4_trend,
        "session":        session,
        "vol_ok":         vol_ok,
        "buy_score":      buy_score,
        "sell_score":     sell_score,
        "min_score":      MIN_SCORE,
        "strategy_type":  stype,
        "regime":         "TREND",
        "ci":             ci_val,
    }

    return signal, indicators


def _ensemble_signal(rates):
    """
    Führt alle Einzel-Strategien aus und liefert Signal nur wenn Mehrheit einig ist.
    Gibt (signal, indicators) zurück — indicators vom stärksten Score.
    """
    votes_buy = 0
    votes_sell = 0
    best_ind = {}
    best_score_val = 0

    for stype, W in STRATEGY_WEIGHTS.items():
        if stype == "ENSEMBLE":
            continue
        mock_strat = {
            "strategy_type": stype,
            "adx_min":       _learn.get("adx_threshold", ADX_MIN),
            "rsi_low_b":     _learn.get("rsi_low_buy",  40),
            "rsi_high_b":    _learn.get("rsi_high_buy", 65),
            "rsi_low_s":     _learn.get("rsi_low_sell", 35),
            "rsi_high_s":    _learn.get("rsi_high_sell",60),
            "min_score":     _learn.get("min_score", 8),
        }
        sig, ind = _single_strategy_signal(rates, mock_strat)
        if sig == "BUY":
            votes_buy += 1
            sc = ind.get("buy_score", 0)
            if sc > best_score_val:
                best_score_val = sc
                best_ind = ind
        elif sig == "SELL":
            votes_sell += 1
            sc = ind.get("sell_score", 0)
            if sc > best_score_val:
                best_score_val = sc
                best_ind = ind

    total_votes = len(STRATEGY_WEIGHTS) - 1  # minus ENSEMBLE itself
    threshold = max(total_votes // 2 + 1, 3)  # majority: at least 4 of 7

    if votes_buy >= threshold:
        best_ind["ensemble_votes"] = f"{votes_buy}/{total_votes} BUY"
        return "BUY", best_ind
    if votes_sell >= threshold:
        best_ind["ensemble_votes"] = f"{votes_sell}/{total_votes} SELL"
        return "SELL", best_ind
    return None, best_ind


def get_signal(rates):
    """
    Multi-strategy confluence signal engine.
    13 indicators vote — needs 8+ points + session + volume.
    """
    # Ensemble-Modus: alle Strategien abstimmen lassen
    stype = _learn.get("strategy_type", "BALANCED")
    if stype == "ENSEMBLE":
        return _ensemble_signal(rates)

    return _single_strategy_signal(rates)


# ── ORDER MANAGEMENT ──────────────────────────────────────────────────────────

def open_position(symbol, signal, balance, atr_v, pattern_name):
    if not MT5_AVAILABLE:
        return False
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False

    sl_dist = atr_v * ATR_SL_MULT
    tp_dist = atr_v * ATR_TP_MULT
    lot     = get_lot(balance, sl_dist, symbol)

    if signal == "BUY":
        price, order_type = tick.ask, mt5.ORDER_TYPE_BUY
        sl = round(price - sl_dist, 5)
        tp = round(price + tp_dist, 5)
    else:
        price, order_type = tick.bid, mt5.ORDER_TYPE_SELL
        sl = round(price + sl_dist, 5)
        tp = round(price - tp_dist, 5)

    with _lock:
        mode = _learn["mode"]

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       lot,
        "type":         order_type,
        "price":        price,
        "sl":           sl,
        "tp":           tp,
        "magic":        MAGIC,
        "comment":      f"CQ-{mode[:2]}-{(pattern_name or 'XX')[:8]}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        pip = pip_value(symbol)
        print(f"[BOT] {signal} {lot}L @ {price}  SL={sl}({round(sl_dist/pip)}p)  "
              f"TP={tp}({round(tp_dist/pip)}p)  Mode={mode}  Pattern={pattern_name}")
        return True
    code = result.retcode if result else "N/A"
    print(f"[BOT] Order fehlgeschlagen: {code}")
    return False


def has_open_position(symbol):
    if not MT5_AVAILABLE:
        return False
    pos = mt5.positions_get(symbol=symbol)
    return pos is not None and len(pos) > 0


def _modify_sl(pos, new_sl):
    req = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   pos.symbol,
        "position": pos.ticket,
        "sl":       new_sl,
        "tp":       pos.tp,
    }
    result = mt5.order_send(req)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        direction = "BUY" if pos.type == 0 else "SELL"
        print(f"[BE] Break-Even aktiviert: #{pos.ticket} SL → {new_sl:.2f} "
              f"({direction} @ {pos.price_open:.2f})")


def check_breakeven(symbol):
    """
    Break-Even: Wenn offene Position BREAK_EVEN_AT×ATR im Profit ist,
    SL auf Einstiegspreis verschieben → Verlust wird zu ±0.
    """
    if not MT5_AVAILABLE or BREAK_EVEN_AT <= 0:
        return
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return
    atr_v = _state.get("indicators", {}).get("atr", 0)
    if not atr_v or atr_v <= 0:
        return
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return
    be_trigger = atr_v * BREAK_EVEN_AT
    for pos in positions:
        if pos.magic != MAGIC:
            continue
        if pos.type == 0:  # BUY
            profit_pts = tick.bid - pos.price_open
            if profit_pts >= be_trigger and pos.sl < pos.price_open:
                new_sl = round(pos.price_open + atr_v * 0.1, 5)
                _modify_sl(pos, new_sl)
        else:  # SELL
            profit_pts = pos.price_open - tick.ask
            if profit_pts >= be_trigger and (pos.sl > pos.price_open or pos.sl == 0):
                new_sl = round(pos.price_open - atr_v * 0.1, 5)
                _modify_sl(pos, new_sl)


def check_trailing_stop(symbol):
    """
    ATR Trailing Stop — folgt dem Preis in Gewinnrichtung.
    Aktiviert erst wenn TRAIL_ACTIVATE_ATR×ATR Profit erreicht (kein Früh-Stop).
    """
    if not MT5_AVAILABLE or TRAIL_DIST_ATR <= 0:
        return
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return
    atr_v = _state.get("indicators", {}).get("atr", 0)
    if not atr_v or atr_v <= 0:
        return
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return

    trail_dist    = atr_v * TRAIL_DIST_ATR
    activate_dist = atr_v * TRAIL_ACTIVATE_ATR

    for pos in positions:
        if pos.magic != MAGIC:
            continue
        if pos.type == 0:  # BUY: trail up as bid rises
            profit_pts = tick.bid - pos.price_open
            if profit_pts < activate_dist:
                continue
            new_sl = round(tick.bid - trail_dist, 5)
            if new_sl > pos.sl + 0.01:
                _modify_sl(pos, new_sl)
        else:  # SELL: trail down as ask falls
            profit_pts = pos.price_open - tick.ask
            if profit_pts < activate_dist:
                continue
            new_sl = round(tick.ask + trail_dist, 5)
            if pos.sl == 0 or new_sl < pos.sl - 0.01:
                _modify_sl(pos, new_sl)


def check_rsi_emergency_close(symbol):
    """
    Schließt profitable Positionen bei extremen RSI-Werten.
    SELL bei RSI < 20 → Bounce droht → Gewinne sichern.
    BUY bei RSI > 80 → Reversal droht → Gewinne sichern.
    """
    if not MT5_AVAILABLE:
        return
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return
    rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, 30)
    if rates is None or len(rates) < 20:
        return
    closes = [float(r["close"]) for r in rates]
    rsi_v  = rsi(closes, 14)
    tick   = mt5.symbol_info_tick(symbol)
    if not tick:
        return

    for pos in positions:
        if pos.magic != MAGIC:
            continue
        if pos.profit <= 0:
            continue  # Nur profitable Positionen schließen
        should_close = False
        if pos.type == 1 and rsi_v < RSI_CLOSE_SELL:   # SELL, RSI überverkauft
            should_close = True
            reason = f"RSI={rsi_v:.1f} < {RSI_CLOSE_SELL} (Bounce droht)"
        elif pos.type == 0 and rsi_v > RSI_CLOSE_BUY:  # BUY, RSI überkauft
            should_close = True
            reason = f"RSI={rsi_v:.1f} > {RSI_CLOSE_BUY} (Reversal droht)"

        if should_close:
            order_type  = mt5.ORDER_TYPE_BUY  if pos.type == 1 else mt5.ORDER_TYPE_SELL
            close_price = tick.ask if pos.type == 1 else tick.bid
            req = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "position":     pos.ticket,
                "symbol":       symbol,
                "volume":       pos.volume,
                "type":         order_type,
                "price":        close_price,
                "magic":        MAGIC,
                "comment":      "CQ-RSI-CLOSE",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_send(req)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                print(f"[RSI-CLOSE] #{pos.ticket} P&L=+{pos.profit:.2f} | {reason}")


def update_account():
    if not MT5_AVAILABLE:
        return
    info = mt5.account_info()
    if info is None:
        return

    state_set("account", {
        "balance":  round(info.balance, 2),
        "equity":   round(info.equity, 2),
        "profit":   round(info.profit, 2),
        "currency": info.currency,
        "leverage": info.leverage,
        "server":   info.server,
        "name":     info.name,
    })

    positions = mt5.positions_get()
    pos_list  = []
    if positions:
        for p in positions:
            pos_list.append({
                "ticket": p.ticket, "symbol": p.symbol,
                "type":   "BUY" if p.type == 0 else "SELL",
                "volume": p.volume, "open": p.price_open,
                "sl": p.sl, "tp": p.tp, "profit": round(p.profit, 2),
            })
    state_set("positions", pos_list)

    deals = mt5.history_deals_get(
        datetime(2000, 1, 1, tzinfo=timezone.utc),
        datetime.now(timezone.utc)
    )
    if deals:
        our     = [d for d in deals if d.magic == MAGIC and d.profit != 0]
        profits = [d.profit for d in our]
        wins    = [p for p in profits if p > 0]
        losses  = [abs(p) for p in profits if p < 0]

        avg_win  = round(sum(wins)   / len(wins),   2) if wins   else 0.0
        avg_loss = round(sum(losses) / len(losses), 2) if losses else 0.0
        pf       = round(sum(wins)   / sum(losses), 2) if losses and sum(losses) > 0 else 0.0
        ratio    = round(avg_win / avg_loss, 2) if avg_loss > 0 else 0.0

        # Vereinfachter Sharpe aus Trade-P&L
        sharpe = 0.0
        if len(profits) > 1:
            avg_p = sum(profits) / len(profits)
            std_p = (sum((p - avg_p) ** 2 for p in profits) / len(profits)) ** 0.5
            if std_p > 0:
                sharpe = round(avg_p / std_p * (len(profits) ** 0.5) / 10, 2)

        # Max Drawdown aus laufender Equity
        max_dd = 0.0
        running = info.balance - sum(profits)   # annaehernd Startkapital
        peak    = running
        for p in profits:
            running += p
            if running > peak:
                peak = running
            dd = (peak - running) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        total_pct = round(sum(profits) / (info.balance - sum(profits)) * 100, 2) \
                    if (info.balance - sum(profits)) > 0 else 0.0

        state_set("stats", {
            "trades_total":  len(profits),
            "trades_win":    len(wins),
            "win_rate":      round(len(wins) / len(profits) * 100, 1) if profits else 0.0,
            "total_profit":  round(sum(profits), 2),
            "total_pct":     total_pct,
            "biggest_win":   round(max(profits), 2) if profits else 0.0,
            "biggest_loss":  round(min(profits), 2) if profits else 0.0,
            "avg_win":       avg_win,
            "avg_loss":      avg_loss,
            "profit_factor": pf,
            "ratio":         ratio,
            "sharpe":        sharpe,
            "max_drawdown":  round(max_dd, 1),
        })

    tick = mt5.symbol_info_tick(SYMBOL)
    if tick:
        pip = pip_value(SYMBOL)
        state_set("price", {
            "bid":    round(tick.bid, 5),
            "ask":    round(tick.ask, 5),
            "spread": round((tick.ask - tick.bid) / pip, 1),
        })

    with _lock:
        _state["learn"] = json.loads(json.dumps(_learn))

    # ── Tages-Reset: Startbalance für Tages-P&L-Tracking ──────────
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _lock:
        if _risk_state["daily_date"] != today_str:
            _risk_state["daily_date"]          = today_str
            _risk_state["daily_start_balance"] = info.balance
            _risk_state["consec_losses"]        = 0
            print(f"[DAY] Neuer Tag: {today_str} | Start: ${info.balance:.2f}")
        elif _risk_state["daily_start_balance"] == 0.0:
            _risk_state["daily_start_balance"] = info.balance


# ── HTTP API ──────────────────────────────────────────────────────────────────

class APIHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")
        qs     = parse_qs(parsed.query)

        if path == "/data":
            data = json.dumps(state_get(), indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type",   "application/json")
            self.send_header("Content-Length", len(data))
            self._cors()
            self.end_headers()
            self.wfile.write(data)

        elif path == "/mode":
            mode = qs.get("set", ["SELF-LEARN"])[0].upper()
            if mode in ("RANDOM", "KELLY", "SELF-LEARN"):
                with _lock:
                    _learn["mode"] = mode
                    _state["learn"] = json.loads(json.dumps(_learn))
                print(f"[API] Modus gewechselt -> {mode}")
                resp = json.dumps({"ok": True, "mode": mode}).encode()
            else:
                resp = json.dumps({"ok": False, "error": "Unbekannter Modus"}).encode()
            self.send_response(200)
            self.send_header("Content-Type",   "application/json")
            self.send_header("Content-Length", len(resp))
            self._cors()
            self.end_headers()
            self.wfile.write(resp)

        else:
            self.send_response(404)
            self.end_headers()


def run_server():
    server = HTTPServer(("127.0.0.1", SERVER_PORT), APIHandler)
    print(f"[API] http://127.0.0.1:{SERVER_PORT}/data")
    server.serve_forever()


# ── MAIN BOT LOOP ─────────────────────────────────────────────────────────────

def bot_loop():
    if not MT5_AVAILABLE:
        state_set("status", "error: MetaTrader5 nicht installiert")
        state_set("connected", False)
        print("[BOT] pip install MetaTrader5")
        return

    if not mt5.initialize():
        state_set("status", "error: MT5 nicht gestartet")
        state_set("connected", False)
        print("[BOT] MT5 Terminal oeffnen!")
        return

    state_set("connected", True)
    state_set("status", "running")
    print(f"[BOT] Verbunden | {SYMBOL} M15 | v3.0 SELF-LEARN aktiv")

    while True:
        try:
            # ── MT5 Verbindungscheck & Reconnect ─────────────────────────
            if MT5_AVAILABLE and not mt5.terminal_info():
                print("[BOT] MT5 Verbindung unterbrochen — Reconnect...")
                state_set("connected", False)
                reconnected = False
                for attempt in range(5):
                    time.sleep(10 * (attempt + 1))
                    if mt5.initialize():
                        state_set("connected", True)
                        reconnected = True
                        print(f"[BOT] MT5 Reconnect OK (Versuch {attempt+1})")
                        break
                if not reconnected:
                    state_set("status", "error: MT5 Verbindung verloren")
                    time.sleep(60)
                    continue

            update_account()
            check_breakeven(SYMBOL)
            check_trailing_stop(SYMBOL)
            check_rsi_emergency_close(SYMBOL)
            self_adjust()

            # ── Elite Risiko-Guards ───────────────────────────────────────
            with _lock:
                consec    = _risk_state["consec_losses"]
                daily_bal = _risk_state["daily_start_balance"]

            if consec >= MAX_CONSEC_LOSSES:
                print(f"[RISK] {consec} Verluste hintereinander — Pause aktiv!")
                time.sleep(CHECK_EVERY)
                continue

            if DAILY_PROFIT_TARGET > 0 and daily_bal > 0:
                bal_now = _state["account"].get("balance", daily_bal)
                day_pct = (bal_now - daily_bal) / daily_bal * 100
                if day_pct >= DAILY_PROFIT_TARGET:
                    print(f"[RISK] Tages-Ziel +{day_pct:.1f}% erreicht — kein neuer Trade!")
                    time.sleep(CHECK_EVERY)
                    continue

            rates = get_bars(SYMBOL, TIMEFRAME, 250)
            if rates is not None:
                signal, indicators = get_signal(rates)

                pattern = indicators.get("candle_pattern")
                bias    = indicators.get("candle_bias", "neutral")
                adx_v   = indicators.get("adx", 0)
                atr_v   = indicators.get("atr", 0.001)
                rsi_v   = indicators.get("rsi", 50)

                # Letzte 80 Kerzen fuer Dashboard-Chart speichern
                candles_for_chart = [
                    {
                        "o": round(float(r["open"]),  2),
                        "h": round(float(r["high"]),  2),
                        "l": round(float(r["low"]),   2),
                        "c": round(float(r["close"]), 2),
                        "v": int(r["tick_volume"]),
                        "t": int(r["time"]),
                    }
                    for r in rates[-80:]
                ]

                with _lock:
                    _state["last_signal"] = {
                        "signal":   signal,
                        "rsi":      rsi_v,
                        "ema_fast": indicators.get("ema20"),
                        "ema_slow": indicators.get("ema50"),
                        "time":     datetime.now(timezone.utc).isoformat(),
                    }
                    _state["indicators"]     = indicators
                    _state["candle_pattern"] = {"name": pattern, "bias": bias}
                    _state["candles"]        = candles_for_chart
                    mode    = _learn["mode"]
                    adx_min = _learn["adx_threshold"]

                h4    = indicators.get("h4_trend", "?")
                ses   = "SESSION" if indicators.get("session") else "OFF-HOURS"
                stype_v = indicators.get("strategy_type", "BALANCED")
                bsc   = indicators.get("buy_score", 0)
                ssc   = indicators.get("sell_score", 0)
                ms_v  = indicators.get("min_score", 8)
                print(f"[BOT] {mode}/{stype_v} | H4={h4} {ses} | "
                      f"RSI={rsi_v:.1f} ADX={adx_v:.1f} | "
                      f"Score B{bsc}/S{ssc} (min={ms_v}) -> {signal or 'WARTE'}")

                if signal and not has_open_position(SYMBOL):
                    balance = _state["account"].get("balance", 1000)
                    print(f"[BOT] *** {signal} SIGNAL — Order wird gesendet ***")
                    open_position(SYMBOL, signal, balance, atr_v, pattern)

        except Exception as e:
            print(f"[BOT] Fehler: {e}")
            state_set("status", f"error: {e}")

        time.sleep(CHECK_EVERY)


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 65)
    print("  CLAUDE + QUANT  |  Trading Bot v3.0  |  Self-Learning")
    print("=" * 65)
    print(f"  Symbol      : {SYMBOL} M15")
    print(f"  EMA         : {EMA_FAST}/{EMA_MID}/{EMA_SLOW}  |  RSI-{RSI_PERIOD}  |  ADX-{ADX_PERIOD} (Min: {ADX_MIN})")
    print(f"  SL/TP       : ATR x {ATR_SL_MULT} / ATR x {ATR_TP_MULT}")
    print(f"  Indikatoren : Bollinger Bands + Stoch RSI + VWAP + Fibonacci + ICT Order Blocks + FVG")
    print(f"  Confluence  : 13 Indikatoren, mind. 8 Punkte + Session + Volumen fuer Trade")
    print(f"  Modi        : SELF-LEARN (default) | KELLY | RANDOM")
    print(f"  Lernen      : alle {LEARN_EVERY} Trades (ADX, RSI, Patterns)")
    print(f"  API         : http://127.0.0.1:{SERVER_PORT}/data")
    print(f"  Modus API   : http://127.0.0.1:{SERVER_PORT}/mode?set=KELLY")
    print("=" * 65)

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    bot_loop()
