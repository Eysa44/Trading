//+------------------------------------------------------------------+
//|  Asia NY Sweep Reversal Elite v2 — XAUUSD M15                   |
//|  Strategie: ICT Asian + NY Liquidity Sweep & Fade                |
//|                                                                   |
//|  FIXES v2:                                                        |
//|  - NY Levels: Mindest-Kerzen von 20 auf 5 reduziert              |
//|  - ATR Filter: Schwellwert angepasst (Standard 500–4000 Punkte)  |
//|  - ADX Filter: Min-Schwellwert auf 20 gesenkt (war 35)            |
//|  - Fallback: NY Levels aus Prev-Day H/L wenn zu wenig Kerzen     |
//+------------------------------------------------------------------+
#property copyright "Claude Quant — AsiaNY Elite v2"
#property version   "2.00"
#property description "Asian + NY Session Liquidity Sweep Reversal for XAUUSD M15"

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>

//══════════════════════════════════════════════════════════════════
//  EINGABE-PARAMETER
//══════════════════════════════════════════════════════════════════

//── ASIAN SESSION ─────────────────────────────────────────────────
input string  _Sec0             = "══ ASIAN SESSION ══";
input int     InpAsianStartH    = 0;      // Asian Start UTC
input int     InpAsianEndH      = 7;      // Asian Ende UTC (Range eingefroren)
input double  InpMinSweepUSD    = 3.0;    // Mindest-Sweep USD über/unter Asian H/L

//── NEW YORK SESSION ──────────────────────────────────────────────
input string  _Sec1             = "══ NEW YORK SESSION ══";
input int     InpNYStartH       = 13;     // NY Start UTC
input int     InpNYEndH         = 22;     // NY Ende UTC
input bool    InpUseNYLevels    = true;   // NY-Levels aus Vortag als Sweep-Zonen
input int     InpNYMinBars      = 5;      // Mindest-Kerzen für NY-Level-Berechnung (FIX: war 20)

//── ENTRY-FENSTER ─────────────────────────────────────────────────
input string  _Sec2             = "══ ENTRY FENSTER ══";
input int     InpLondonStartH   = 7;      // London Open Start UTC
input int     InpLondonEndH     = 12;     // London Open Ende UTC
input bool    InpTradeNYSweep   = true;   // Auch NY-Session Sweeps handeln

//── FILTER ────────────────────────────────────────────────────────
input string  _Sec3             = "══ FILTER ══";
input int     InpATRPeriod      = 14;     // ATR Periode
input double  InpATRMin         = 300.0;  // Min ATR Punkte (FIX: $3 Mindest-Volatilität)
input double  InpATRMax         = 4000.0; // Max ATR Punkte (FIX: $40 Chaos-Schutz)
input int     InpADXPeriod      = 14;     // ADX Periode
input double  InpADXMin         = 20.0;   // Min ADX (FIX: war 35, jetzt 20)
input int     InpMaxSpread      = 50;     // Max. Spread in Punkten

//── RISIKO ────────────────────────────────────────────────────────
input string  _Sec4             = "══ RISIKO ══";
input double  InpRiskPct        = 1.0;    // Risiko % pro Trade
input double  InpMaxDailyDD     = 3.0;    // Max. Tagesverlust %
input int     InpMaxTradesPerDay = 2;     // Max. Trades pro Tag

//── STOP LOSS ─────────────────────────────────────────────────────
input string  _Sec5             = "══ STOP LOSS ══";
input double  InpSLBuffer       = 0.5;    // ATR-Puffer über Sweep-Wick für SL
input double  InpSLMinUSD       = 15.0;   // Mindest-SL USD (Stop-Hunt Schutz)

//── TAKE PROFIT ───────────────────────────────────────────────────
input string  _Sec6             = "══ TAKE PROFIT ══";
input bool    InpTP1UseOpposite = true;   // TP1 = gegenüber liegendes Extrem
input double  InpTP1Mult        = 2.0;    // TP1 ATR-Vielfaches (wenn UseOpposite=false)
input double  InpTP1ClosePct    = 50.0;   // TP1 Partial-Close %
input double  InpTrailMult      = 2.0;    // ATR-Trailing Multiplikator (TP2 Runner)
input double  InpTrailActivate  = 1.0;    // ATR-Multiplikator bis Trail aktiviert

