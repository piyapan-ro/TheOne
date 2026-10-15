//+------------------------------------------------------------------+
//|                                                AIScalper_M1.mq5  |
//|   M1 scalper driven by a machine-learning model (ONNX)           |
//|   Model: train_ai_scalper.py  ->  MQL5\Files\AIScalper.onnx      |
//+------------------------------------------------------------------+
#property copyright "Riverplus"
#property version   "1.00"
#property description "AI (RandomForest/ONNX) M1 scalper for XAUUSD."
#property description "Put AIScalper.onnx in MQL5\\Files. SL/TP/horizon must match training."
#property tester_file "AIScalper.onnx"

#include <Trade\Trade.mqh>

#define NF      21     // number of features - must match FEATURE_NAMES in Python
#define WINDOW  600    // bars used to compute features (indicator warm-up)

enum ENUM_LOT_MODE
  {
   LOT_FIXED        = 0, // Fixed lot
   LOT_RISK_PERCENT = 1  // Risk % of equity per trade
  };

input group "=== Model ==="
input string          InpModelFile        = "AIScalper.onnx"; // Model file in MQL5\Files
input double          InpProbThreshold    = 0.55;            // Min probability to trade (from training)
input bool            InpPrintFeatures    = false;           // Print features (compare with parity_check.csv)

input group "=== General ==="
input ulong           InpMagic            = 260110;          // Magic number
input string          InpComment          = "AIScalperM1";   // Order comment

input group "=== Money management ==="
input ENUM_LOT_MODE   InpLotMode          = LOT_RISK_PERCENT; // Lot mode
input double          InpFixedLot         = 0.01;            // Fixed lot (if Fixed)
input double          InpRiskPercent      = 0.5;             // Risk % per trade (if Risk %)
input double          InpMaxLot           = 1.00;            // Max lot cap
input bool            InpForceMinLot      = true;            // Use min lot if risk-% lot is too small

input group "=== SL / TP (must match training) ==="
input double          InpSlAtrMult        = 1.5;             // SL = ATR(14) x
input double          InpTpAtrMult        = 1.5;             // TP = ATR(14) x
input int             InpMaxBarsInTrade   = 30;              // Close after N bars (= --horizon)

input group "=== Optional trade management ==="
input bool            InpUseBreakeven     = false;           // Use breakeven (model trained without it)
input double          InpBeTriggerAtr     = 1.0;             // BE trigger (ATR x profit)
input double          InpBeLockPrice      = 0.10;            // BE lock-in in price ($)

input group "=== Filters ==="
input double          InpMaxSpreadPrice   = 0.80;            // Max spread in price ($) (0 = off)
input double          InpMinAtrPrice      = 0.30;            // Min ATR in price ($) (0 = off)
input double          InpMaxAtrPrice      = 15.0;            // Max ATR in price ($) (0 = off)
input int             InpMaxPositions     = 1;               // Max open positions
input int             InpCooldownBars     = 3;               // Bars to wait after an entry
input bool            InpUseSession       = true;            // Use trading session
input int             InpStartHour        = 9;               // Session start hour (server time)
input int             InpEndHour          = 21;              // Session end hour (server time)
input double          InpMaxSlippagePrice = 0.50;            // Max slippage in price ($)
input bool            InpDebug            = true;            // Print why no trade (Experts tab)

input group "=== Daily protection ==="
input int             InpMaxTradesPerDay  = 20;              // Max entries per day (0 = off)
input double          InpMaxDailyLossPct  = 3.0;             // Max daily loss % (0 = off)
input double          InpDailyTargetPct   = 0.0;             // Daily profit target % (0 = off)

//--- globals
CTrade   trade;
long     g_model = INVALID_HANDLE;
datetime g_lastBarTime = 0;
double   g_atr = 0.0;
double   g_pBuy = 0.0, g_pSell = 0.0;
string   g_reason = "waiting for new bar";

