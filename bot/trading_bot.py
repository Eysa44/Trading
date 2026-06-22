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
    "adjustments":     0,
    "last_adjust":     "Noch keine Anpassung",
    "recent_wr":       0.0,
    "recent_trades":   0,
    "kelly_f":         0.0,
    "blocked_patterns": [],
    "pattern_stats":   {},
}
_last_trade_count = 0


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


def state_get():
    with _lock:
        return json.loads(json.dumps(_state))


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


# ── SIGNAL ENGINE ─────────────────────────────────────────────────────────────

def get_bars(symbol, timeframe, n=250):
    if not MT5_AVAILABLE:
        return None
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n)
    if rates is None or len(rates) == 0:
        return None
    return rates


def get_signal(rates):
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

    ef = ema_series(closes, EMA_FAST)
    em = ema_series(closes, EMA_MID)
    es = ema_series(closes, EMA_SLOW)
    rsi_v = rsi(closes[-RSI_PERIOD - 10:], RSI_PERIOD)
    ml, ms, mh = macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    atr_v = atr(list(rates)[-ATR_PERIOD - 5:], ATR_PERIOD)
    adx_v = adx(list(rates)[-ADX_PERIOD * 3:], ADX_PERIOD)
    struct = market_structure(closes[-30:])
    pname, pbias = detect_candle_patterns(list(rates)[-3:])

    indicators = {
        "ema20":           round(ef[-1], 5),
        "ema50":           round(em[-1], 5),
        "ema200":          round(es[-1], 5),
        "rsi":             round(rsi_v, 1),
        "macd":            round(ml[-1], 6),
        "macd_sig":        round(ms[-1], 6),
        "macd_hist":       round(mh[-1], 6),
        "atr":             round(atr_v, 5),
        "adx":             round(adx_v, 1),
        "adx_min":         adx_min,
        "structure":       struct,
        "candle_pattern":  pname,
        "candle_bias":     pbias,
    }

    ema_bull  = ef[-1] > em[-1] > es[-1]
    ema_bear  = ef[-1] < em[-1] < es[-1]
    macd_bull = mh[-1] > 0 and ml[-1] > ms[-1]
    macd_bear = mh[-1] < 0 and ml[-1] < ms[-1]
    blocked_pattern = pname in blocked

    signal = None
    if (ema_bull and adx_v >= adx_min and rsi_lo_buy <= rsi_v <= rsi_hi_buy
            and macd_bull and struct == "bullish"
            and pbias == "bullish" and not blocked_pattern):
        signal = "BUY"
    elif (ema_bear and adx_v >= adx_min and rsi_lo_sell <= rsi_v <= rsi_hi_sell
            and macd_bear and struct == "bearish"
            and pbias == "bearish" and not blocked_pattern):
        signal = "SELL"

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
        our = [d for d in deals if d.magic == MAGIC and d.profit != 0]
        profits = [d.profit for d in our]
        wins    = [p for p in profits if p > 0]
        state_set("stats", {
            "trades_total": len(profits),
            "trades_win":   len(wins),
            "win_rate":     round(len(wins)/len(profits)*100, 1) if profits else 0.0,
            "total_profit": round(sum(profits), 2),
            "biggest_win":  round(max(profits), 2) if profits else 0.0,
            "biggest_loss": round(min(profits), 2) if profits else 0.0,
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
                    mode = _learn["mode"]
                    adx_min = _learn["adx_threshold"]

                print(f"[BOT] {mode} | RSI={rsi_v:.1f} ADX={adx_v:.1f}(min={adx_min}) "
                      f"Kerze={pattern or '--'}({bias}) Struct={indicators.get('structure')} "
                      f"-> {signal or 'WARTE'}")

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
    print(f"  EMA         : {EMA_FAST}/{EMA_MID}/{EMA_SLOW}")
    print(f"  RSI         : {RSI_PERIOD}  |  ADX Start: {ADX_MIN}")
    print(f"  SL/TP       : ATR x {ATR_SL_MULT} / ATR x {ATR_TP_MULT}")
    print(f"  Modi        : SELF-LEARN (default) | KELLY | RANDOM")
    print(f"  Lernen      : alle {LEARN_EVERY} Trades (ADX, RSI, Patterns)")
    print(f"  API         : http://127.0.0.1:{SERVER_PORT}/data")
    print(f"  Modus API   : http://127.0.0.1:{SERVER_PORT}/mode?set=KELLY")
    print("=" * 65)

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    bot_loop()