//── RANGE QUALITÄT ────────────────────────────────────────────────
input string  _Sec7             = "══ RANGE QUALITÄT ══";
input double  InpMinRangeUSD    = 8.0;    // Mindest Asian Range (USD)
input double  InpMaxRangeUSD    = 100.0;  // Max Asian Range (USD)

//══════════════════════════════════════════════════════════════════
//  GLOBALE VARIABLEN
//══════════════════════════════════════════════════════════════════
CTrade        g_trade;
int           g_atr_handle;
int           g_adx_handle;
ulong         g_magic       = 20260701;

// Asian Range
double        g_AsianHigh   = 0.0;
double        g_AsianLow    = 1e9;
bool          g_AsianLocked = false;

// NY Levels (aus Vortag)
double        g_NYHigh_prev = 0.0;
double        g_NYLow_prev  = 1e9;
bool          g_NYLevelsOK  = false;

// Session-State
bool          g_SellTraded  = false;
bool          g_BuyTraded   = false;
int           g_DayTrades   = 0;
datetime      g_LastDay     = 0;
double        g_DayStartBal = 0.0;

// Trailing
ulong         g_TP2_Tickets[];

//══════════════════════════════════════════════════════════════════
//  INIT / DEINIT
//══════════════════════════════════════════════════════════════════
int OnInit()
  {
   g_atr_handle = iATR(_Symbol, PERIOD_M15, InpATRPeriod);
   g_adx_handle = iADX(_Symbol, PERIOD_M15, InpADXPeriod);

   if(g_atr_handle == INVALID_HANDLE || g_adx_handle == INVALID_HANDLE)
     {
      Alert("[AsiaNY Elite v2] Indikator-Initialisierung fehlgeschlagen");
      return INIT_FAILED;
     }

   g_trade.SetExpertMagicNumber(g_magic);
   g_trade.SetDeviationInPoints(20);
   g_trade.SetTypeFilling(ORDER_FILLING_IOC);

   ArrayResize(g_TP2_Tickets, 0);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   IndicatorRelease(g_atr_handle);
   IndicatorRelease(g_adx_handle);
  }

//══════════════════════════════════════════════════════════════════
//  TICK HANDLER
//══════════════════════════════════════════════════════════════════
void OnTick()
  {
   if(!IsNewBar()) return;

   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   int hour = dt.hour;

   // ── Tages-Reset ──────────────────────────────────────────────
   datetime today_start = iTime(_Symbol, PERIOD_D1, 0);
   if(today_start != g_LastDay)
     {
      ResetDay(today_start);
      BuildNYLevels();   // NY Levels aus gestern aufbauen
     }

   // ── Asian Range aufzeichnen ───────────────────────────────────
   if(!g_AsianLocked)
     {
      if(hour >= InpAsianStartH && hour < InpAsianEndH)
         UpdateAsianRange();
      else if(hour >= InpAsianEndH)
         LockAsianRange();
     }

   // ── Tagesverlust-Check ────────────────────────────────────────
   if(DailyDDBreached()) return;

   // ── Filter prüfen ────────────────────────────────────────────
   double atr = GetATR();
   double adx = GetADX();
   if(atr <= 0.0) return;

   // ATR Filter
   double atr_pts = atr / _Point;
   if(atr_pts < InpATRMin)
     {
      static datetime last_atr_log = 0;
      if(TimeCurrent() - last_atr_log > 3600)
        {
         PrintFormat("[AsiaNY Elite v2] ATR Filter blockiert: %.0f (Min=%.0f)", atr_pts, InpATRMin);
         last_atr_log = TimeCurrent();
        }
      // Kein return — Trailing läuft weiter, nur kein Entry
     }
   bool atr_ok = (atr_pts >= InpATRMin && atr_pts <= InpATRMax);

   // ADX Filter
   if(adx < InpADXMin)
     {
      static datetime last_adx_log = 0;
      if(TimeCurrent() - last_adx_log > 3600)
        {
         PrintFormat("[AsiaNY Elite v2] ADX Filter blockiert: %.1f (Min=%.1f)", adx, InpADXMin);
         last_adx_log = TimeCurrent();
        }
     }
   bool adx_ok = (adx >= InpADXMin);

   // ── Trailing für TP2-Positionen ───────────────────────────────
   ManageTrailing(atr);

   // ── Entry nur wenn Filter OK ──────────────────────────────────
   if(!atr_ok || !adx_ok) return;
   if(g_DayTrades >= InpMaxTradesPerDay) return;
   if(!g_AsianLocked) return;

   // ── Spread-Check ─────────────────────────────────────────────
   long spread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   if(spread > InpMaxSpread) return;

   // ── Sweep-Signal prüfen ───────────────────────────────────────
   bool in_london = (hour >= InpLondonStartH && hour < InpLondonEndH);
   bool in_ny     = (hour >= InpNYStartH && hour < InpNYEndH);

   if(!in_london && !(InpTradeNYSweep && in_ny)) return;

   CheckAndTradeAsianSweep(atr, hour);

   if(InpUseNYLevels && g_NYLevelsOK && in_ny)
      CheckAndTradeNYSweep(atr);
  }

