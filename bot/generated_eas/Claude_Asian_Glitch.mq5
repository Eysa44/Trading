//+------------------------------------------------------------------+
//|  CLAUDE QUANT — Asian Session Gold Glitch v1.0                  |
//|  XAUUSD M5                                                       |
//|  Strategie: ICT Asian Liquidity Sweep & Fade                     |
//|  nach @algo.jan "Asian Session Gold Glitch — Simple Entry Model" |
//|                                                                   |
//|  Logik:                                                          |
//|  1. Asian Session (00:00-07:00 UTC) High/Low aufzeichnen         |
//|  2. London Open (07:00-12:00 UTC): Warten auf Sweep              |
//|  3. Preis spikt über Asian High/Low → schliesst ZURÜCK rein      |
//|  4. Fade-Trade: SELL nach High-Sweep, BUY nach Low-Sweep         |
//|  5. SL hinter Sweep-Wick + ATR Buffer                            |
//|  6. TP1 = gegenüber liegendes Asian-Extrem (50% close)           |
//|  7. TP2 = ATR-Trailing Runner (kein festes Limit)                |
//+------------------------------------------------------------------+
#property copyright "Claude + Quant — Asian Glitch v1.0"
#property version   "1.00"
#property description "Asian Session Liquidity Sweep EA for XAUUSD M5"

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>

//══════════════════════════════════════════════════════════════════
//  EINGABE-PARAMETER
//══════════════════════════════════════════════════════════════════

//── ASIAN SESSION ─────────────────────────────────────────────────
input string  _Sec0              = "══ ASIAN SESSION SETUP ══";
input int     InpAsianStartH     = 0;     // Asian Session Start (UTC Stunde)
input int     InpAsianEndH       = 7;     // Asian Session Ende (UTC Stunde) — Range eingefroren
input int     InpLondonStartH    = 7;     // Sweep-Fenster Start UTC (London Open)
input int     InpLondonEndH      = 12;    // Sweep-Fenster Ende UTC (NY Pre-Market)
input double  InpMinSweepUSD     = 3.0;   // Mindest-Sweep in USD über/unter Asian H/L (Noise-Filter)

//── RISIKO ────────────────────────────────────────────────────────
input string  _Sec1              = "══ RISIKO ══";
input double  InpRiskPct         = 1.0;   // Risiko % pro Trade (vom Kontostand)
input double  InpMaxDailyDD      = 3.0;   // Max. Tagesverlust % (dann Stop)
input int     InpMaxSpread       = 50;    // Max. Spread in Punkten
input int     InpMaxTradesPerDay = 1;     // Max. Trades pro Tag (1 Setup pro Asian Range)

//── STOP LOSS ─────────────────────────────────────────────────────
input string  _Sec2              = "══ STOP LOSS ══";
input double  InpSLBuffer        = 0.5;   // ATR-Puffer über Sweep-Wick für SL
input double  InpSLMinUSD        = 15.0;  // Mindest-SL in USD (Stop-Hunt Schutz)

//── TAKE PROFIT ───────────────────────────────────────────────────
input string  _Sec3              = "══ TAKE PROFIT ══";
input bool    InpTP1UseAsian     = true;  // TP1 = gegenüber liegendes Asian-Extrem (empfohlen)
input double  InpTP1Mult         = 2.0;   // TP1 als ATR-Vielfaches (wenn InpTP1UseAsian=false)
input double  InpTrailMult       = 2.0;   // Trailing Stop Abstand TP2 (ATR x)
input double  InpTrailActivate   = 1.0;   // Trail startet ab X*ATR Gewinn

//── FILTER ────────────────────────────────────────────────────────
input string  _Sec4              = "══ FILTER ══";
input bool    InpSkipMonday      = true;  // Montag vor 10 UTC überspringen (dünne Liquidität)
input bool    InpSkipFriday      = true;  // Freitag ab 15 UTC überspringen (Weekend-Risiko)
input double  InpMinRangeUSD     = 8.0;   // Mindest-Asian-Range in USD
input double  InpMaxRangeUSD     = 80.0;  // Max-Asian-Range in USD
input int     InpSweepLookback   = 5;     // Max. Kerzen nach Sweep bis Close-Back erwartet wird
input int     InpGMTOffset       = 0;     // Broker UTC-Offset (0=TimeGMT auto, 2=UTC+2, 3=UTC+3)

//══════════════════════════════════════════════════════════════════
//  GLOBALE VARIABLEN
//══════════════════════════════════════════════════════════════════