//+------------------------------------------------------------------+
int OnInit()
  {
   g_model = OnnxCreate(InpModelFile, ONNX_DEFAULT);
   if(g_model == INVALID_HANDLE)
     {
      PrintFormat("Cannot load model '%s' from MQL5\\Files (error %d). Run train_ai_scalper.py first.",
                  InpModelFile, GetLastError());
      return(INIT_FAILED);
     }
   const ulong in_shape[]   = {1, NF};
   const ulong out_label[]  = {1};
   const ulong out_proba[]  = {1, 3};
   if(!OnnxSetInputShape(g_model, 0, in_shape) ||
      !OnnxSetOutputShape(g_model, 0, out_label) ||
      !OnnxSetOutputShape(g_model, 1, out_proba))
     {
      PrintFormat("ONNX shape setup failed (error %d) - model must have %d inputs, 3 classes", GetLastError(), NF);
      return(INIT_FAILED);
     }

   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints((ulong)MathMax(1, MathRound(InpMaxSlippagePrice / _Point)));
   trade.SetTypeFillingBySymbol(_Symbol);

   PrintFormat("AIScalper loaded. %s digits=%d point=%g spread=%.2f threshold=%.2f",
               _Symbol, _Digits, _Point, SpreadPrice(), InpProbThreshold);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(g_model != INVALID_HANDLE) OnnxRelease(g_model);
   Comment("");
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   int tradesToday = 0; double closedPL = 0.0; datetime lastEntry = 0;
   GetTodayStats(tradesToday, closedPL, lastEntry);
   double dayPL       = closedPL + GetFloatingPL();
   double dayStartBal = AccountInfoDouble(ACCOUNT_BALANCE) - closedPL;
   bool lossHit   = (InpMaxDailyLossPct > 0 && dayStartBal > 0 && dayPL <= -dayStartBal * InpMaxDailyLossPct / 100.0);
   bool targetHit = (InpDailyTargetPct  > 0 && dayStartBal > 0 && dayPL >=  dayStartBal * InpDailyTargetPct  / 100.0);
   if(lossHit && CountPositions() > 0)
     {
      Print("Daily loss limit reached - closing all positions");
      CloseAll();
     }

   ManagePositions();
   UpdateComment(tradesToday, dayPL, lossHit, targetHit);

   datetime barTime = iTime(_Symbol, PERIOD_M1, 0);
   if(barTime == 0 || barTime == g_lastBarTime) return;
   g_lastBarTime = barTime;

   //--- run the model on every new bar (so the panel always shows probabilities)
   if(!Predict())
     {
      Block(g_reason);
      return;
     }

   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))          { Block("Algo Trading button is OFF"); return; }
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))                    { Block("'Allow Algo Trading' unchecked in EA properties"); return; }
   long tmode = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_MODE);
   if(tmode == SYMBOL_TRADE_MODE_DISABLED || tmode == SYMBOL_TRADE_MODE_CLOSEONLY)
                                                             { Block("Symbol trading disabled / close-only"); return; }
   if(lossHit)                                               { Block("Daily loss limit hit"); return; }
   if(targetHit)                                             { Block("Daily profit target hit"); return; }
   if(InpMaxTradesPerDay > 0 && tradesToday >= InpMaxTradesPerDay) { Block("Max trades per day reached"); return; }
   if(CountPositions() >= InpMaxPositions)                   { Block("Max positions open"); return; }
   if(!InSession())                                          { Block("Outside session hours (server time)"); return; }
   if(InpMaxSpreadPrice > 0 && SpreadPrice() > InpMaxSpreadPrice)
                                                             { Block(StringFormat("Spread %.2f > max %.2f", SpreadPrice(), InpMaxSpreadPrice)); return; }
   if(InpMinAtrPrice > 0 && g_atr < InpMinAtrPrice)          { Block(StringFormat("ATR %.2f < min", g_atr)); return; }
   if(InpMaxAtrPrice > 0 && g_atr > InpMaxAtrPrice)          { Block(StringFormat("ATR %.2f > max", g_atr)); return; }
   if(InpCooldownBars > 0 && lastEntry > 0)
     {
      int barsSince = iBarShift(_Symbol, PERIOD_M1, lastEntry, false);
      if(barsSince >= 0 && barsSince < InpCooldownBars)      { Block("Cooldown after last entry"); return; }
     }

   int signal = 0;
   if(g_pBuy  >= InpProbThreshold && g_pBuy  > g_pSell) signal =  1;
   if(g_pSell >= InpProbThreshold && g_pSell > g_pBuy)  signal = -1;

   if(signal != 0)
      OpenTrade(signal);
   else
      Block(StringFormat("No signal: P(buy)=%.2f P(sell)=%.2f < %.2f", g_pBuy, g_pSell, InpProbThreshold));
  }

