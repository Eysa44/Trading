//+------------------------------------------------------------------+
//|  CLAUDE QUANT ELITE v2.0  —  XAUUSD M15                        |
//|  Multi-Timeframe Confluence Trading System                       |
//|  Strategie : VOLUME_CONF           Auto-Optimiert              |
//|  Python-Backtest: WR=50.0%  Return=+23.1%  Max DD=5.9%   |
//|  Profit Factor: 1.91   Trades: 46                                   |
//|  Kapital: $10000 -> $12305.06 (+$2305.06)               |
//+------------------------------------------------------------------+
//  FEATURES:
//  - H4 Trend-Filter (EMA50/200) — nur in H4-Trendrichtung handeln
//  - H1 Momentum-Filter (EMA20/50, RSI, MACD) — Bias-Bestätigung
//  - M15 Entry mit 10-Faktor Confluence-Score
//  - Doppeltes TP: TP1=1.5R (50% schließen) + TP2=4.0R (Runner)
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

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>

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

//── SESSION & WOCHENTAG ───────────────────────────────────────────
input string  _Sec1           = "══ SESSION ══";
input int     InpSessionStart = 7;    // Session Start UTC (London Open)
input int     InpSessionEnd   = 20;   // Session End UTC (NY Close)
input bool    InpSkipMonday   = true;   // Montag 7-10 UTC überspringen (schwache Liquidität)
input bool    InpSkipFriday   = true;   // Freitag ab 17 UTC überspringen (Weekend-Gap Risiko)
input int     InpConfirmBars  = 2;    // Signal-Bestätigung (1=sofort, 2=2 Kerzen hintereinander)

//── MULTI-TIMEFRAME ───────────────────────────────────────────────
input string  _Sec2           = "══ MULTI-TIMEFRAME ══";
input bool    InpUseH4Filter  = true;  // H4 Trend-Filter aktiv
input bool    InpUseH1Filter  = true;  // H1 Momentum-Filter aktiv
input int     InpH4EMA        = 50;    // H4 Haupt-EMA Periode

//── ENTRY PARAMETER (Optimizer-Ergebnis) ──────────────────────────
input string  _Sec3           = "══ ENTRY (VOLUME_CONF) ══";
input int     InpADXMin       = 20;   // ADX Minimum Trend-Stärke
input int     InpRSILowBuy    = 40;    // RSI Kauf-Zone Untergrenze
input int     InpRSIHighBuy   = 62;    // RSI Kauf-Zone Obergrenze
input int     InpRSILowSell   = 62;    // RSI Verkauf-Zone Untergrenze
input int     InpRSIHighSell  = 77;    // RSI Verkauf-Zone Obergrenze
input int     InpMinScore     = 7;  // Min. Confluence Score
input double  InpVolMinRatio  = 0.80;        // Volumen-Min. vs 20-Kerzen Ø

//── TRADE MANAGEMENT ──────────────────────────────────────────────
input string  _Sec4           = "══ TRADE MANAGEMENT ══";
input double  InpSLMult       = 2.0;   // Stop Loss (ATR x)
input double  InpTP1Mult      = 1.5;  // Take Profit 1 (ATR x) — 50% sofort sichern
input bool    InpTP2NoLimit   = true;        // TP2 ohne fixes Ziel (nur ATR-Trail) — Elite-Modus
input double  InpTP2Mult      = 4.0;  // Take Profit 2 fix (wenn InpTP2NoLimit=false)
input double  InpBEAt         = 0.0;     // Break-Even nach TP1 (ATR x, 0=aus)
input bool    InpTrailing     = true;        // ATR Trailing Stop aktiv
input double  InpTrailMult    = 1.2;         // Trailing Stop Abstand (ATR x)
input double  InpTrailActivate= 0.5;         // Trail startet ab X*ATR Gewinn (vermeidet Früh-Stop)
// RSI-Override: extrem überverkauft/überkauft → Gewinne sofort sichern vor Bounce
input int     InpRSICloseSell = 20;          // SELL schließen wenn RSI unter diesen Wert fällt
input int     InpRSICloseBuy  = 80;          // BUY schließen wenn RSI über diesen Wert steigt

