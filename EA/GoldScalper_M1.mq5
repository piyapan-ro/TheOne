//+------------------------------------------------------------------+
//|                                              GoldScalper_M1.mq5  |
//|        M1 scalper for XAUUSD: EMA trend + RSI pullback entries   |
//|        ATR-based SL/TP, risk-% sizing, BE, trailing, daily guard |
//+------------------------------------------------------------------+
#property copyright "Riverplus"
#property version   "1.10"
#property description "M1 scalping EA (EMA trend + RSI pullback) tuned for XAUUSD."
#property description "Test in Strategy Tester (Every tick based on real ticks) before going live."

#include <Trade\Trade.mqh>

enum ENUM_LOT_MODE
  {
   LOT_FIXED        = 0, // Fixed lot
   LOT_RISK_PERCENT = 1  // Risk % of equity per trade
  };

//--- inputs
input group "=== General ==="
input ulong           InpMagic            = 260109;          // Magic number
input string          InpComment          = "GoldScalperM1"; // Order comment
input ENUM_TIMEFRAMES InpTF               = PERIOD_M1;       // Signal timeframe

input group "=== Money management ==="
input ENUM_LOT_MODE   InpLotMode          = LOT_RISK_PERCENT; // Lot mode
input double          InpFixedLot         = 0.01;            // Fixed lot (if Fixed)
input double          InpRiskPercent      = 0.5;             // Risk % per trade (if Risk %)
input double          InpMaxLot           = 1.00;            // Max lot cap
input bool            InpForceMinLot      = true;            // Use min lot if risk-% lot is too small

input group "=== Trend filter ==="
input int             InpEmaFast          = 20;              // Fast EMA
input int             InpEmaSlow          = 50;              // Slow EMA
input bool            InpUseHTF           = true;            // Use higher-timeframe filter
input ENUM_TIMEFRAMES InpHTF              = PERIOD_M15;      // HTF timeframe
input int             InpHTFEma           = 50;              // HTF EMA period

input group "=== RSI pullback ==="
input int             InpRsiPeriod        = 7;               // RSI period
input double          InpRsiBuyLevel      = 35.0;            // Buy: RSI crosses back above
input double          InpRsiSellLevel     = 65.0;            // Sell: RSI crosses back below

input group "=== ATR stop loss / take profit ==="
input int             InpAtrPeriod        = 14;              // ATR period
input double          InpSlAtrMult        = 1.5;             // SL = ATR x
input double          InpTpAtrMult        = 2.0;             // TP = ATR x
input double          InpMinAtrPrice      = 0.30;            // Min ATR in price ($) - skip dead market (0 = off)
input double          InpMaxAtrPrice      = 15.0;            // Max ATR in price ($) - skip news spikes (0 = off)

input group "=== Trade management ==="
input bool            InpUseBreakeven     = true;            // Use breakeven
input double          InpBeTriggerAtr     = 1.0;             // BE trigger (ATR x profit)
input double          InpBeLockPrice      = 0.10;            // BE lock-in in price ($)
input bool            InpUseTrailing      = true;            // Use trailing stop
input double          InpTrailStartAtr    = 1.2;             // Trailing start (ATR x profit)
input double          InpTrailDistAtr     = 1.0;             // Trailing distance (ATR x)
input int             InpMaxBarsInTrade   = 30;              // Close after N bars (0 = off)

input group "=== Filters ==="
input double          InpMaxSpreadPrice   = 0.80;            // Max spread in price ($) (0 = off)
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
int      hEmaFast = INVALID_HANDLE, hEmaSlow = INVALID_HANDLE;
int      hRsi = INVALID_HANDLE, hAtr = INVALID_HANDLE, hHtfEma = INVALID_HANDLE;
datetime g_lastBarTime = 0;
double   g_atr = 0.0;
string   g_reason = "waiting for new bar";