//+------------------------------------------------------------------+
//| Features - exact mirror of compute_features() in Python          |
//+------------------------------------------------------------------+
bool ComputeFeatures(float &f[])
  {
   MqlRates r[];
   ArraySetAsSeries(r, false);                       // oldest first, last = bar 1 (closed)
   int n = CopyRates(_Symbol, PERIOD_M1, 1, WINDOW, r);
   if(n < WINDOW) { g_reason = StringFormat("Not enough history (%d/%d bars)", n, WINDOW); return false; }

   double e20[], e50[], rsi[], tr[];
   ArrayResize(e20, n); ArrayResize(e50, n); ArrayResize(rsi, n); ArrayResize(tr, n);
   double a20 = 2.0 / 21.0, a50 = 2.0 / 51.0;
   e20[0] = r[0].close; e50[0] = r[0].close; rsi[0] = 50.0; tr[0] = r[0].high - r[0].low;
   double ag = 0.0, al = 0.0;
   for(int i = 1; i < n; i++)
     {
      double c = r[i].close, pc = r[i - 1].close;
      e20[i] = a20 * c + (1.0 - a20) * e20[i - 1];
      e50[i] = a50 * c + (1.0 - a50) * e50[i - 1];
      double d = c - pc;
      double g = d > 0 ? d : 0.0, l = d < 0 ? -d : 0.0;
      ag = (ag * 13.0 + g) / 14.0;
      al = (al * 13.0 + l) / 14.0;
      rsi[i] = (al == 0.0) ? 100.0 : 100.0 - 100.0 / (1.0 + ag / al);
      tr[i] = MathMax(r[i].high - r[i].low, MathMax(MathAbs(r[i].high - pc), MathAbs(r[i].low - pc)));
     }

   int t = n - 1;
   double atr = 0.0, atr100 = 0.0, vsum = 0.0;
   for(int k = 0; k < 14;  k++) atr    += tr[t - k];
   for(int k = 0; k < 100; k++) atr100 += tr[t - k];
   for(int k = 0; k < 20;  k++) vsum   += (double)r[t - k].tick_volume;
   atr /= 14.0; atr100 /= 100.0;
   double vmean = vsum / 20.0;
   g_atr = atr;
   if(atr <= 0) { g_reason = "ATR is zero"; return false; }

   double c = r[t].close, o = r[t].open, h = r[t].high, lo = r[t].low;
   double A = atr;

   ArrayResize(f, NF);
   f[0]  = (float)((c - r[t - 1].close)  / A);
   f[1]  = (float)((c - r[t - 3].close)  / A);
   f[2]  = (float)((c - r[t - 5].close)  / A);
   f[3]  = (float)((c - r[t - 10].close) / A);
   f[4]  = (float)((c - r[t - 20].close) / A);
   f[5]  = (float)((c - e20[t]) / A);
   f[6]  = (float)((c - e50[t]) / A);
   f[7]  = (float)((e20[t] - e50[t]) / A);
   f[8]  = (float)((e20[t] - e20[t - 5]) / A);
   f[9]  = (float)(rsi[t] / 100.0);
   f[10] = (float)((rsi[t] - rsi[t - 3]) / 100.0);
   f[11] = (float)(atr100 > 0 ? atr / atr100 : 1.0);
   f[12] = (float)((c - o) / A);
   f[13] = (float)((h - MathMax(o, c)) / A);
   f[14] = (float)((MathMin(o, c) - lo) / A);
   f[15] = (float)RangePos(r, t, 20);
   f[16] = (float)RangePos(r, t, 60);
   f[17] = (float)(vmean > 0 ? (double)r[t].tick_volume / vmean : 1.0);
   MqlDateTime dt; TimeToStruct(r[t].time, dt);
   double hrs = dt.hour + dt.min / 60.0;
   f[18] = (float)MathSin(2.0 * M_PI * hrs / 24.0);
   f[19] = (float)MathCos(2.0 * M_PI * hrs / 24.0);
   f[20] = (float)(atr / c * 10000.0);

   if(InpPrintFeatures)
     {
      string s = TimeToString(r[t].time, TIME_DATE | TIME_MINUTES);
      for(int i = 0; i < NF; i++) s += StringFormat(",%.5f", f[i]);
      Print("FEATURES ", s);
     }
   return true;
  }