//── CONFLUENCE GEWICHTUNGEN (VOLUME_CONF) ─────────────────────────────
input string  _Sec5           = "══ GEWICHTUNGEN ══";
input int     W_EMA           = 2;   // EMA-Trend Gewicht (M15)
input int     W_ADX           = 2;   // ADX-Stärke Gewicht
input int     W_RSI           = 2;   // RSI Zone Gewicht
input int     W_MACD          = 2;  // MACD Gewicht
input int     W_BB            = 1;    // Bollinger Bands Gewicht
input int     W_STOCH         = 1; // Stochastic Gewicht
input int     W_CCI           = 2;   // CCI Momentum Gewicht
input int     W_VOL           = 5;   // Volumen Bestätigung Gewicht

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

//══════════════════════════════════════════════════════════════════
//  OnInit
//══════════════════════════════════════════════════════════════════
int OnInit()
  {
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
     {
      Alert("FEHLER: Indikator-Handle ungueltig! Pruefe Symbol/Timeframe.");
      return INIT_FAILED;
     }

   g_DayStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
   g_DayStart        = TimeCurrent();

   Print("CLAUDE QUANT ELITE v2.0 | Strategie: VOLUME_CONF | WR=50.0% | Return=+23.1%");
   Print("H4-Filter: ", InpUseH4Filter ? "AN" : "AUS",
         " | H1-Filter: ", InpUseH1Filter ? "AN" : "AUS",
         " | Session: ", InpSessionStart, "-", InpSessionEnd, " UTC");
   Print("SL=", InpSLMult, "R | TP1=", InpTP1Mult, "R | TP2=", InpTP2Mult, "R | BE=", InpBEAt, "R");

   return INIT_SUCCEEDED;
  }

//══════════════════════════════════════════════════════════════════
//  OnDeinit
//══════════════════════════════════════════════════════════════════
void OnDeinit(const int reason)
  {
   int h[] = {hM_ATR, hM_RSI, hM_ADX, hM_EMA20, hM_EMA50, hM_EMA200,
               hM_MACD, hM_BB, hM_STOCH, hM_CCI,
               hH1_EMA20, hH1_EMA50, hH1_RSI, hH1_MACD,
               hH4_EMA50, hH4_EMA200, hH4_ADX};
   for(int i = 0; i < ArraySize(h); i++)
      if(h[i] != INVALID_HANDLE) IndicatorRelease(h[i]);
   Comment("");
  }

//══════════════════════════════════════════════════════════════════
//  OnTick — Haupt-Logik
//══════════════════════════════════════════════════════════════════
void OnTick()
  {
   //── Tages-Reset ──────────────────────────────────────────────
   MqlDateTime now_dt, day_dt;
   TimeToStruct(TimeCurrent(),  now_dt);
   TimeToStruct(g_DayStart,     day_dt);
   if(now_dt.day != day_dt.day)
     {
      g_DayStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
      g_DayStart        = TimeCurrent();
      g_DailyTrades     = 0;  // Tages-Zähler zurücksetzen
     }

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
     {
      double day_dd = (g_DayStartBalance - balance) / g_DayStartBalance * 100.0;
      if(day_dd >= InpMaxDailyDD) return;
     }

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

   //── Signal holen ─────────────────────────────────────────────
   int sig = GetSignal();
   if(sig == 0) return;

   //── Trade öffnen ─────────────────────────────────────────────
   OpenTrade(sig, atr);
  }