CTrade trade;
const ulong MAGIC = 20260701;

int      hM_ATR;

double   g_AsianHigh      = 0;
double   g_AsianLow       = 1e9;
bool     g_AsianLocked    = false;
bool     g_SellTraded     = false;   // Verhindert Doppel-Sell nach mehreren Sweeps
bool     g_BuyTraded      = false;
bool     g_HighSwept      = false;   // Sweep über Asian High wurde detektiert
bool     g_LowSwept       = false;   // Sweep unter Asian Low wurde detektiert
int      g_SweepBarCount  = 0;       // Kerzen seit letztem Sweep (Timeout-Zähler)

datetime g_DayStart       = 0;
double   g_DayStartBal    = 0;
int      g_TradesToday    = 0;
datetime g_LastBar        = 0;

//══════════════════════════════════════════════════════════════════
//  OnInit
//══════════════════════════════════════════════════════════════════
int OnInit()
  {
   trade.SetExpertMagicNumber(MAGIC);
   trade.SetDeviationInPoints(20);

   hM_ATR = iATR(_Symbol, PERIOD_M5, 14);
   if(hM_ATR == INVALID_HANDLE)
     { Alert("ATR Handle ungueltig! Symbol/TF pruefen."); return INIT_FAILED; }

   g_DayStartBal = AccountInfoDouble(ACCOUNT_BALANCE);
   g_DayStart    = TimeCurrent();
   ResetDay();

   Print("=== Asian Session Gold Glitch v1.0 | XAUUSD M5 ===");
   PrintFormat("Asian: %d:00-%d:00 UTC | Sweep-Fenster: %d:00-%d:00 UTC",
               InpAsianStartH, InpAsianEndH, InpLondonStartH, InpLondonEndH);
   PrintFormat("Min-Sweep: %.1f USD | Min-Range: %.1f USD | Max-Range: %.1f USD",
               InpMinSweepUSD, InpMinRangeUSD, InpMaxRangeUSD);
   return INIT_SUCCEEDED;
  }

//══════════════════════════════════════════════════════════════════
//  OnDeinit
//══════════════════════════════════════════════════════════════════
void OnDeinit(const int reason)
  {
   if(hM_ATR != INVALID_HANDLE) IndicatorRelease(hM_ATR);
   Comment("");
  }