double RangePos(const MqlRates &r[], const int t, const int len)
  {
   double hh = r[t].high, ll = r[t].low;
   for(int k = 1; k < len; k++)
     {
      hh = MathMax(hh, r[t - k].high);
      ll = MathMin(ll, r[t - k].low);
     }
   double rng = hh - ll;
   return rng > 0 ? (r[t].close - ll) / rng : 0.5;
  }

//+------------------------------------------------------------------+
bool Predict()
  {
   float f[];
   if(!ComputeFeatures(f)) return false;

   matrixf x(1, NF);
   for(int i = 0; i < NF; i++) x[0][i] = f[i];
   long     label[1];
   matrixf  proba(1, 3);
   if(!OnnxRun(g_model, ONNX_NO_CONVERSION, x, label, proba))
     {
      g_reason = StringFormat("OnnxRun failed (error %d)", GetLastError());
      return false;
     }
   g_pBuy  = proba[0][1];
   g_pSell = proba[0][2];
   return true;
  }

//+------------------------------------------------------------------+
void OpenTrade(const int dir)
  {
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick) || g_atr <= 0) return;

   double minDist = MinStopDistance();
   double slDist  = MathMax(g_atr * InpSlAtrMult, minDist);
   double tpDist  = MathMax(g_atr * InpTpAtrMult, minDist);
   double price = (dir > 0) ? tick.ask : tick.bid;
   double sl = NormalizeDouble((dir > 0) ? price - slDist : price + slDist, _Digits);
   double tp = NormalizeDouble((dir > 0) ? price + tpDist : price - tpDist, _Digits);

   double lot = CalcLot(dir, price, slDist);
   if(lot <= 0) { Block("Lot size is zero (risk too small for min lot, or not enough margin)"); return; }

   string cmt = StringFormat("%s p=%.2f", InpComment, dir > 0 ? g_pBuy : g_pSell);
   bool ok = (dir > 0) ? trade.Buy(lot, _Symbol, price, sl, tp, cmt)
                       : trade.Sell(lot, _Symbol, price, sl, tp, cmt);
   uint rc = trade.ResultRetcode();
   if(!ok || (rc != TRADE_RETCODE_DONE && rc != TRADE_RETCODE_PLACED))
      Block(StringFormat("Order failed: retcode=%u %s", rc, trade.ResultRetcodeDescription()));
   else
     {
      g_reason = StringFormat("%s opened, P=%.2f", dir > 0 ? "BUY" : "SELL", dir > 0 ? g_pBuy : g_pSell);
      PrintFormat("%s %.2f lots @ %.*f SL %.*f TP %.*f  P(buy)=%.2f P(sell)=%.2f",
                  dir > 0 ? "BUY" : "SELL", lot, _Digits, price, _Digits, sl, _Digits, tp, g_pBuy, g_pSell);
     }
  }

