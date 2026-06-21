"""
CLAUDE + QUANT Trading Bot
Strategy : EMA 20/50 Crossover + RSI Filter
Symbol   : EURUSD (configurable)
Timeframe: M15
Risk     : 1% per trade, 1:2 RR
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
    print("[WARN] MetaTrader5 package not found. Run: pip install MetaTrader5")

# ── CONFIG ────────────────────────────────────────────────────────────────────
SYMBOL        = "EURUSD"
TIMEFRAME     = 16385          # mt5.TIMEFRAME_M15
LOT           = 0.01           # minimum lot (overridden by risk calc)
RISK_PCT      = 1.0            # % of balance per trade
SL_PIPS       = 20             # stop-loss in pips
TP_PIPS       = 40             # take-profit in pips (1:2 RR)
EMA_FAST      = 20
EMA_SLOW      = 50
RSI_PERIOD    = 14
RSI_OB        = 70             # overbought
RSI_OS        = 30             # oversold
MAGIC         = 234567
CHECK_EVERY   = 15             # seconds between signal checks
SERVER_PORT   = 5000
# ─────────────────────────────────────────────────────────────────────────────


# ── SHARED STATE (thread-safe via lock) ───────────────────────────────────────
_lock = threading.Lock()
_state = {
    "status":      "starting",
    "connected":   False,
    "symbol":      SYMBOL,
    "account": {
        "balance":  0,
        "equity":   0,
        "profit":   0,
        "currency": "USD",
        "leverage": 100,
        "server":   "",
        "name":     "",
    },
    "positions":   [],
    "last_signal": None,
    "stats": {
        "trades_total": 0,
        "trades_win":   0,
        "win_rate":     0.0,
        "total_profit": 0.0,
        "biggest_win":  0.0,
        "biggest_loss": 0.0,
    },
    "price": {
        "bid": 0,
        "ask": 0,
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


# ── INDICATORS ────────────────────────────────────────────────────────────────
def ema(prices, period):
    k = 2 / (period + 1)
    e = prices[0]
    for p in prices[1:]:
        e = p * k + e * (1 - k)
    return e


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
    rs = ag / al
    return 100 - (100 / (1 + rs))


# ── MT5 HELPERS ───────────────────────────────────────────────────────────────
def pip_value(symbol):
    return 0.0001 if "JPY" not in symbol else 0.01


def calc_lot(balance, sl_pips, symbol):
    pip_val = pip_value(symbol)
    risk_amount = balance * (RISK_PCT / 100)
    lot = risk_amount / (sl_pips * pip_val * 100000)
    return round(max(lot, 0.01), 2)


def get_bars(symbol, timeframe, n=100):
    if not MT5_AVAILABLE:
        return None
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n)
    if rates is None or len(rates) == 0:
        return None
    return rates


def get_signal(rates):
    closes = [r["close"] for r in rates]
    if len(closes) < EMA_SLOW + 5:
        return None, 50.0, 0, 0

    ema_f = ema_series(closes, EMA_FAST)
    ema_s = ema_series(closes, EMA_SLOW)
    rsi_v = rsi(closes[-RSI_PERIOD - 5:], RSI_PERIOD)

    cross_up   = ema_f[-2] <= ema_s[-2] and ema_f[-1] > ema_s[-1]
    cross_down = ema_f[-2] >= ema_s[-2] and ema_f[-1] < ema_s[-1]

    signal = None
    if cross_up   and rsi_v < RSI_OB:
        signal = "BUY"
    elif cross_down and rsi_v > RSI_OS:
        signal = "SELL"

    return signal, round(rsi_v, 1), round(ema_f[-1], 5), round(ema_s[-1], 5)


def open_position(symbol, signal, balance):
    if not MT5_AVAILABLE:
        return False
    pip = pip_value(symbol)
    lot = calc_lot(balance, SL_PIPS, symbol)
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False

    if signal == "BUY":
        price = tick.ask
        sl    = round(price - SL_PIPS * pip, 5)
        tp    = round(price + TP_PIPS * pip, 5)
        order_type = mt5.ORDER_TYPE_BUY
    else:
        price = tick.bid
        sl    = round(price + SL_PIPS * pip, 5)
        tp    = round(price - TP_PIPS * pip, 5)
        order_type = mt5.ORDER_TYPE_SELL

    request = {
        "action":    mt5.TRADE_ACTION_DEAL,
        "symbol":    symbol,
        "volume":    lot,
        "type":      order_type,
        "price":     price,
        "sl":        sl,
        "tp":        tp,
        "magic":     MAGIC,
        "comment":   "CLAUDE+QUANT",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"[BOT] {signal} order opened: {lot} lots @ {price}  SL={sl}  TP={tp}")
        return True
    else:
        code = result.retcode if result else "N/A"
        print(f"[BOT] Order failed: {code}")
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

    # Positions
    positions = mt5.positions_get()
    pos_list = []
    if positions:
        for p in positions:
            pos_list.append({
                "ticket":  p.ticket,
                "symbol":  p.symbol,
                "type":    "BUY" if p.type == 0 else "SELL",
                "volume":  p.volume,
                "open":    p.price_open,
                "sl":      p.sl,
                "tp":      p.tp,
                "profit":  round(p.profit, 2),
                "magic":   p.magic,
            })
    state_set("positions", pos_list)

    # Stats from history
    deals = mt5.history_deals_get(
        datetime(2000, 1, 1, tzinfo=timezone.utc),
        datetime.now(timezone.utc)
    )
    if deals:
        our_deals = [d for d in deals if d.magic == MAGIC and d.profit != 0]
        profits = [d.profit for d in our_deals]
        wins    = [p for p in profits if p > 0]
        state_set("stats", {
            "trades_total": len(profits),
            "trades_win":   len(wins),
            "win_rate":     round(len(wins) / len(profits) * 100, 1) if profits else 0.0,
            "total_profit": round(sum(profits), 2),
            "biggest_win":  round(max(profits), 2) if profits else 0.0,
            "biggest_loss": round(min(profits), 2) if profits else 0.0,
        })

    # Tick price
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
        pass  # suppress access logs

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
    print(f"[API] Server running on http://127.0.0.1:{SERVER_PORT}/data")
    server.serve_forever()


# ── MAIN BOT LOOP ─────────────────────────────────────────────────────────────
def bot_loop():
    if not MT5_AVAILABLE:
        state_set("status", "error: MetaTrader5 package not installed")
        state_set("connected", False)
        print("[BOT] MetaTrader5 package not installed.")
        print("[BOT] Install with: pip install MetaTrader5")
        return

    # Connect
    if not mt5.initialize():
        state_set("status", "error: MT5 not running")
        state_set("connected", False)
        print("[BOT] Could not connect to MT5. Make sure the terminal is open.")
        return

    state_set("connected", True)
    state_set("status", "running")
    print(f"[BOT] Connected to MT5  |  Symbol: {SYMBOL}  |  TF: M15")
    print(f"[BOT] Strategy: EMA{EMA_FAST}/{EMA_SLOW} crossover + RSI filter")
    print(f"[BOT] Risk: {RISK_PCT}% per trade  |  SL: {SL_PIPS}p  |  TP: {TP_PIPS}p")

    while True:
        try:
            update_account()

            rates = get_bars(SYMBOL, TIMEFRAME, 120)
            if rates is not None:
                signal, rsi_v, ema_f, ema_s = get_signal(rates)

                with _lock:
                    _state["last_signal"] = {
                        "signal":  signal,
                        "rsi":     rsi_v,
                        "ema_fast": ema_f,
                        "ema_slow": ema_s,
                        "time":    datetime.now(timezone.utc).isoformat(),
                    }

                if signal and not has_open_position(SYMBOL):
                    balance = _state["account"].get("balance", 1000)
                    print(f"[BOT] Signal: {signal}  RSI={rsi_v}  EMA{EMA_FAST}={ema_f}  EMA{EMA_SLOW}={ema_s}")
                    open_position(SYMBOL, signal, balance)

        except Exception as e:
            print(f"[BOT] Error: {e}")
            state_set("status", f"error: {e}")

        time.sleep(CHECK_EVERY)


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  CLAUDE + QUANT  |  Trading Bot v1.0")
    print("=" * 60)
    print(f"  Symbol    : {SYMBOL}")
    print(f"  Timeframe : M15")
    print(f"  Strategy  : EMA{EMA_FAST}/{EMA_SLOW} + RSI{RSI_PERIOD}")
    print(f"  Risk/Trade: {RISK_PCT}%")
    print(f"  Dashboard : http://127.0.0.1:{SERVER_PORT}/data")
    print("=" * 60)

    # Start API server in background
    t = threading.Thread(target=run_server, daemon=True)
    t.start()

    # Start bot
    bot_loop()