//══════════════════════════════════════════════════════════════════
//  NY LEVELS AUFBAUEN (aus gestrigem NY-Fenster)
//══════════════════════════════════════════════════════════════════
void BuildNYLevels()
  {
   g_NYHigh_prev = 0.0;
   g_NYLow_prev  = 1e9;
   g_NYLevelsOK  = false;

   if(!InpUseNYLevels) return;

   // Bars der letzten 2 Tage auf M15 scannen
   int total = iBars(_Symbol, PERIOD_M15);
   int bars_found = 0;

   for(int i = 1; i < MathMin(total, 288); i++)  // max 3 Tage zurück (288 × M15 = 3 Tage)
     {
      datetime bar_time = iTime(_Symbol, PERIOD_M15, i);
      MqlDateTime bdt;
      TimeToStruct(bar_time, bdt);

      // Gestern's NY-Session Bars (UTC 13-22 Uhr, nicht heute)
      datetime today_start = iTime(_Symbol, PERIOD_D1, 0);
      if(bar_time >= today_start) continue;  // heute überspringen

      datetime yesterday_start = iTime(_Symbol, PERIOD_D1, 1);
      if(bar_time < yesterday_start) break;  // vor gestern — fertig

      if(bdt.hour >= InpNYStartH && bdt.hour < InpNYEndH)
        {
         double bar_high = iHigh(_Symbol, PERIOD_M15, i);
         double bar_low  = iLow(_Symbol, PERIOD_M15, i);

         if(bar_high > g_NYHigh_prev) g_NYHigh_prev = bar_high;
         if(bar_low  < g_NYLow_prev)  g_NYLow_prev  = bar_low;
         bars_found++;
        }
     }

   if(bars_found < InpNYMinBars)
     {
      // Fallback: Prev-Day High/Low verwenden
      if(iBars(_Symbol, PERIOD_D1) >= 2)
        {
         g_NYHigh_prev = iHigh(_Symbol, PERIOD_D1, 1);
         g_NYLow_prev  = iLow(_Symbol, PERIOD_D1, 1);
         g_NYLevelsOK  = true;
         PrintFormat("[AsiaNY Elite v2] NY Levels Fallback: Prev-Day H=%.2f L=%.2f",
                     g_NYHigh_prev, g_NYLow_prev);
        }
      else
        {
         PrintFormat("[AsiaNY Elite v2] NY Levels nicht gebaut: zu wenige Kerzen (%d/%d)",
                     bars_found, InpNYMinBars);
        }
      return;
     }

   g_NYLevelsOK = true;
   PrintFormat("[AsiaNY Elite v2] NY Levels aufgebaut: H=%.2f L=%.2f (%d Kerzen)",
               g_NYHigh_prev, g_NYLow_prev, bars_found);
  }