//+------------------------------------------------------------------+
double CalcLot(const int dir, const double price, const double slDist)
  {
   double lot = InpFixedLot;
   ENUM_ORDER_TYPE type = (dir > 0) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   if(InpLotMode == LOT_RISK_PERCENT)
     {
      double riskMoney = AccountInfoDouble(ACCOUNT_EQUITY) * InpRiskPercent / 100.0;
      double profit = 0.0, lossPerLot = 0.0;
      double slPrice = (dir > 0) ? price - slDist : price + slDist;
      if(OrderCalcProfit(type, _Symbol, 1.0, price, slPrice, profit))
         lossPerLot = MathAbs(profit);
      else
        {
         double tv = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
         double ts = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
         if(ts > 0) lossPerLot = slDist / ts * tv;
        }
      if(lossPerLot <= 0) return 0.0;
      lot = riskMoney / lossPerLot;
     }
   lot = MathMin(lot, InpMaxLot);
   double raw = lot;
   lot = NormalizeLot(lot);
   double minLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   if(lot <= 0 && InpForceMinLot && raw > 0)
     {
      lot = minLot;
      PrintFormat("Risk-%% lot %.4f below min lot - using %.2f (actual risk higher than %.2f%%)", raw, lot, InpRiskPercent);
     }
   double margin = 0.0, freeMargin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   while(lot >= minLot && OrderCalcMargin(type, _Symbol, lot, price, margin) && margin > freeMargin * 0.9)
      lot = NormalizeLot(lot * 0.8);
   return (lot >= minLot) ? lot : 0.0;
  }

double NormalizeLot(double lot)
  {
   double minLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double step   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0) step = 0.01;
   lot = MathFloor(lot / step + 1e-9) * step;
   if(lot < minLot) return 0.0;
   if(lot > maxLot) lot = maxLot;
   int vd = (int)MathMax(0, MathCeil(-MathLog10(step)));
   return NormalizeDouble(lot, vd);
  }

//+------------------------------------------------------------------+
void ManagePositions()
  {
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double minDist = MinStopDistance();
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !IsMine()) continue;
      long     type   = PositionGetInteger(POSITION_TYPE);
      double   open   = PositionGetDouble(POSITION_PRICE_OPEN);
      double   sl     = PositionGetDouble(POSITION_SL);
      double   tp     = PositionGetDouble(POSITION_TP);
      datetime opened = (datetime)PositionGetInteger(POSITION_TIME);

      if(InpMaxBarsInTrade > 0 && iBarShift(_Symbol, PERIOD_M1, opened, false) >= InpMaxBarsInTrade)
        { trade.PositionClose(ticket); continue; }

      if(!InpUseBreakeven || g_atr <= 0) continue;
      if(type == POSITION_TYPE_BUY && bid - open >= g_atr * InpBeTriggerAtr)
        {
         double be = NormalizeDouble(MathMin(open + InpBeLockPrice, bid - minDist), _Digits);
         if(be > sl + _Point) trade.PositionModify(ticket, be, tp);
        }
      else if(type == POSITION_TYPE_SELL && open - ask >= g_atr * InpBeTriggerAtr)
        {
         double be = NormalizeDouble(MathMax(open - InpBeLockPrice, ask + minDist), _Digits);
         if(sl == 0 || be < sl - _Point) trade.PositionModify(ticket, be, tp);
        }
     }
  }

//+------------------------------------------------------------------+
bool IsMine()  // call after PositionGetTicket / PositionSelect
  {
   return PositionGetString(POSITION_SYMBOL) == _Symbol &&
          (ulong)PositionGetInteger(POSITION_MAGIC) == InpMagic;
  }