//+------------------------------------------------------------------+
int OnInit()
  {
   if(InpEmaFast >= InpEmaSlow)
     {
      Print("Fast EMA must be smaller than Slow EMA");
      return(INIT_PARAMETERS_INCORRECT);
     }

   hEmaFast = iMA(_Symbol, InpTF, InpEmaFast, 0, MODE_EMA, PRICE_CLOSE);
   hEmaSlow = iMA(_Symbol, InpTF, InpEmaSlow, 0, MODE_EMA, PRICE_CLOSE);
   hRsi     = iRSI(_Symbol, InpTF, InpRsiPeriod, PRICE_CLOSE);
   hAtr     = iATR(_Symbol, InpTF, InpAtrPeriod);
   if(InpUseHTF)
      hHtfEma = iMA(_Symbol, InpHTF, InpHTFEma, 0, MODE_EMA, PRICE_CLOSE);

   if(hEmaFast == INVALID_HANDLE || hEmaSlow == INVALID_HANDLE ||
      hRsi == INVALID_HANDLE || hAtr == INVALID_HANDLE ||
      (InpUseHTF && hHtfEma == INVALID_HANDLE))
     {
      Print("Failed to create indicator handles, error ", GetLastError());
      return(INIT_FAILED);
     }

   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints((ulong)MathMax(1, MathRound(InpMaxSlippagePrice / _Point)));
   trade.SetTypeFillingBySymbol(_Symbol);
   trade.SetAsyncMode(false);

   PrintFormat("%s: digits=%d point=%g stopsLevel=%d spread=%.2f filling=%d tradeMode=%d",
               _Symbol, _Digits, _Point, (int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL),
               SpreadPrice(), (int)SymbolInfoInteger(_Symbol, SYMBOL_FILLING_MODE),
               (int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_MODE));

   if(Period() != InpTF)
      Print("Note: chart timeframe differs from signal timeframe. Signals use ", EnumToString(InpTF));

   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(hEmaFast != INVALID_HANDLE) IndicatorRelease(hEmaFast);
   if(hEmaSlow != INVALID_HANDLE) IndicatorRelease(hEmaSlow);
   if(hRsi     != INVALID_HANDLE) IndicatorRelease(hRsi);
   if(hAtr     != INVALID_HANDLE) IndicatorRelease(hAtr);
   if(hHtfEma  != INVALID_HANDLE) IndicatorRelease(hHtfEma);
   Comment("");
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   //--- update ATR every tick (closed bar value) for management
   double atrBuf[];
   ArraySetAsSeries(atrBuf, true);
   if(CopyBuffer(hAtr, 0, 1, 1, atrBuf) == 1)
      g_atr = atrBuf[0];

   //--- daily stats & protection
   int    tradesToday = 0;
   double closedPL    = 0.0;
   datetime lastEntry = 0;
   GetTodayStats(tradesToday, closedPL, lastEntry);
   double floatingPL  = GetFloatingPL();
   double dayPL       = closedPL + floatingPL;
   double dayStartBal = AccountInfoDouble(ACCOUNT_BALANCE) - closedPL;

   bool dailyLossHit = (InpMaxDailyLossPct > 0 && dayStartBal > 0 &&
                        dayPL <= -dayStartBal * InpMaxDailyLossPct / 100.0);
   bool dailyTargetHit = (InpDailyTargetPct > 0 && dayStartBal > 0 &&
                          dayPL >= dayStartBal * InpDailyTargetPct / 100.0);

   if(dailyLossHit && CountPositions() > 0)
     {
      Print("Daily loss limit reached - closing all positions");
      CloseAll();
     }

   //--- manage open positions every tick
   ManagePositions();

   UpdateComment(tradesToday, dayPL, dailyLossHit, dailyTargetHit);

   //--- entries only on a new bar
   datetime barTime = iTime(_Symbol, InpTF, 0);
   if(barTime == 0 || barTime == g_lastBarTime)
      return;
   g_lastBarTime = barTime;

   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))          { Block("Algo Trading button is OFF in terminal"); return; }
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))                    { Block("'Allow Algo Trading' unchecked in EA properties"); return; }
   if(!AccountInfoInteger(ACCOUNT_TRADE_EXPERT))             { Block("Account does not allow EA trading"); return; }
   long tmode = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_MODE);
   if(tmode == SYMBOL_TRADE_MODE_DISABLED || tmode == SYMBOL_TRADE_MODE_CLOSEONLY)
                                                             { Block("Symbol trading disabled / close-only"); return; }
   if(dailyLossHit)                                          { Block("Daily loss limit hit"); return; }
   if(dailyTargetHit)                                        { Block("Daily profit target hit"); return; }
   if(InpMaxTradesPerDay > 0 && tradesToday >= InpMaxTradesPerDay) { Block("Max trades per day reached"); return; }
   if(CountPositions() >= InpMaxPositions)                   { Block("Max positions open"); return; }
   if(!InSession())                                          { Block("Outside session hours (server time)"); return; }
   if(InpMaxSpreadPrice > 0 && SpreadPrice() > InpMaxSpreadPrice)
                                                             { Block(StringFormat("Spread %.2f > max %.2f", SpreadPrice(), InpMaxSpreadPrice)); return; }
   if(InpCooldownBars > 0 && lastEntry > 0)
     {
      int barsSince = iBarShift(_Symbol, InpTF, lastEntry, false);
      if(barsSince >= 0 && barsSince < InpCooldownBars)      { Block("Cooldown after last entry"); return; }
     }

   int signal = GetSignal();
   if(signal != 0)
      OpenTrade(signal);
   else if(InpDebug)
      PrintFormat("No signal: %s", g_reason);
  }