//══════════════════════════════════════════════════════════════════
//  ASIAN SWEEP SIGNAL
//══════════════════════════════════════════════════════════════════
void CheckAndTradeAsianSweep(double atr, int hour)
  {
   if(g_AsianHigh <= 0.0 || g_AsianLow >= 1e8) return;

   double range = g_AsianHigh - g_AsianLow;
   if(range < InpMinRangeUSD || range > InpMaxRangeUSD) return;

   double bar_high  = iHigh(_Symbol, PERIOD_M15, 1);
   double bar_low   = iLow(_Symbol, PERIOD_M15, 1);
   double bar_close = iClose(_Symbol, PERIOD_M15, 1);

   // SELL: Sweep über Asian High, schließt zurück inside
   if(!g_SellTraded
      && bar_high  >= g_AsianHigh + InpMinSweepUSD
      && bar_close <  g_AsianHigh)
     {
      double sl_dist = MathMax(bar_high - iClose(_Symbol, PERIOD_M15, 0) + atr * InpSLBuffer,
                               InpSLMinUSD);
      double tp1 = InpTP1UseOpposite ? g_AsianLow : (iAsk(_Symbol) - atr * InpTP1Mult);
      OpenSell(sl_dist, tp1, "AG-Asian-SELL");
      g_SellTraded = true;
     }

   // BUY: Sweep unter Asian Low, schließt zurück inside
   if(!g_BuyTraded
      && bar_low   <= g_AsianLow - InpMinSweepUSD
      && bar_close >  g_AsianLow)
     {
      double sl_dist = MathMax(iClose(_Symbol, PERIOD_M15, 0) - bar_low + atr * InpSLBuffer,
                               InpSLMinUSD);
      double tp1 = InpTP1UseOpposite ? g_AsianHigh : (iBid(_Symbol) + atr * InpTP1Mult);
      OpenBuy(sl_dist, tp1, "AG-Asian-BUY");
      g_BuyTraded = true;
     }
  }

//══════════════════════════════════════════════════════════════════
//  NY LEVEL SWEEP SIGNAL
//══════════════════════════════════════════════════════════════════
void CheckAndTradeNYSweep(double atr)
  {
   if(!g_NYLevelsOK) return;

   double bar_high  = iHigh(_Symbol, PERIOD_M15, 1);
   double bar_low   = iLow(_Symbol, PERIOD_M15, 1);
   double bar_close = iClose(_Symbol, PERIOD_M15, 1);

   // SELL: Sweep über NY High (Vortag)
   if(!g_SellTraded
      && bar_high  >= g_NYHigh_prev + InpMinSweepUSD
      && bar_close <  g_NYHigh_prev)
     {
      double sl_dist = MathMax(bar_high - iClose(_Symbol, PERIOD_M15, 0) + atr * InpSLBuffer,
                               InpSLMinUSD);
      double tp1 = InpTP1UseOpposite ? g_NYLow_prev : (iAsk(_Symbol) - atr * InpTP1Mult);
      OpenSell(sl_dist, tp1, "AG-NY-SELL");
      g_SellTraded = true;
     }

   // BUY: Sweep unter NY Low (Vortag)
   if(!g_BuyTraded
      && bar_low   <= g_NYLow_prev - InpMinSweepUSD
      && bar_close >  g_NYLow_prev)
     {
      double sl_dist = MathMax(iClose(_Symbol, PERIOD_M15, 0) - bar_low + atr * InpSLBuffer,
                               InpSLMinUSD);
      double tp1 = InpTP1UseOpposite ? g_NYHigh_prev : (iBid(_Symbol) + atr * InpTP1Mult);
      OpenBuy(sl_dist, tp1, "AG-NY-BUY");
      g_BuyTraded = true;
     }
  }

//══════════════════════════════════════════════════════════════════
//  ORDER-FUNKTIONEN
//══════════════════════════════════════════════════════════════════
void OpenSell(double sl_dist, double tp1_price, string comment)
  {
   double bid    = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double sl     = NormalizeDouble(bid + sl_dist, _Digits);
   double tp1    = NormalizeDouble(tp1_price, _Digits);
   double lots   = CalcLots(sl_dist);

   if(lots <= 0) return;

   // Halbe Größe für TP1 (Partial Close)
   double lots_tp1 = NormalizeLots(lots * InpTP1ClosePct / 100.0);
   double lots_tp2 = NormalizeLots(lots - lots_tp1);

   // TP1 Order
   if(lots_tp1 > 0 && g_trade.Sell(lots_tp1, _Symbol, bid, sl, tp1, comment + "-TP1"))
     {
      g_DayTrades++;
      PrintFormat("[AsiaNY Elite v2] SELL TP1 %.2f lots @ %.2f, SL=%.2f, TP1=%.2f", lots_tp1, bid, sl, tp1);
     }

   // TP2 Runner (kein festes TP → Trailing)
   if(lots_tp2 > 0 && g_trade.Sell(lots_tp2, _Symbol, bid, sl, 0, comment + "-TP2"))
     {
      ulong ticket = g_trade.ResultOrder();
      int n = ArraySize(g_TP2_Tickets);
      ArrayResize(g_TP2_Tickets, n + 1);
      g_TP2_Tickets[n] = ticket;
      PrintFormat("[AsiaNY Elite v2] SELL TP2 %.2f lots @ %.2f (Trailing Runner)", lots_tp2, bid);
     }
  }

