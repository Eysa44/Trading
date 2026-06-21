"""
CLAUDE + QUANT Trading Bot  v2.0
═══════════════════════════════════════════════════════════════
STRATEGY STACK (Elite-Level):
  Trend     : EMA 20 / 50 / 200  +  ADX > 25 (nur starke Trends)
  Momentum  : RSI 14  +  MACD (12/26/9)
  Volatility: ATR-14 basierter Stop-Loss (dynamisch, kein fixer Pip-Wert)
  Confirm   : Candlestick Pattern Recognition (18 Patterns)
  Structure : Market Structure (HH/HL für Long, LH/LL für Short)
  Risk      : 1% per Trade, ATR × 1.5 SL, ATR × 3.0 TP (1:2 RR)

CANDLESTICK PATTERNS erkannt:
  Bullish: Hammer, Inverted Hammer, Bullish Engulfing, Morning Star,
           Dragonfly Doji, Three White Soldiers, Piercing Line, Tweezer Bottom
  Bearish: Shooting Star, Hanging Man, Bearish Engulfing, Evening Star,
           Gravestone Doji, Three Black Crows, Dark Cloud Cover, Tweezer Top
  Neutral: Doji, Spinning Top

EINSTIEGS-LOGIK (ALLE Bedingungen müssen erfüllt sein):
  BUY  → EMA20 > EMA50 > EMA200  AND  ADX > 25  AND  RSI 40-65
         AND  MACD bullish  AND  Market Structure bullish
         AND  Bullish Candle Pattern
  SELL → EMA20 < EMA50 < EMA200  AND  ADX > 25  AND  RSI 35-60
         AND  MACD bearish  AND  Market Structure bearish
         AND  Bearish Candle Pattern
═══════════════════════════════════════════════════════════════
"""

import time
import json
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    print("[WARN] MetaTrader5 nicht gefunden. Ausführen: pip install MetaTrader5")

# ── CONFIG ────────────────────────────────────────────────────────────────────
SYMBOL        = "EURUSD"
TIMEFRAME     = 16385      # mt5.TIMEFRAME_M15
RISK_PCT      = 1.0        # % des Kontos pro Trade
ATR_SL_MULT   = 1.5        # Stop-Loss = ATR × 1.5
ATR_TP_MULT   = 3.0        # Take-Profit = ATR × 3.0  (1:2 RR)
EMA_FAST      = 20
EMA_MID       = 50
EMA_SLOW      = 200
MACD_FAST     = 12
MACD_SLOW     = 26
MACD_SIGNAL   = 9
RSI_PERIOD    = 14
ADX_PERIOD    = 14
ADX_MIN       = 25         # Mindest-Trendstärke
ATR_PERIOD    = 14
MAGIC         = 234567
CHECK_EVERY   = 15         # Sekunden zwischen Signal-Checks
SERVER_PORT   = 5000
# ─────────────────────────────────────────────────────────────────────────────


# ── SHARED STATE ──────────────────────────────────────────────────────────────
_lock = threading.Lock()
_state = {
    "status":       "starting",
    "connected":    False,
    "symbol":       SYMBOL,
    "account": {
        "balance":  0,
        "equity":   0,
        "profit":   0,
        "currency": "USD",
        "leverage": 100,
        "server":   "",
        "name":     "",
    },
    "positions":    [],
    "last_signal":  None,
    "indicators":   {},
    "candle_pattern": None,
    "stats": {
        "trades_total": 0,
        "trades_win":   0,
        "win_rate":     0.0,
        "total_profit": 0.0,
        "biggest_win":  0.0,
        "biggest_loss": 0.0,
    },
    "price": {
        "bid":    0,
        "ask":    0,
        "spread": 0,
    },
    "last_update": "",
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
    ema_f = ema_series(closes, fast)
    ema_s = ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(ema_f, ema_s)]
    signal_line = ema_series(macd_line, signal)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, histogram


