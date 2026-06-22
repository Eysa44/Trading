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

import time
import json
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
ATR_SL_MULT  = 1.5
ATR_TP_MULT  = 3.0
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
    "min_score":       8,   # Confluence-Punkte (optimierbar)
    "adjustments":     0,
    "last_adjust":     "Noch keine Anpassung",
    "recent_wr":       0.0,
    "recent_trades":   0,
    "kelly_f":         0.0,
    "blocked_patterns": [],
    "pattern_stats":   {},
}
_last_trade_count = 0


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
        print(f"[OPT] Optimierte Parameter geladen: ADX={p['adx_threshold']} "
              f"RSI-B={p['rsi_low_buy']}-{p['rsi_high_buy']} "
              f"MinScore={p.get('min_score', 8)}")
    except Exception as e:
        print(f"[WARN] best_params.json konnte nicht geladen werden: {e}")


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


# ── SIGNAL ENGINE ─────────────────────────────────────────────────────────────

def get_bars(symbol, timeframe, n=250):
    if not MT5_AVAILABLE:
        return None
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n)
    if rates is None or len(rates) == 0:
        return None
    return rates


def get_signal(rates):
    """
    Multi-strategy confluence signal engine.
    13 indicators vote — needs 8+ points + session + volume.
    """
    closes = [r["close"] for r in rates]
    if len(closes) < EMA_SLOW + 10:
        return None, {}

    with _lock:
        adx_min     = _learn["adx_threshold"]
        rsi_lo_buy  = _learn["rsi_low_buy"]
        rsi_hi_buy  = _learn["rsi_high_buy"]
        rsi_lo_sell = _learn["rsi_low_sell"]
        rsi_hi_sell = _learn["rsi_high_sell"]
        blocked     = list(_learn["blocked_patterns"])
        min_score   = _learn.get("min_score", 8)

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

    # ── CONFLUENCE SCORING ────────────────────────────────────────────────────
    buy_score = sell_score = 0

    # 1. EMA Triple Alignment (2pts — institutional trend direction)
    if ef[-1] > em[-1] > es[-1]:   buy_score  += 2
    elif ef[-1] < em[-1] < es[-1]: sell_score += 2

    # 2. ADX Strength Gate (1pt — no score if market ranging)
    if adx_v >= adx_min:
        buy_score  += 1
        sell_score += 1

    # 3. RSI Zone (1pt — momentum not extended)
    if rsi_lo_buy  <= rsi_v <= rsi_hi_buy:  buy_score  += 1
    if rsi_lo_sell <= rsi_v <= rsi_hi_sell: sell_score += 1

    # 4. MACD Cross (1pt — momentum confirming)
    if mh[-1] > 0 and ml[-1] > ms_l[-1]:  buy_score  += 1
    if mh[-1] < 0 and ml[-1] < ms_l[-1]:  sell_score += 1

    # 5. Bollinger Bands (2pts bounce, 1pt squeeze)
    if bb_pctb is not None:
        if bb_pctb < 0.2:   buy_score  += 2   # near lower band → oversold bounce
        elif bb_pctb > 0.8: sell_score += 2   # near upper band → overbought reject
        if bb_bw is not None and bb_bw < 1.0:
            buy_score  += 1                   # BB squeeze → explosive move coming
            sell_score += 1

    # 6. Stochastic RSI Cross (2pts — sensitive momentum reversal)
    if stk < 25 and stk > stk_d:   buy_score  += 2
    elif stk > 75 and stk < stk_d: sell_score += 2

    # 7. VWAP Position (1pt — institutional bias)
    if vwap_v:
        if price > vwap_v: buy_score  += 1
        else:              sell_score += 1

    # 8. Fibonacci Key Level (2pts — magnet zones 38.2/50/61.8/78.6)
    if fib and fib["near_key"] and fib["nearest"] in ("38.2","50.0","61.8","78.6"):
        if price >= fib["nearest_price"]: buy_score  += 2
        else:                             sell_score += 2

    # 9. ICT Order Block Zone (2pts — institutional footprint)
    if obs["bullish"]:
        ob = obs["bullish"]
        if ob["low"] <= price <= ob["high"] * 1.0005:
            buy_score += 2
    if obs["bearish"]:
        ob = obs["bearish"]
        if ob["low"] * 0.9995 <= price <= ob["high"]:
            sell_score += 2

    # 10. Fair Value Gap (1pt — price filling imbalance)
    for fg in fvgs["bullish"]:
        if fg["bottom"] <= price <= fg["top"]: buy_score  += 1; break
    for fg in fvgs["bearish"]:
        if fg["bottom"] <= price <= fg["top"]: sell_score += 1; break

    # 11. Market Structure (1pt — higher highs / lower lows)
    if struct == "bullish":    buy_score  += 1
    elif struct == "bearish":  sell_score += 1

    # 12. H4 Higher Timeframe (2pts — most important: trade with HTF)
    if h4_trend == "bullish":  buy_score  += 2
    elif h4_trend == "bearish": sell_score += 2

    # 13. Candlestick Pattern (1pt — timing confirmation)
    BULL_PAT = {"Hammer","Inverted Hammer","Dragonfly Doji","Bullish Engulfing",
                "Piercing Line","Morning Star","Three White Soldiers","Tweezer Bottom"}
    BEAR_PAT = {"Shooting Star","Hanging Man","Gravestone Doji","Bearish Engulfing",
                "Dark Cloud Cover","Evening Star","Three Black Crows","Tweezer Top"}
    if pname and pname not in blocked:
        if pname in BULL_PAT:  buy_score  += 1
        if pname in BEAR_PAT:  sell_score += 1

    # ── DECISION: session + volume gates + optimized confluence threshold ────
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
    }

    return signal, indicators


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
            update_account()
            self_adjust()

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

                h4  = indicators.get("h4_trend", "?")
                ses = "SESSION" if indicators.get("session") else "OFF-HOURS"
                bbs = f"BB%B={indicators.get('bb_pctb','?')}"
                stk_v = indicators.get("stoch_k", 0)
                bsc = indicators.get("buy_score", 0)
                ssc = indicators.get("sell_score", 0)
                print(f"[BOT] {mode} | H4={h4} {ses} | RSI={rsi_v:.1f} ADX={adx_v:.1f} "
                      f"StochRSI={stk_v} {bbs} | Score B{bsc}/S{ssc} -> {signal or 'WARTE'}")

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