//══════════════════════════════════════════════════════════════════
//  OnTick
//══════════════════════════════════════════════════════════════════
void OnTick()
  {
   ManageTrades();
   ShowDashboard();

   // Nur auf neue M5-Kerze reagieren
   datetime cur_bar = iTime(_Symbol, PERIOD_M5, 0);
   if(cur_bar == g_LastBar) return;
   g_LastBar = cur_bar;

   //── Tages-Reset ──────────────────────────────────────────────
   MqlDateTime now_dt, day_dt;
   TimeToStruct(TimeCurrent(), now_dt);
   TimeToStruct(g_DayStart,   day_dt);
   if(now_dt.day != day_dt.day)
     {
      g_DayStartBal = AccountInfoDouble(ACCOUNT_BALANCE);
      g_DayStart    = TimeCurrent();
      g_TradesToday = 0;
      ResetDay();
     }

   // UTC-Zeit: InpGMTOffset=0 → TimeGMT() auto, sonst manueller Offset
   datetime utc_time = (InpGMTOffset == 0) ? TimeGMT()
                                            : TimeCurrent() - (datetime)(InpGMTOffset * 3600);
   MqlDateTime gmt;
   TimeToStruct(utc_time, gmt);
   int h = gmt.hour;

   //── Wochentag-Filter ─────────────────────────────────────────
   MqlDateTime loc;
   TimeToStruct(TimeCurrent(), loc);
   if(InpSkipMonday && loc.day_of_week == 1 && h < 10) return;
   if(InpSkipFriday && loc.day_of_week == 5 && h >= 15) return;

   double bar_high  = iHigh (_Symbol, PERIOD_M5, 1);
   double bar_low   = iLow  (_Symbol, PERIOD_M5, 1);
   double bar_close = iClose(_Symbol, PERIOD_M5, 1);

   //── Phase 1: Asian Range aufbauen ────────────────────────────
   if(h >= InpAsianStartH && h < InpAsianEndH)
     {
      if(!g_AsianLocked)
        {
         if(bar_high > g_AsianHigh) g_AsianHigh = bar_high;
         if(bar_low  < g_AsianLow)  g_AsianLow  = bar_low;
        }
      return;   // Während Asian Session nicht handeln
     }

   //── Asian Range einfrieren ────────────────────────────────────
   if(!g_AsianLocked && g_AsianHigh > 0 && g_AsianLow < 1e9)
     {
      g_AsianLocked = true;
      double range = g_AsianHigh - g_AsianLow;
      PrintFormat("[AsianGlitch] Range eingefroren: H=%.2f  L=%.2f  %.2f USD",
                  g_AsianHigh, g_AsianLow, range);

      if(range < InpMinRangeUSD || range > InpMaxRangeUSD)
        {
         PrintFormat("[AsianGlitch] Range %.2f außerhalb [%.1f-%.1f] → Skip", range, InpMinRangeUSD, InpMaxRangeUSD);
         g_TradesToday = InpMaxTradesPerDay;  // blockieren
        }
      return;
     }

   //── Phase 2: Sweep-Fenster (London Open) ─────────────────────
   if(!g_AsianLocked) return;
   if(h < InpLondonStartH || h >= InpLondonEndH) return;

   // Checks vor Trade-Öffnung
   if(g_TradesToday >= InpMaxTradesPerDay) return;
   if(CountMyPositions() >= 2) return;

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   if(g_DayStartBal > 0)
     {
      double dd = (g_DayStartBal - balance) / g_DayStartBal * 100.0;
      if(dd >= InpMaxDailyDD) return;
     }

   long spread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   if(spread > InpMaxSpread) return;

   double atr = Buf(hM_ATR, 0, 1);
   if(atr <= 0) return;

   //── Sweep-Flags aktualisieren ─────────────────────────────────
   // Sweep über Asian High → Flag setzen (unabhängig von Close)
   if(!g_HighSwept && bar_high >= g_AsianHigh + InpMinSweepUSD)
     {
      g_HighSwept     = true;
      g_SweepBarCount = 0;
      PrintFormat("[AsianGlitch] HIGH SWEEP: bar_high=%.2f > H+%.1f=%.2f",
                  bar_high, InpMinSweepUSD, g_AsianHigh + InpMinSweepUSD);
     }
   if(!g_LowSwept && bar_low <= g_AsianLow - InpMinSweepUSD)
     {
      g_LowSwept      = true;
      g_SweepBarCount = 0;
      PrintFormat("[AsianGlitch] LOW SWEEP: bar_low=%.2f < L-%.1f=%.2f",
                  bar_low, InpMinSweepUSD, g_AsianLow - InpMinSweepUSD);
     }

   // Zähler hochzählen; Sweep verfällt nach InpSweepLookback Kerzen ohne Close-Back
   if(g_HighSwept || g_LowSwept) g_SweepBarCount++;
   if(g_SweepBarCount > InpSweepLookback)
     {
      if(g_HighSwept) PrintFormat("[AsianGlitch] HIGH SWEEP abgelaufen (kein Close-Back in %d Kerzen)", InpSweepLookback);
      if(g_LowSwept)  PrintFormat("[AsianGlitch] LOW  SWEEP abgelaufen (kein Close-Back in %d Kerzen)", InpSweepLookback);
      g_HighSwept = false;
      g_LowSwept  = false;
      g_SweepBarCount = 0;
     }

   //── Signal: Close-Back nach Sweep → ENTRY ────────────────────
   // SELL: Sweep über Asian High ist aktiv UND bar[1] schliesst zurück darunter
   bool sell_sig = !g_SellTraded && g_HighSwept && (bar_close < g_AsianHigh);

   // BUY: Sweep unter Asian Low ist aktiv UND bar[1] schliesst zurück darüber
   bool buy_sig  = !g_BuyTraded  && g_LowSwept  && (bar_close > g_AsianLow);

   if(sell_sig && !buy_sig)
     {
      double sweep_wick = bar_high;  // letzte bekannte Sweep-Kerze
      // Suche exaktes Sweep-Hoch in Lookback-Fenster
      for(int k = 1; k <= MathMin(InpSweepLookback, 10); k++)
         sweep_wick = MathMax(sweep_wick, iHigh(_Symbol, PERIOD_M5, k));
      OpenTrade(-1, atr, sweep_wick);
      g_SellTraded = true;
      g_HighSwept  = false;
     }
   else if(buy_sig && !sell_sig)
     {
      double sweep_wick = bar_low;
      for(int k = 1; k <= MathMin(InpSweepLookback, 10); k++)
         sweep_wick = MathMin(sweep_wick, iLow(_Symbol, PERIOD_M5, k));
      OpenTrade(1, atr, sweep_wick);
      g_BuyTraded = true;
      g_LowSwept  = false;
     }
  }