def atr(rates, period=14):
    trs = []
    for i in range(1, len(rates)):
        h = rates[i]["high"]
        l = rates[i]["low"]
        pc = rates[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return trs[-1] if trs else 0.001
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val


def adx(rates, period=14):
    """Average Directional Index — misst Trendstärke (0-100)."""
    if len(rates) < period + 2:
        return 0.0
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(rates)):
        h, l, ph, pl = rates[i]["high"], rates[i]["low"], rates[i-1]["high"], rates[i-1]["low"]
        pc = rates[i-1]["close"]
        up   = h - ph
        down = pl - l
        plus_dm.append(up   if up > down and up > 0   else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    def smooth(arr, p):
        s = sum(arr[:p])
        result = [s]
        for v in arr[p:]:
            s = s - s / p + v
            result.append(s)
        return result

    tr_s   = smooth(trs, period)
    pdm_s  = smooth(plus_dm, period)
    mdm_s  = smooth(minus_dm, period)

    pdi = [100 * p / t if t else 0 for p, t in zip(pdm_s, tr_s)]
    mdi = [100 * m / t if t else 0 for m, t in zip(mdm_s, tr_s)]
    dx  = [100 * abs(p - m) / (p + m) if (p + m) else 0 for p, m in zip(pdi, mdi)]

    if len(dx) < period:
        return sum(dx) / len(dx)
    adx_val = sum(dx[:period]) / period
    for d in dx[period:]:
        adx_val = (adx_val * (period - 1) + d) / period
    return adx_val


def market_structure(closes, lookback=10):
    """
    Bestimmt ob Marktstruktur bullisch oder bearisch ist.
    Bullisch = Higher Highs + Higher Lows
    Bearisch = Lower Highs + Lower Lows
    """
    if len(closes) < lookback * 2:
        return "neutral"
    mid = len(closes) // 2
    first_half  = closes[:mid]
    second_half = closes[mid:]
    prev_high = max(first_half)
    prev_low  = min(first_half)
    curr_high = max(second_half)
    curr_low  = min(second_half)
    if curr_high > prev_high and curr_low > prev_low:
        return "bullish"
    if curr_high < prev_high and curr_low < prev_low:
        return "bearish"
    return "neutral"


# ── CANDLESTICK PATTERN ENGINE ────────────────────────────────────────────────

def candle_body(c):
    return abs(c["close"] - c["open"])

def candle_range(c):
    return c["high"] - c["low"] or 0.00001

def upper_shadow(c):
    return c["high"] - max(c["open"], c["close"])

def lower_shadow(c):
    return min(c["open"], c["close"]) - c["low"]

def is_bullish(c):
    return c["close"] > c["open"]

def is_bearish(c):
    return c["close"] < c["open"]


def detect_candle_patterns(rates):
    """
    Erkennt 18 Candlestick-Patterns auf den letzten 3 Kerzen.
    Gibt zurück: (pattern_name, bias)  bias = 'bullish' | 'bearish' | 'neutral'
    """
    if len(rates) < 3:
        return None, "neutral"

    c0 = rates[-1]   # aktuelle Kerze
    c1 = rates[-2]   # vorherige Kerze
    c2 = rates[-3]   # zwei Kerzen zurück

    body0  = candle_body(c0)
    body1  = candle_body(c1)
    range0 = candle_range(c0)
    range1 = candle_range(c1)
    us0    = upper_shadow(c0)
    ls0    = lower_shadow(c0)
    us1    = upper_shadow(c1)
    ls1    = lower_shadow(c1)

    avg_body = (candle_body(c0) + candle_body(c1) + candle_body(c2)) / 3

    # ── BULLISCHE PATTERNS ────────────────────────────────────────────────────

    # Hammer: kleiner Body oben, langer unterer Schatten, wenig oberer Schatten
    if (is_bullish(c0) and
            ls0 >= body0 * 2 and
            us0 <= body0 * 0.3 and
            body0 < range0 * 0.4):
        return "Hammer", "bullish"

    # Inverted Hammer: kleiner Body unten, langer oberer Schatten
    if (is_bullish(c0) and
            us0 >= body0 * 2 and
            ls0 <= body0 * 0.3 and
            body0 < range0 * 0.4):
        return "Inverted Hammer", "bullish"

    # Dragonfly Doji: sehr kleiner Body, langer unterer Schatten
    if (body0 <= range0 * 0.05 and
            ls0 >= range0 * 0.6 and
            us0 <= range0 * 0.1):
        return "Dragonfly Doji", "bullish"

    # Bullish Engulfing: vorherige Kerze bearish, aktuelle bullisch und größer
    if (is_bearish(c1) and is_bullish(c0) and
            c0["open"] < c1["close"] and
            c0["close"] > c1["open"] and
            body0 > body1):
        return "Bullish Engulfing", "bullish"

    # Piercing Line: bearische Kerze, dann bullische die über 50% der c1-Body schließt
    if (is_bearish(c1) and is_bullish(c0) and
            c0["open"] < c1["low"] and
            c0["close"] > (c1["open"] + c1["close"]) / 2 and
            c0["close"] < c1["open"]):
        return "Piercing Line", "bullish"

    # Morning Star: bearisch → kleiner Körper (Stern) → bullisch > 50% c2-Body
    if (is_bearish(c2) and
            candle_body(c1) < avg_body * 0.3 and
            is_bullish(c0) and
            c0["close"] > (c2["open"] + c2["close"]) / 2):
        return "Morning Star", "bullish"

    # Three White Soldiers: 3 steigende bullische Kerzen mit kleinen Schatten
    if (is_bullish(c0) and is_bullish(c1) and is_bullish(c2) and
            c0["close"] > c1["close"] > c2["close"] and
            c0["open"] > c1["open"] > c2["open"] and
            us0 < body0 * 0.3 and us1 < body1 * 0.3):
        return "Three White Soldiers", "bullish"

    # Tweezer Bottom: beide Kerzen haben fast gleiches Tief
    if (is_bearish(c1) and is_bullish(c0) and
            abs(c0["low"] - c1["low"]) < range0 * 0.05):
        return "Tweezer Bottom", "bullish"

    # ── BEARISCHE PATTERNS ────────────────────────────────────────────────────

    # Shooting Star: kleiner Body unten, langer oberer Schatten
    if (is_bearish(c0) and
            us0 >= body0 * 2 and
            ls0 <= body0 * 0.3 and
            body0 < range0 * 0.4):
        return "Shooting Star", "bearish"

    # Hanging Man: wie Hammer aber in Aufwärtstrend (bearisch)
    if (is_bearish(c0) and
            ls0 >= body0 * 2 and
            us0 <= body0 * 0.3 and
            body0 < range0 * 0.4 and
            is_bullish(c1)):
        return "Hanging Man", "bearish"

    # Gravestone Doji: sehr kleiner Body oben, langer oberer Schatten
    if (body0 <= range0 * 0.05 and
            us0 >= range0 * 0.6 and
            ls0 <= range0 * 0.1):
        return "Gravestone Doji", "bearish"

    # Bearish Engulfing: vorherige bullisch, aktuelle bearisch und größer
    if (is_bullish(c1) and is_bearish(c0) and
            c0["open"] > c1["close"] and
            c0["close"] < c1["open"] and
            body0 > body1):
        return "Bearish Engulfing", "bearish"

    # Dark Cloud Cover: bullische Kerze, dann bearische die unter 50% schließt
    if (is_bullish(c1) and is_bearish(c0) and
            c0["open"] > c1["high"] and
            c0["close"] < (c1["open"] + c1["close"]) / 2 and
            c0["close"] > c1["open"]):
        return "Dark Cloud Cover", "bearish"

    # Evening Star: bullisch → kleiner Stern → bearisch > 50% c2-Body
    if (is_bullish(c2) and
            candle_body(c1) < avg_body * 0.3 and
            is_bearish(c0) and
            c0["close"] < (c2["open"] + c2["close"]) / 2):
        return "Evening Star", "bearish"

    # Three Black Crows: 3 fallende bearische Kerzen
    if (is_bearish(c0) and is_bearish(c1) and is_bearish(c2) and
            c0["close"] < c1["close"] < c2["close"] and
            c0["open"] < c1["open"] < c2["open"] and
            ls0 < body0 * 0.3 and ls1 < body1 * 0.3):
        return "Three Black Crows", "bearish"

    # Tweezer Top: beide Kerzen haben fast gleiches Hoch
    if (is_bullish(c1) and is_bearish(c0) and
            abs(c0["high"] - c1["high"]) < range0 * 0.05):
        return "Tweezer Top", "bearish"

    # ── NEUTRALE PATTERNS ─────────────────────────────────────────────────────

    # Doji: Open ≈ Close
    if body0 <= range0 * 0.05:
        return "Doji", "neutral"

    # Spinning Top: kleiner Body, beide Schatten lang
    if (body0 < avg_body * 0.3 and
            us0 > body0 and ls0 > body0):
        return "Spinning Top", "neutral"

    return None, "neutral"


# ── SIGNAL ENGINE ─────────────────────────────────────────────────────────────

def pip_value(symbol):
    return 0.0001 if "JPY" not in symbol else 0.01


def calc_lot(balance, sl_price_dist, symbol):
    """ATR-basiertes Position Sizing — 1% Risiko."""
    risk_amount = balance * (RISK_PCT / 100)
    lot = risk_amount / (sl_price_dist * 100000)
    return round(max(lot, 0.01), 2)


def get_bars(symbol, timeframe, n=250):
    if not MT5_AVAILABLE:
        return None
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n)
    if rates is None or len(rates) == 0:
        return None
    return rates