int CountPositions()
  {
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
      if(PositionGetTicket(i) > 0 && IsMine()) n++;
   return n;
  }

double GetFloatingPL()
  {
   double pl = 0.0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
      if(PositionGetTicket(i) > 0 && IsMine())
         pl += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
   return pl;
  }

void CloseAll()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0 && IsMine()) trade.PositionClose(ticket);
     }
  }

void GetTodayStats(int &trades, double &closedPL, datetime &lastEntry)
  {
   trades = 0; closedPL = 0.0; lastEntry = 0;
   datetime dayStart = iTime(_Symbol, PERIOD_D1, 0);
   if(dayStart == 0 || !HistorySelect(dayStart, TimeCurrent() + 3600)) return;
   for(int i = 0; i < HistoryDealsTotal(); i++)
     {
      ulong d = HistoryDealGetTicket(i);
      if(d == 0 || HistoryDealGetString(d, DEAL_SYMBOL) != _Symbol) continue;
      if((ulong)HistoryDealGetInteger(d, DEAL_MAGIC) != InpMagic) continue;
      if(HistoryDealGetInteger(d, DEAL_ENTRY) == DEAL_ENTRY_IN)
        {
         trades++;
         datetime t = (datetime)HistoryDealGetInteger(d, DEAL_TIME);
         if(t > lastEntry) lastEntry = t;
        }
      closedPL += HistoryDealGetDouble(d, DEAL_PROFIT) + HistoryDealGetDouble(d, DEAL_SWAP)
                + HistoryDealGetDouble(d, DEAL_COMMISSION) + HistoryDealGetDouble(d, DEAL_FEE);
     }
  }

//+------------------------------------------------------------------+
bool InSession()
  {
   if(!InpUseSession) return true;
   MqlDateTime t; TimeToStruct(TimeCurrent(), t);
   if(t.day_of_week == 0 || t.day_of_week == 6) return false;
   if(InpStartHour == InpEndHour) return true;
   if(InpStartHour < InpEndHour) return (t.hour >= InpStartHour && t.hour < InpEndHour);
   return (t.hour >= InpStartHour || t.hour < InpEndHour);
  }

double SpreadPrice()
  {
   return SymbolInfoDouble(_Symbol, SYMBOL_ASK) - SymbolInfoDouble(_Symbol, SYMBOL_BID);
  }

double MinStopDistance()
  {
   long lvl = MathMax(SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL),
                      SymbolInfoInteger(_Symbol, SYMBOL_TRADE_FREEZE_LEVEL));
   return lvl * _Point + SpreadPrice() + 2 * _Point;
  }

void Block(const string why)
  {
   g_reason = why;
   if(InpDebug) Print("No trade: ", why);
  }

void UpdateComment(const int tradesToday, const double dayPL, const bool lossHit, const bool targetHit)
  {
   if(MQLInfoInteger(MQL_TESTER) && !MQLInfoInteger(MQL_VISUAL_MODE)) return;
   string status = lossHit ? "STOPPED (daily loss)" : (targetHit ? "STOPPED (daily target)" : "ACTIVE");
   MqlDateTime t; TimeToStruct(TimeCurrent(), t);
   Comment(StringFormat("AI Scalper M1  |  %s\n%s  digits=%d\nP(buy)=%.2f  P(sell)=%.2f  threshold=%.2f\nSpread: %.2f   ATR: %.2f\nTrades today: %d   Day P/L: %.2f %s\nServer time: %02d:%02d  Session: %s\nLast check: %s",
                        status, _Symbol, _Digits, g_pBuy, g_pSell, InpProbThreshold,
                        SpreadPrice(), g_atr, tradesToday, dayPL, AccountInfoString(ACCOUNT_CURRENCY),
                        t.hour, t.min, InSession() ? "open" : "closed", g_reason));
  }
//+------------------------------------------------------------------+