//+------------------------------------------------------------------+
//| Signal: +1 buy, -1 sell, 0 none (uses closed bars only)          |
//+------------------------------------------------------------------+
int GetSignal()
  {
   double emaF[], emaS[], rsi[], atr[];
   ArraySetAsSeries(emaF, true);
   ArraySetAsSeries(emaS, true);
   ArraySetAsSeries(rsi,  true);
   ArraySetAsSeries(atr,  true);

   if(CopyBuffer(hEmaFast, 0, 0, 5, emaF) < 5) return 0;
   if(CopyBuffer(hEmaSlow, 0, 0, 5, emaS) < 5) return 0;
   if(CopyBuffer(hRsi,     0, 0, 4, rsi)  < 4) return 0;
   if(CopyBuffer(hAtr,     0, 0, 2, atr)  < 2) return 0;

   if(InpMinAtrPrice > 0 && atr[1] < InpMinAtrPrice) { g_reason = StringFormat("ATR %.2f < min %.2f", atr[1], InpMinAtrPrice); return 0; }
   if(InpMaxAtrPrice > 0 && atr[1] > InpMaxAtrPrice) { g_reason = StringFormat("ATR %.2f > max %.2f", atr[1], InpMaxAtrPrice); return 0; }

   double open1  = iOpen(_Symbol,  InpTF, 1);
   double close1 = iClose(_Symbol, InpTF, 1);

   //--- higher-timeframe bias
   int htfBias = 0;
   if(InpUseHTF)
     {
      double htf[];
      ArraySetAsSeries(htf, true);
      if(CopyBuffer(hHtfEma, 0, 0, 2, htf) < 2) return 0;
      double htfClose = iClose(_Symbol, InpHTF, 1);
      htfBias = (htfClose > htf[1]) ? 1 : (htfClose < htf[1] ? -1 : 0);
     }

   bool upTrend   = emaF[1] > emaS[1] && emaF[1] > emaF[3] && close1 > emaS[1];
   bool downTrend = emaF[1] < emaS[1] && emaF[1] < emaF[3] && close1 < emaS[1];

   bool rsiBuy  = rsi[2] < InpRsiBuyLevel  && rsi[1] >= InpRsiBuyLevel;
   bool rsiSell = rsi[2] > InpRsiSellLevel && rsi[1] <= InpRsiSellLevel;

   bool bullBar = close1 > open1;
   bool bearBar = close1 < open1;

   if(upTrend && rsiBuy && bullBar && (!InpUseHTF || htfBias > 0))
      return 1;
   if(downTrend && rsiSell && bearBar && (!InpUseHTF || htfBias < 0))
      return -1;

   g_reason = StringFormat("trend %s, HTF %d, RSI %.1f->%.1f, bar %s",
                           upTrend ? "UP" : (downTrend ? "DOWN" : "none"), htfBias, rsi[2], rsi[1],
                           bullBar ? "bull" : (bearBar ? "bear" : "doji"));
   return 0;
  }

//+------------------------------------------------------------------+
void OpenTrade(const int dir)
  {
   if(g_atr <= 0) return;

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return;

   int    digits  = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double minDist = MinStopDistance();
   double slDist  = MathMax(g_atr * InpSlAtrMult, minDist);
   double tpDist  = MathMax(g_atr * InpTpAtrMult, minDist);

   double price = (dir > 0) ? tick.ask : tick.bid;
   double sl    = (dir > 0) ? price - slDist : price + slDist;
   double tp    = (dir > 0) ? price + tpDist : price - tpDist;
   sl = NormalizeDouble(sl, digits);
   tp = NormalizeDouble(tp, digits);

   double lot = CalcLot(dir, price, slDist);
   if(lot <= 0)
     {
      g_reason = "Lot size is zero (risk too small for min lot, or not enough margin)";
      Print(g_reason);
      return;
     }

   bool ok = (dir > 0) ? trade.Buy(lot, _Symbol, price, sl, tp, InpComment)
                       : trade.Sell(lot, _Symbol, price, sl, tp, InpComment);
   if(!ok || (trade.ResultRetcode() != TRADE_RETCODE_DONE && trade.ResultRetcode() != TRADE_RETCODE_PLACED))
     {
      g_reason = StringFormat("Order failed: retcode=%u %s", trade.ResultRetcode(), trade.ResultRetcodeDescription());
      Print(g_reason);
     }
   else
      PrintFormat("%s %.2f lots @ %.*f  SL %.*f  TP %.*f", dir > 0 ? "BUY" : "SELL",
                  lot, digits, price, digits, sl, digits, tp);
  }