def get_signal(rates):
    """
    Elite-Signal-Engine: alle Bedingungen müssen grün sein.
    Gibt zurück: (signal, indicators_dict)
    """
    closes = [r["close"] for r in rates]
    highs  = [r["high"]  for r in rates]
    lows   = [r["low"]   for r in rates]

    if len(closes) < EMA_SLOW + 10:
        return None, {}

    # Indikatoren berechnen
    ema_f  = ema_series(closes, EMA_FAST)
    ema_m  = ema_series(closes, EMA_MID)
    ema_s  = ema_series(closes, EMA_SLOW)
    rsi_v  = rsi(closes[-RSI_PERIOD - 10:], RSI_PERIOD)
    macd_l, macd_sig, macd_hist = macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    atr_v  = atr(list(rates)[-ATR_PERIOD - 5:], ATR_PERIOD)
    adx_v  = adx(list(rates)[-ADX_PERIOD * 3:], ADX_PERIOD)
    struct = market_structure(closes[-30:])

    # Candlestick Pattern
    pattern_name, pattern_bias = detect_candle_patterns(list(rates)[-3:])

    indicators = {
        "ema20":    round(ema_f[-1], 5),
        "ema50":    round(ema_m[-1], 5),
        "ema200":   round(ema_s[-1], 5),
        "rsi":      round(rsi_v, 1),
        "macd":     round(macd_l[-1], 6),
        "macd_sig": round(macd_sig[-1], 6),
        "macd_hist":round(macd_hist[-1], 6),
        "atr":      round(atr_v, 5),
        "adx":      round(adx_v, 1),
        "structure": struct,
        "candle_pattern": pattern_name,
        "candle_bias":    pattern_bias,
    }

    # ── EMA Trend-Ausrichtung
    ema_bull = ema_f[-1] > ema_m[-1] > ema_s[-1]
    ema_bear = ema_f[-1] < ema_m[-1] < ema_s[-1]

    # ── MACD
    macd_bull = macd_hist[-1] > 0 and macd_l[-1] > macd_sig[-1]
    macd_bear = macd_hist[-1] < 0 and macd_l[-1] < macd_sig[-1]

    # ── Signal entscheiden (ALLE Bedingungen erforderlich)
    signal = None

    if (ema_bull and
            adx_v >= ADX_MIN and
            40 <= rsi_v <= 65 and
            macd_bull and
            struct == "bullish" and
            pattern_bias == "bullish"):
        signal = "BUY"

    elif (ema_bear and
            adx_v >= ADX_MIN and
            35 <= rsi_v <= 60 and
            macd_bear and
            struct == "bearish" and
            pattern_bias == "bearish"):
        signal = "SELL"

    return signal, indicators