void OpenBuy(double sl_dist, double tp1_price, string comment)
  {
   double ask    = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double sl     = NormalizeDouble(ask - sl_dist, _Digits);
   double tp1    = NormalizeDouble(tp1_price, _Digits);
   double lots   = CalcLots(sl_dist);

   if(lots <= 0) return;

   double lots_tp1 = NormalizeLots(lots * InpTP1ClosePct / 100.0);
   double lots_tp2 = NormalizeLots(lots - lots_tp1);

   if(lots_tp1 > 0 && g_trade.Buy(lots_tp1, _Symbol, ask, sl, tp1, comment + "-TP1"))
     {
      g_DayTrades++;
      PrintFormat("[AsiaNY Elite v2] BUY TP1 %.2f lots @ %.2f, SL=%.2f, TP1=%.2f", lots_tp1, ask, sl, tp1);
     }

   if(lots_tp2 > 0 && g_trade.Buy(lots_tp2, _Symbol, ask, sl, 0, comment + "-TP2"))
     {
      ulong ticket = g_trade.ResultOrder();
      int n = ArraySize(g_TP2_Tickets);
      ArrayResize(g_TP2_Tickets, n + 1);
      g_TP2_Tickets[n] = ticket;
      PrintFormat("[AsiaNY Elite v2] BUY TP2 %.2f lots @ %.2f (Trailing Runner)", lots_tp2, ask);
     }
  }

//══════════════════════════════════════════════════════════════════
//  TRAILING MANAGEMENT (TP2 Runner)
//══════════════════════════════════════════════════════════════════
void ManageTrailing(double atr)
  {
   if(atr <= 0) return;

   for(int i = ArraySize(g_TP2_Tickets) - 1; i >= 0; i--)
     {
      if(!PositionSelectByTicket(g_TP2_Tickets[i])) continue;

      double open_price = PositionGetDouble(POSITION_PRICE_OPEN);
      double cur_sl     = PositionGetDouble(POSITION_SL);
      long   pos_type   = PositionGetInteger(POSITION_TYPE);
      double cur_price  = (pos_type == POSITION_TYPE_BUY)
                          ? SymbolInfoDouble(_Symbol, SYMBOL_BID)
                          : SymbolInfoDouble(_Symbol, SYMBOL_ASK);

      double trail_dist = atr * InpTrailMult;
      double activate   = atr * InpTrailActivate;

      if(pos_type == POSITION_TYPE_BUY)
        {
         if(cur_price < open_price + activate) continue;
         double new_sl = NormalizeDouble(cur_price - trail_dist, _Digits);
         if(new_sl > cur_sl + _Point)
            g_trade.PositionModify(g_TP2_Tickets[i], new_sl, 0);
        }
      else
        {
         if(cur_price > open_price - activate) continue;
         double new_sl = NormalizeDouble(cur_price + trail_dist, _Digits);
         if(new_sl < cur_sl - _Point || cur_sl == 0)
            g_trade.PositionModify(g_TP2_Tickets[i], new_sl, 0);
        }
     }
  }

//══════════════════════════════════════════════════════════════════
//  HILFS-FUNKTIONEN
//══════════════════════════════════════════════════════════════════
void UpdateAsianRange()
  {
   double h = iHigh(_Symbol, PERIOD_M15, 1);
   double l = iLow(_Symbol, PERIOD_M15, 1);
   if(h > g_AsianHigh) g_AsianHigh = h;
   if(l < g_AsianLow)  g_AsianLow  = l;
  }