//+------------------------------------------------------------------+
double CalcLot(const int dir, const double price, const double slDist)
  {
   double lot = InpFixedLot;

   if(InpLotMode == LOT_RISK_PERCENT)
     {
      double riskMoney = AccountInfoDouble(ACCOUNT_EQUITY) * InpRiskPercent / 100.0;
      double lossPerLot = 0.0;
      double profit = 0.0;
      ENUM_ORDER_TYPE type = (dir > 0) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      double slPrice = (dir > 0) ? price - slDist : price + slDist;

      if(OrderCalcProfit(type, _Symbol, 1.0, price, slPrice, profit))
         lossPerLot = MathAbs(profit);
      else
        {
         double tickVal  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
         double tickSize = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
         if(tickSize > 0) lossPerLot = slDist / tickSize * tickVal;
        }
      if(lossPerLot <= 0) return 0.0;
      lot = riskMoney / lossPerLot;
     }

   lot = MathMin(lot, InpMaxLot);
   double rawLot = lot;
   lot = NormalizeLot(lot);
   if(lot <= 0 && InpForceMinLot && rawLot > 0)
     {
      lot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
      PrintFormat("Risk-%% lot %.4f below min lot - using %.2f (actual risk is higher than %.2f%%)", rawLot, lot, InpRiskPercent);
     }

   //--- shrink if not enough free margin
   double margin = 0.0;
   ENUM_ORDER_TYPE mtype = (dir > 0) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double freeMargin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   double minLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   while(lot >= minLot && OrderCalcMargin(mtype, _Symbol, lot, price, margin) && margin > freeMargin * 0.9)
      lot = NormalizeLot(lot * 0.8);
   if(lot < minLot) return 0.0;
   return lot;
  }

//+------------------------------------------------------------------+
double NormalizeLot(double lot)
  {
   double minLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double step   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0) step = 0.01;
   lot = MathFloor(lot / step + 1e-9) * step;
   if(lot < minLot) return 0.0;
   if(lot > maxLot) lot = maxLot;
   int volDigits = (int)MathMax(0, MathCeil(-MathLog10(step)));
   return NormalizeDouble(lot, volDigits);
  }

//+------------------------------------------------------------------+
//| Breakeven, trailing stop, time exit                              |
//+------------------------------------------------------------------+
void ManagePositions()
  {
   if(g_atr <= 0) return;
   int    digits  = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double minDist = MinStopDistance();
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;

      long   type  = PositionGetInteger(POSITION_TYPE);
      double open  = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl    = PositionGetDouble(POSITION_SL);
      double tp    = PositionGetDouble(POSITION_TP);
      datetime opened = (datetime)PositionGetInteger(POSITION_TIME);

      //--- time exit
      if(InpMaxBarsInTrade > 0)
        {
         int bars = iBarShift(_Symbol, InpTF, opened, false);
         if(bars >= InpMaxBarsInTrade)
           {
            trade.PositionClose(ticket);
            continue;
           }
        }

      double newSL = sl;

      if(type == POSITION_TYPE_BUY)
        {
         double profitDist = bid - open;
         if(InpUseBreakeven && profitDist >= g_atr * InpBeTriggerAtr)
           {
            double be = open + InpBeLockPrice;
            if(sl < be) newSL = be;
           }
         if(InpUseTrailing && profitDist >= g_atr * InpTrailStartAtr)
           {
            double trail = bid - g_atr * InpTrailDistAtr;
            if(trail > newSL) newSL = trail;
           }
         newSL = MathMin(newSL, bid - minDist);
         newSL = NormalizeDouble(newSL, digits);
         if(newSL > sl + _Point && newSL > open - g_atr * 10)
            trade.PositionModify(ticket, newSL, tp);
        }
      else if(type == POSITION_TYPE_SELL)
        {
         double profitDist = open - ask;
         if(InpUseBreakeven && profitDist >= g_atr * InpBeTriggerAtr)
           {
            double be = open - InpBeLockPrice;
            if(sl == 0 || sl > be) newSL = be;
           }
         if(InpUseTrailing && profitDist >= g_atr * InpTrailStartAtr)
           {
            double trail = ask + g_atr * InpTrailDistAtr;
            if(newSL == 0 || trail < newSL) newSL = trail;
           }
         if(newSL != 0) newSL = MathMax(newSL, ask + minDist);
         newSL = NormalizeDouble(newSL, digits);
         if(newSL != 0 && (sl == 0 || newSL < sl - _Point))
            trade.PositionModify(ticket, newSL, tp);
        }
     }
  }