def open_position(symbol, signal, balance, atr_v):
    if not MT5_AVAILABLE:
        return False
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False

    sl_dist = atr_v * ATR_SL_MULT
    tp_dist = atr_v * ATR_TP_MULT
    lot     = calc_lot(balance, sl_dist, symbol)

    if signal == "BUY":
        price = tick.ask
        sl    = round(price - sl_dist, 5)
        tp    = round(price + tp_dist, 5)
        order_type = mt5.ORDER_TYPE_BUY
    else:
        price = tick.bid
        sl    = round(price + sl_dist, 5)
        tp    = round(price - tp_dist, 5)
        order_type = mt5.ORDER_TYPE_SELL

    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       lot,
        "type":         order_type,
        "price":        price,
        "sl":           sl,
        "tp":           tp,
        "magic":        MAGIC,
        "comment":      "CLAUDE+QUANT v2",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        pip = pip_value(symbol)
        sl_pips = round(sl_dist / pip)
        tp_pips = round(tp_dist / pip)
        print(f"[BOT] {signal} eröffnet: {lot} Lots @ {price}  SL={sl}({sl_pips}p)  TP={tp}({tp_pips}p)")
        return True
    else:
        code = result.retcode if result else "N/A"
        print(f"[BOT] Order fehlgeschlagen: {code}")
        return False