//══════════════════════════════════════════════════════════════════
//  GetSignal — Multi-Timeframe Confluence
//══════════════════════════════════════════════════════════════════
int GetSignal()
  {
   //── H4 Trend-Filter ──────────────────────────────────────────
   int h4_bias = 0;
   if(InpUseH4Filter)
     {
      double h4e50  = Buf(hH4_EMA50,  0, 1);
      double h4e200 = Buf(hH4_EMA200, 0, 1);
      double h4adx  = Buf(hH4_ADX,    0, 1);
      double h4c    = iClose(_Symbol, PERIOD_H4, 1);

      if(h4e50 > 0 && h4e200 > 0 && h4c > 0)
        {
         if(h4e50 > h4e200 && h4c > h4e50)  h4_bias =  1;
         if(h4e50 < h4e200 && h4c < h4e50)  h4_bias = -1;
        }
      if(h4adx < 15) return 0;  // H4 seitwärts → kein Trade
     }

   //── H1 Momentum-Filter ───────────────────────────────────────
   int h1_bias = 0;
   if(InpUseH1Filter)
     {
      double h1e20  = Buf(hH1_EMA20, 0, 1);
      double h1e50  = Buf(hH1_EMA50, 0, 1);
      double h1rsi  = Buf(hH1_RSI,   0, 1);
      double h1ml   = Buf(hH1_MACD,  0, 1);
      double h1ms   = Buf(hH1_MACD,  1, 1);

      if(h1e20 > h1e50) h1_bias++;   else h1_bias--;
      if(h1ml  > h1ms)  h1_bias++;   else h1_bias--;
      if(h1rsi > 50 && h1rsi < 80) h1_bias++;
      if(h1rsi < 50 && h1rsi > 20) h1_bias--;
     }

   // H4 und H1 dürfen nicht in entgegengesetzte Richtungen zeigen
   if(InpUseH4Filter && InpUseH1Filter)
     {
      if(h4_bias ==  1 && h1_bias <= -1) return 0;
      if(h4_bias == -1 && h1_bias >=  1) return 0;
     }

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
     {
      if(macdl > macds) bs += W_ADX;
      else              ss += W_ADX;
     }

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
     {
      if(bull_c) bs += W_BB / 2;
      if(bear_c) ss += W_BB / 2;
     }

   //── 6. Stochastic ────────────────────────────────────────────
   if(stk < 25 && stk > stk_d) bs += W_STOCH;
   if(stk > 75 && stk < stk_d) ss += W_STOCH;

   //── 7. CCI Momentum ──────────────────────────────────────────
   if(W_CCI > 0)
     {
      if(cci >  100) bs += W_CCI;
      if(cci < -100) ss += W_CCI;
     }

   //── 8. Volumen-Bestätigung ────────────────────────────────────
   if(W_VOL > 0)
     {
      if(bull_c) bs += W_VOL;
      if(bear_c) ss += W_VOL;
     }

   //── 9. H1 Bias Bonus ─────────────────────────────────────────
   if(InpUseH1Filter)
     {
      if(h1_bias >= 2) bs += 2;
      if(h1_bias <= -2) ss += 2;
     }

   //── 10. H4 Trend Bonus (stärkster Filter) ────────────────────
   if(InpUseH4Filter)
     {
      if(h4_bias ==  1) bs += 3;
      if(h4_bias == -1) ss += 3;
     }

   //── Entscheidung + 2-Kerzen-Bestätigung ─────────────────────
   int raw_sig = 0;
   if(bs >= InpMinScore && bs > ss + 2) raw_sig =  1;
   if(ss >= InpMinScore && ss > bs + 2) raw_sig = -1;

   // 2-Kerzen-Bestätigung: Signal muss auf vorheriger Kerze ebenfalls aktiv gewesen sein
   if(InpConfirmBars >= 2 && raw_sig != 0)
     {
      static int prev_sig   = 0;
      static datetime prev_bar_time = 0;
      datetime this_bar = iTime(_Symbol, PERIOD_M15, 1);
      datetime prev_bar = iTime(_Symbol, PERIOD_M15, 2);
      if(prev_bar_time == prev_bar && prev_sig == raw_sig)
        {
         prev_bar_time = this_bar;
         prev_sig      = raw_sig;
         return raw_sig;  // Beide Kerzen bestätigen → echtes Signal
        }
      prev_bar_time = this_bar;
      prev_sig      = raw_sig;
      return 0;  // Nur eine Kerze → noch kein Einstieg
     }

   return raw_sig;
  }

//══════════════════════════════════════════════════════════════════
//  OpenTrade — Dual TP: TP1 (50% Position) + TP2 (Runner)
//══════════════════════════════════════════════════════════════════
void OpenTrade(int dir, double atr)
  {
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
     {
      trade.Buy(half, _Symbol, ask, ask - sl_pts, ask + tp1_pts, "CQ-TP1");
      trade.Buy(half, _Symbol, ask, ask - sl_pts, tp2_buy,       "CQ-TP2");
     }
   else
     {
      trade.Sell(half, _Symbol, bid, bid + sl_pts, bid - tp1_pts, "CQ-TP1");
      trade.Sell(half, _Symbol, bid, bid + sl_pts, tp2_sell,      "CQ-TP2");
     }

   // Zähler aktualisieren
   g_DailyTrades++;
   g_LastOpenTime = TimeCurrent();

   PrintFormat("TRADE OPEN | %s | Lot:%.2fx2 | SL:%.2f | TP1:%.2f | TP2:%.2f | ATR:%.2f | Tag#%d",
               dir == 1 ? "BUY" : "SELL", half, sl_pts, tp1_pts, tp2_pts, atr, g_DailyTrades);
  }