//══════════════════════════════════════════════════════════════════
//  OpenTrade
//  dir=+1 BUY, dir=-1 SELL
//  sweep_extreme = High der Sweep-Kerze (SELL) oder Low (BUY)
//══════════════════════════════════════════════════════════════════
void OpenTrade(int dir, double atr, double sweep_extreme)
  {
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   double sl_pts  = 0;
   double tp1_pts = 0;

   if(dir == 1)  // BUY nach Low-Sweep
     {
      // SL: unter Sweep-Low + ATR-Puffer
      sl_pts  = ask - sweep_extreme + atr * InpSLBuffer;
      sl_pts  = MathMax(sl_pts, InpSLMinUSD);
      // TP1: Asian High (gegenüberliegendes Extrem)
      if(InpTP1UseAsian && g_AsianHigh > ask)
         tp1_pts = g_AsianHigh - ask;
      else
         tp1_pts = atr * InpTP1Mult;
     }
   else  // SELL nach High-Sweep
     {
      // SL: über Sweep-High + ATR-Puffer
      sl_pts  = sweep_extreme - bid + atr * InpSLBuffer;
      sl_pts  = MathMax(sl_pts, InpSLMinUSD);
      // TP1: Asian Low (gegenüberliegendes Extrem)
      if(InpTP1UseAsian && g_AsianLow > 0 && g_AsianLow < bid)
         tp1_pts = bid - g_AsianLow;
      else
         tp1_pts = atr * InpTP1Mult;
     }

   if(tp1_pts <= 0 || sl_pts <= 0)
     {
      Print("OpenTrade: ungültige SL/TP Werte → abgebrochen");
      return;
     }

   // Lot-Berechnung (1% Risiko auf sl_pts)
   double bal   = AccountInfoDouble(ACCOUNT_BALANCE);
   double risk  = bal * (InpRiskPct / 100.0);
   double csize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   double full  = NormalizeDouble(risk / (sl_pts * csize), 2);
   double minl  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxl  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double step  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);

   full = MathMax(full, minl * 2);
   full = MathMin(full, maxl);
   full = MathFloor(full / step) * step;

   double half = NormalizeDouble(full / 2.0, 2);
   half = MathMax(half, minl);
   half = MathFloor(half / step) * step;

   double rr = tp1_pts / sl_pts;

   if(dir == 1)
     {
      trade.Buy(half, _Symbol, ask, ask - sl_pts, ask + tp1_pts, "AG-TP1");
      trade.Buy(half, _Symbol, ask, ask - sl_pts, 0.0,           "AG-TP2");
     }
   else
     {
      trade.Sell(half, _Symbol, bid, bid + sl_pts, bid - tp1_pts, "AG-TP1");
      trade.Sell(half, _Symbol, bid, bid + sl_pts, 0.0,            "AG-TP2");
     }

   g_TradesToday++;
   PrintFormat("ASIAN GLITCH TRADE | %s | H=%.2f L=%.2f | Entry=%.2f SL=%.2f TP1=%.2f | RR=%.2f | Lot=%.2fx2",
               dir==1?"BUY":"SELL",
               g_AsianHigh, g_AsianLow,
               dir==1?ask:bid, sl_pts, tp1_pts, rr, half);
  }