def has_open_position(symbol):
    if not MT5_AVAILABLE:
        return False
    positions = mt5.positions_get(symbol=symbol)
    return positions is not None and len(positions) > 0


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
    pos_list = []
    if positions:
        for p in positions:
            pos_list.append({
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type":   "BUY" if p.type == 0 else "SELL",
                "volume": p.volume,
                "open":   p.price_open,
                "sl":     p.sl,
                "tp":     p.tp,
                "profit": round(p.profit, 2),
                "magic":  p.magic,
            })
    state_set("positions", pos_list)

    deals = mt5.history_deals_get(
        datetime(2000, 1, 1, tzinfo=timezone.utc),
        datetime.now(timezone.utc)
    )
    if deals:
        our_deals = [d for d in deals if d.magic == MAGIC and d.profit != 0]
        profits   = [d.profit for d in our_deals]
        wins      = [p for p in profits if p > 0]
        state_set("stats", {
            "trades_total": len(profits),
            "trades_win":   len(wins),
            "win_rate":     round(len(wins) / len(profits) * 100, 1) if profits else 0.0,
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


# ── LOCAL HTTP API SERVER ──────────────────────────────────────────────────────
class APIHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path in ("/data", "/data/"):
            data = json.dumps(state_get(), indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type",  "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()


def run_server():
    server = HTTPServer(("127.0.0.1", SERVER_PORT), APIHandler)
    print(f"[API] Server läuft auf http://127.0.0.1:{SERVER_PORT}/data")
    server.serve_forever()


# ── MAIN BOT LOOP ─────────────────────────────────────────────────────────────
def bot_loop():
    if not MT5_AVAILABLE:
        state_set("status", "error: MetaTrader5 nicht installiert")
        state_set("connected", False)
        print("[BOT] MetaTrader5 nicht installiert.")
        print("[BOT] Installieren mit: pip install MetaTrader5")
        return

    if not mt5.initialize():
        state_set("status", "error: MT5 nicht gestartet")
        state_set("connected", False)
        print("[BOT] Konnte nicht mit MT5 verbinden. Ist der Terminal geöffnet?")
        return

    state_set("connected", True)
    state_set("status", "running")
    print(f"[BOT] Verbunden mit MT5  |  Symbol: {SYMBOL}  |  TF: M15")
    print(f"[BOT] Strategie: EMA{EMA_FAST}/{EMA_MID}/{EMA_SLOW} + MACD + RSI + ADX + Candlestick Patterns")
    print(f"[BOT] Risiko: {RISK_PCT}% pro Trade  |  SL: ATR×{ATR_SL_MULT}  |  TP: ATR×{ATR_TP_MULT}")

    while True:
        try:
            update_account()

            rates = get_bars(SYMBOL, TIMEFRAME, 250)
            if rates is not None:
                signal, indicators = get_signal(rates)

                pattern = indicators.get("candle_pattern")
                bias    = indicators.get("candle_bias", "neutral")
                adx_v   = indicators.get("adx", 0)
                atr_v   = indicators.get("atr", 0.001)
                rsi_v   = indicators.get("rsi", 50)

                with _lock:
                    _state["last_signal"]    = {
                        "signal": signal,
                        "rsi":    rsi_v,
                        "ema_fast":  indicators.get("ema20"),
                        "ema_slow":  indicators.get("ema50"),
                        "time":   datetime.now(timezone.utc).isoformat(),
                    }
                    _state["indicators"]     = indicators
                    _state["candle_pattern"] = {
                        "name": pattern,
                        "bias": bias,
                    }

                status_line = (
                    f"RSI={rsi_v:.1f}  ADX={adx_v:.1f}  "
                    f"Kerze={pattern or 'keins'}({bias})  "
                    f"Struktur={indicators.get('structure')}  "
                    f"Signal={signal or 'WARTE'}"
                )
                print(f"[BOT] {status_line}")

                if signal and not has_open_position(SYMBOL):
                    balance = _state["account"].get("balance", 1000)
                    print(f"[BOT] *** {signal} SIGNAL BESTÄTIGT — öffne Position ***")
                    open_position(SYMBOL, signal, balance, atr_v)

        except Exception as e:
            print(f"[BOT] Fehler: {e}")
            state_set("status", f"error: {e}")

        time.sleep(CHECK_EVERY)


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 65)
    print("  CLAUDE + QUANT  |  Trading Bot v2.0  |  Elite Strategy Stack")
    print("=" * 65)
    print(f"  Symbol      : {SYMBOL}")
    print(f"  Timeframe   : M15")
    print(f"  EMA Trend   : {EMA_FAST}/{EMA_MID}/{EMA_SLOW}")
    print(f"  Momentum    : RSI-{RSI_PERIOD}  +  MACD {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL}")
    print(f"  Trendstärke : ADX-{ADX_PERIOD} (min. {ADX_MIN})")
    print(f"  Stop-Loss   : ATR-{ATR_PERIOD} × {ATR_SL_MULT}")
    print(f"  Take-Profit : ATR-{ATR_PERIOD} × {ATR_TP_MULT}")
    print(f"  Kerzen      : 18 Patterns (Hammer, Engulfing, Doji, ...)")
    print(f"  Risiko      : {RISK_PCT}% pro Trade")
    print(f"  Dashboard   : http://127.0.0.1:{SERVER_PORT}/data")
    print("=" * 65)

    t = threading.Thread(target=run_server, daemon=True)
    t.start()

    bot_loop()