void LockAsianRange()
  {
   if(!g_AsianLocked && g_AsianHigh > 0 && g_AsianLow < 1e8)
     {
      g_AsianLocked = true;
      PrintFormat("[AsiaNY Elite v2] Asian Range eingefroren: H=%.2f L=%.2f Range=%.2f USD",
                  g_AsianHigh, g_AsianLow, g_AsianHigh - g_AsianLow);
     }
  }

void ResetDay(datetime today_start)
  {
   g_LastDay     = today_start;
   g_AsianHigh   = 0.0;
   g_AsianLow    = 1e9;
   g_AsianLocked = false;
   g_SellTraded  = false;
   g_BuyTraded   = false;
   g_DayTrades   = 0;
   g_DayStartBal = AccountInfoDouble(ACCOUNT_BALANCE);
   ArrayResize(g_TP2_Tickets, 0);
  }

double GetATR()
  {
   double buf[2];
   if(CopyBuffer(g_atr_handle, 0, 1, 1, buf) < 1) return 0;
   return buf[0];
  }

double GetADX()
  {
   double buf[2];
   if(CopyBuffer(g_adx_handle, 0, 1, 1, buf) < 1) return 0;
   return buf[0];
  }

double CalcLots(double sl_dist)
  {
   double balance  = AccountInfoDouble(ACCOUNT_BALANCE);
   double risk_usd = balance * InpRiskPct / 100.0;
   double tick_val = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tick_sz  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick_val <= 0 || tick_sz <= 0 || sl_dist <= 0) return 0;
   double lot_risk = (risk_usd * tick_sz) / (sl_dist * tick_val);
   double min_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double max_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double lot_step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double lots     = MathFloor(lot_risk / lot_step) * lot_step;
   return MathMax(min_lot, MathMin(max_lot, lots));
  }

double NormalizeLots(double lots)
  {
   double min_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double max_lot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double lot_step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double result   = MathFloor(lots / lot_step) * lot_step;
   return MathMax(min_lot, MathMin(max_lot, result));
  }

bool DailyDDBreached()
  {
   if(g_DayStartBal <= 0) return false;
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double dd_pct  = (g_DayStartBal - equity) / g_DayStartBal * 100.0;
   return (dd_pct >= InpMaxDailyDD);
  }

bool IsNewBar()
  {
   static datetime last_bar = 0;
   datetime cur = iTime(_Symbol, PERIOD_M15, 0);
   if(cur == last_bar) return false;
   last_bar = cur;
   return true;
  }

double iAsk(string sym) { return SymbolInfoDouble(sym, SYMBOL_ASK); }
double iBid(string sym) { return SymbolInfoDouble(sym, SYMBOL_BID); }

//══════════════════════════════════════════════════════════════════
//  ON CHART EVENT — DASHBOARD
//══════════════════════════════════════════════════════════════════
void OnChartEvent(const int id, const long &lparam,
                  const double &dparam, const string &sparam)
  {
   Comment(StringFormat(
      "[AsiaNY Elite v2]\n"
      "Asian H: %.2f  L: %.2f  Range: %.2f USD\n"
      "NY Prev H: %.2f  L: %.2f  OK: %s\n"
      "Phase: %s\n"
      "Heute: %d Trade(s) | DD-Start: %.2f",
      g_AsianHigh, g_AsianLow,
      (g_AsianHigh > 0 && g_AsianLow < 1e8) ? g_AsianHigh - g_AsianLow : 0.0,
      g_NYHigh_prev, (g_NYLow_prev < 1e8 ? g_NYLow_prev : 0.0), (g_NYLevelsOK ? "JA" : "NEIN"),
      GetPhaseName(),
      g_DayTrades, g_DayStartBal
   ));
  }

string GetPhaseName()
  {
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   int h = dt.hour;
   if(h >= InpAsianStartH && h < InpAsianEndH)   return "ASIAN RANGE AUFBAU";
   if(h >= InpLondonStartH && h < InpLondonEndH) return "LONDON SWEEP HUNTING";
   if(h >= InpNYStartH && h < InpNYEndH)         return "NY SWEEP HUNTING";
   return "AUSSERHALB FENSTER";
  }
//+------------------------------------------------------------------+