//══════════════════════════════════════════════════════════════════
//  ManageTrades — Break-Even + Trailing Stop
//══════════════════════════════════════════════════════════════════
void ManageTrades()
  {
   double atr = Buf(hM_ATR, 0, 1);
   if(atr <= 0) return;

   bool   tp1_open   = false;
   bool   tp2_open   = false;
   ulong  tp2_ticket = 0;

   // Positionen scannen
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket)) continue;
      if(PositionGetInteger(POSITION_MAGIC) != (long)MAGIC) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      string cmt = PositionGetString(POSITION_COMMENT);
      if(StringFind(cmt, "CQ-TP1") >= 0) tp1_open = true;
      if(StringFind(cmt, "CQ-TP2") >= 0) { tp2_open = true; tp2_ticket = ticket; }
     }

   // TP1 wurde getroffen (TP1 weg, TP2 noch offen) → BE für TP2
   if(!tp1_open && tp2_open && tp2_ticket > 0 && InpBEAt > 0.0)
     {
      if(PositionSelectByTicket(tp2_ticket))
        {
         double entry = PositionGetDouble(POSITION_PRICE_OPEN);
         double sl    = PositionGetDouble(POSITION_SL);
         double tp    = PositionGetDouble(POSITION_TP);
         long   ptype = PositionGetInteger(POSITION_TYPE);

         if(ptype == POSITION_TYPE_BUY && sl < entry - _Point)
            trade.PositionModify(tp2_ticket, entry + _Point, tp);
         else if(ptype == POSITION_TYPE_SELL && sl > entry + _Point)
            trade.PositionModify(tp2_ticket, entry - _Point, tp);
        }
     }

   // ── RSI Emergency Close (Bounce-Schutz) ─────────────────────────
   // Wenn RSI extremes Niveau → alle Gewinne sofort sichern, bevor Reversal kommt
   double cur_rsi = Buf(hM_RSI, 0, 0);
   if(cur_rsi > 0)
     {
      for(int i = PositionsTotal() - 1; i >= 0; i--)
        {
         ulong tk = PositionGetTicket(i);
         if(!PositionSelectByTicket(tk)) continue;
         if(PositionGetInteger(POSITION_MAGIC) != (long)MAGIC) continue;
         if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
         long   ptype  = PositionGetInteger(POSITION_TYPE);
         double profit = PositionGetDouble(POSITION_PROFIT);
         if(profit <= 0) continue;  // Nur profitable Positionen notfall-schließen
         // SELL bei überverkauftem RSI → Bounce droht → Gewinne sichern
         if(ptype == POSITION_TYPE_SELL && cur_rsi < InpRSICloseSell)
           {
            trade.PositionClose(tk);
            Print("RSI-EMERGENCY CLOSE (SELL | RSI=", cur_rsi, " < ", InpRSICloseSell, ")");
           }
         // BUY bei überkauftem RSI → Verkaufsdruck droht → Gewinne sichern
         else if(ptype == POSITION_TYPE_BUY && cur_rsi > InpRSICloseBuy)
           {
            trade.PositionClose(tk);
            Print("RSI-EMERGENCY CLOSE (BUY | RSI=", cur_rsi, " > ", InpRSICloseBuy, ")");
           }
        }
     }

   // ── ATR Trailing Stop für TP2 Runner ─────────────────────────────
   // Trail aktiviert erst nach InpTrailActivate*ATR Gewinn (kein Früh-Stop)
   if(InpTrailing && tp2_open && tp2_ticket > 0)
     {
      if(PositionSelectByTicket(tp2_ticket))
        {
         double sl    = PositionGetDouble(POSITION_SL);
         double tp    = PositionGetDouble(POSITION_TP);
         double entry = PositionGetDouble(POSITION_PRICE_OPEN);
         long   ptype = PositionGetInteger(POSITION_TYPE);
         double trail = atr * InpTrailMult;
         double activate_dist = atr * InpTrailActivate;

         if(ptype == POSITION_TYPE_BUY)
           {
            double bid    = SymbolInfoDouble(_Symbol, SYMBOL_BID);
            double new_sl = bid - trail;
            // Trail erst wenn Gewinn > activate_dist (z.B. 0.5*ATR)
            bool   in_profit = (bid - entry) >= activate_dist;
            // Bei InpTP2NoLimit: kein festes TP, nur Trail (tp=0 → kein Limit)
            bool   tp_ok = (tp <= 0 || new_sl < tp - _Point);
            if(in_profit && new_sl > sl + _Point && tp_ok)
               trade.PositionModify(tp2_ticket, new_sl, tp);
           }
         else
           {
            double ask    = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
            double new_sl = ask + trail;
            bool   in_profit = (entry - ask) >= activate_dist;
            bool   tp_ok = (tp <= 0 || new_sl > tp + _Point);
            if(in_profit && new_sl < sl - _Point && tp_ok)
               trade.PositionModify(tp2_ticket, new_sl, tp);
           }
        }
     }

   // Break-Even für TP1 Positionen (falls TP1=TP2 oder nur eine Position)
   if(InpBEAt > 0.0)
     {
      double trigger = atr * InpBEAt;
      for(int i = PositionsTotal() - 1; i >= 0; i--)
        {
         ulong ticket = PositionGetTicket(i);
         if(!PositionSelectByTicket(ticket)) continue;
         if(PositionGetInteger(POSITION_MAGIC) != (long)MAGIC) continue;
         if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;

         double entry = PositionGetDouble(POSITION_PRICE_OPEN);
         double sl    = PositionGetDouble(POSITION_SL);
         double tp    = PositionGetDouble(POSITION_TP);
         long   ptype = PositionGetInteger(POSITION_TYPE);

         if(ptype == POSITION_TYPE_BUY)
           {
            double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
            if(bid - entry >= trigger && sl < entry - _Point)
               trade.PositionModify(ticket, entry + _Point, tp);
           }
         else
           {
            double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
            if(entry - ask >= trigger && sl > entry + _Point)
               trade.PositionModify(ticket, entry - _Point, tp);
           }
        }
     }
  }