//══════════════════════════════════════════════════════════════════
//  ManageTrades — TP1-Hit → BE für TP2 + ATR Trailing
//══════════════════════════════════════════════════════════════════
void ManageTrades()
  {
   double atr = Buf(hM_ATR, 0, 1);
   if(atr <= 0) return;

   bool  tp1_open   = false;
   bool  tp2_open   = false;
   ulong tp2_ticket = 0;

   for(int i = PositionsTotal()-1; i >= 0; i--)
     {
      ulong tk = PositionGetTicket(i);
      if(!PositionSelectByTicket(tk)) continue;
      if(PositionGetInteger(POSITION_MAGIC) != (long)MAGIC) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      string cmt = PositionGetString(POSITION_COMMENT);
      if(StringFind(cmt, "AG-TP1") >= 0) tp1_open = true;
      if(StringFind(cmt, "AG-TP2") >= 0) { tp2_open = true; tp2_ticket = tk; }
     }

   // TP1 getroffen → Break-Even für TP2 Runner
   if(!tp1_open && tp2_open && tp2_ticket > 0)
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

   // ATR Trailing Stop für TP2 (Runner)
   if(tp2_open && tp2_ticket > 0)
     {
      if(PositionSelectByTicket(tp2_ticket))
        {
         double entry = PositionGetDouble(POSITION_PRICE_OPEN);
         double sl    = PositionGetDouble(POSITION_SL);
         double tp    = PositionGetDouble(POSITION_TP);
         long   ptype = PositionGetInteger(POSITION_TYPE);
         double trail = atr * InpTrailMult;
         double act   = atr * InpTrailActivate;

         if(ptype == POSITION_TYPE_BUY)
           {
            double bid_p  = SymbolInfoDouble(_Symbol, SYMBOL_BID);
            double new_sl = bid_p - trail;
            bool in_profit = (bid_p - entry) >= act;
            bool tp_ok     = (tp <= 0 || new_sl < tp - _Point);
            if(in_profit && new_sl > sl + _Point && tp_ok)
               trade.PositionModify(tp2_ticket, new_sl, tp);
           }
         else
           {
            double ask_p  = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
            double new_sl = ask_p + trail;
            bool in_profit = (entry - ask_p) >= act;
            bool tp_ok     = (tp <= 0 || new_sl > tp + _Point);
            if(in_profit && new_sl < sl - _Point && tp_ok)
               trade.PositionModify(tp2_ticket, new_sl, tp);
           }
        }
     }
  }

//══════════════════════════════════════════════════════════════════
//  ShowDashboard
//══════════════════════════════════════════════════════════════════
void ShowDashboard()
  {
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);
   double day_pl  = balance - g_DayStartBal;
   double day_pct = g_DayStartBal > 0 ? day_pl / g_DayStartBal * 100.0 : 0;
   double atr     = Buf(hM_ATR, 0, 1);

   MqlDateTime gmt;
   TimeToStruct(TimeGMT(), gmt);
   int h = gmt.hour;

   string phase;
   if(h >= InpAsianStartH && h < InpAsianEndH && !g_AsianLocked)
      phase = StringFormat("ASIAN RANGE  H=%.2f  L=%.2f", g_AsianHigh, g_AsianLow < 1e9 ? g_AsianLow : 0);
   else if(h >= InpLondonStartH && h < InpLondonEndH && g_AsianLocked)
      phase = "SWEEP HUNTING  (London Open)";
   else if(g_AsianLocked)
      phase = "AUSSERHALB FENSTER";
   else
      phase = "WARTEN (Vor Asian Session)";

   double range = (g_AsianLocked && g_AsianHigh > 0 && g_AsianLow < 1e9)
                  ? g_AsianHigh - g_AsianLow : 0;

   Comment(StringFormat(
      "╔══════════════════════════════════════════╗\n"
      "║  ASIAN SESSION GOLD GLITCH v1.0          ║\n"
      "║  @algo.jan  ICT Liquidity Sweep          ║\n"
      "╠══════════════════════════════════════════╣\n"
      "║  Balance   : %10.2f USD              ║\n"
      "║  Equity    : %10.2f USD              ║\n"
      "║  Tag P&L   : %+10.2f (%+.1f%%)          ║\n"
      "╠══════════════════════════════════════════╣\n"
      "║  Asian High : %9.2f                  ║\n"
      "║  Asian Low  : %9.2f                  ║\n"
      "║  Range      : %9.2f USD              ║\n"
      "║  ATR M5     : %9.2f                  ║\n"
      "╠══════════════════════════════════════════╣\n"
      "║  Phase : %-30s ║\n"
      "║  Trades heute: %d / %d                    ║\n"
      "╚══════════════════════════════════════════╝",
      balance, equity, day_pl, day_pct,
      g_AsianHigh > 0 ? g_AsianHigh : 0,
      g_AsianLow < 1e9 ? g_AsianLow : 0,
      range, atr,
      phase,
      g_TradesToday, InpMaxTradesPerDay));
  }

//══════════════════════════════════════════════════════════════════
//  Hilfsfunktionen
//══════════════════════════════════════════════════════════════════

void ResetDay()
  {
   g_AsianHigh     = 0;
   g_AsianLow      = 1e9;
   g_AsianLocked   = false;
   g_SellTraded    = false;
   g_BuyTraded     = false;
   g_HighSwept     = false;
   g_LowSwept      = false;
   g_SweepBarCount = 0;
  }

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
// Ende — Claude Quant Asian Session Gold Glitch v1.0
//+------------------------------------------------------------------+