//+------------------------------------------------------------------+
void GetTodayStats(int &trades, double &closedPL, datetime &lastEntry)
  {
   trades = 0; closedPL = 0.0; lastEntry = 0;
   datetime dayStart = iTime(_Symbol, PERIOD_D1, 0);
   if(dayStart == 0) return;
   if(!HistorySelect(dayStart, TimeCurrent() + 3600)) return;

   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
     {
      ulong d = HistoryDealGetTicket(i);
      if(d == 0) continue;
      if(HistoryDealGetString(d, DEAL_SYMBOL) != _Symbol) continue;
      if((ulong)HistoryDealGetInteger(d, DEAL_MAGIC) != InpMagic) continue;

      long entry = HistoryDealGetInteger(d, DEAL_ENTRY);
      if(entry == DEAL_ENTRY_IN)
        {
         trades++;
         datetime t = (datetime)HistoryDealGetInteger(d, DEAL_TIME);
         if(t > lastEntry) lastEntry = t;
        }
      closedPL += HistoryDealGetDouble(d, DEAL_PROFIT)
                + HistoryDealGetDouble(d, DEAL_SWAP)
                + HistoryDealGetDouble(d, DEAL_COMMISSION)
                + HistoryDealGetDouble(d, DEAL_FEE);
     }
  }

//+------------------------------------------------------------------+
double GetFloatingPL()
  {
   double pl = 0.0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      pl += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
     }
   return pl;
  }

//+------------------------------------------------------------------+
int CountPositions()
  {
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      n++;
     }
   return n;
  }

//+------------------------------------------------------------------+
void CloseAll()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      trade.PositionClose(ticket);
     }
  }

//+------------------------------------------------------------------+
bool InSession()
  {
   if(!InpUseSession) return true;
   MqlDateTime t;
   TimeToStruct(TimeCurrent(), t);
   if(t.day_of_week == 0 || t.day_of_week == 6) return false;
   if(InpStartHour == InpEndHour) return true;
   if(InpStartHour < InpEndHour)
      return (t.hour >= InpStartHour && t.hour < InpEndHour);
   return (t.hour >= InpStartHour || t.hour < InpEndHour); // overnight window
  }

//+------------------------------------------------------------------+
double SpreadPrice()
  {
   return SymbolInfoDouble(_Symbol, SYMBOL_ASK) - SymbolInfoDouble(_Symbol, SYMBOL_BID);
  }

//+------------------------------------------------------------------+
void Block(const string why)
  {
   g_reason = why;
   if(InpDebug) Print("No trade: ", why);
  }

//+------------------------------------------------------------------+
double MinStopDistance()
  {
   long stops  = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   long freeze = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_FREEZE_LEVEL);
   long lvl = MathMax(stops, freeze);
   return lvl * _Point + SpreadPrice() + 2 * _Point;
  }

//+------------------------------------------------------------------+
void UpdateComment(const int tradesToday, const double dayPL, const bool lossHit, const bool targetHit)
  {
   if(MQLInfoInteger(MQL_TESTER) && !MQLInfoInteger(MQL_VISUAL_MODE)) return;
   string status = lossHit ? "STOPPED (daily loss)" : (targetHit ? "STOPPED (daily target)" : "ACTIVE");
   MqlDateTime t; TimeToStruct(TimeCurrent(), t);
   Comment(StringFormat("GoldScalper M1  |  %s\n%s  digits=%d\nSpread: %.2f   ATR: %.2f\nTrades today: %d   Day P/L: %.2f %s\nServer time: %02d:%02d  Session: %s\nLast check: %s",
                        status, _Symbol, _Digits, SpreadPrice(), g_atr, tradesToday, dayPL,
                        AccountInfoString(ACCOUNT_CURRENCY), t.hour, t.min, InSession() ? "open" : "closed", g_reason));
  }
//+------------------------------------------------------------------+