//══════════════════════════════════════════════════════════════════
//  ShowDashboard — Chart Comment
//══════════════════════════════════════════════════════════════════
void ShowDashboard()
  {
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

   string h4_str = (h4e50 > h4e200) ? "BULLISH" : (h4e50 < h4e200 ? "BEARISH" : "NEUTRAL");
   string sess_str = in_sess ? "AKTIV (London/NY)" : "WARTEN (Asian)";

   Comment(StringFormat(
      "╔══════════════════════════════════════╗\n"
      "║  CLAUDE QUANT ELITE v2.0             ║\n"
      "║  Strategie : %-22s ║\n"
      "╠══════════════════════════════════════╣\n"
      "║  Balance  : %10.2f USD          ║\n"
      "║  Equity   : %10.2f USD          ║\n"
      "║  Tag P&L  : %+10.2f USD         ║\n"
      "╠══════════════════════════════════════╣\n"
      "║  H4 Trend : %-22s ║\n"
      "║  H1 RSI   : %5.1f                    ║\n"
      "║  M15 RSI  : %5.1f                    ║\n"
      "║  ATR M15  : %8.2f                ║\n"
      "╠══════════════════════════════════════╣\n"
      "║  Session  : %-22s ║\n"
      "║  Positionen: %2d  | Trades heute: %2d  ║\n"
      "╚══════════════════════════════════════╝",
      "VOLUME_CONF", balance, equity, day_pl,
      h4_str, h1rsi, m15rsi, atr,
      sess_str, CountMyPositions(), g_DailyTrades));
  }

//══════════════════════════════════════════════════════════════════
//  Hilfsfunktionen
//══════════════════════════════════════════════════════════════════

int CountMyPositions()
  {
   int count = 0;
   for(int i = 0; i < PositionsTotal(); i++)
     {
      ulong t = PositionGetTicket(i);
      if(!PositionSelectByTicket(t)) continue;
      if(PositionGetInteger(POSITION_MAGIC) == (long)MAGIC &&
         PositionGetString(POSITION_SYMBOL) == _Symbol) count++;
     }
   return count;
  }

double Buf(int handle, int buffer, int shift)
  {
   double arr[];
   ArraySetAsSeries(arr, true);
   if(CopyBuffer(handle, buffer, shift, 1, arr) <= 0) return 0.0;
   return arr[0];
  }

//+------------------------------------------------------------------+
// Ende CLAUDE QUANT ELITE v2.0
//+------------------------------------------------------------------+
