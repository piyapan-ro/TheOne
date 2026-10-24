//+------------------------------------------------------------------+
//|                                                    AIEA_M5.mq5   |
//|   AI EA: closed M5 bars + S/R + Order Blocks + structure + H1/H4 |
//|   -> AI scores BUY vs SELL (Dashboard /api/ea/decide)            |
//|   -> EA confirms on closed M5 bars and sends one market order    |
//+------------------------------------------------------------------+
#property copyright "Riverplus"
#property version   "1.00"
#property description "AI EA for XAUUSD M5. Needs the AI Trader Dashboard (start.bat) running on this PC."
#property description "Tools > Options > Expert Advisors > Allow WebRequest for: http://127.0.0.1:8000"

#include <Trade\Trade.mqh>

enum ENUM_LOT_MODE
  {
   LOT_FIXED        = 0, // Fixed lot
   LOT_RISK_PERCENT = 1  // Risk % of equity per trade
  };

input group "=== AI bridge (Dashboard) ==="
input string          InpBridgeUrl        = "http://127.0.0.1:8000"; // Dashboard URL (add to WebRequest list)
input string          InpApiToken         = "";              // API token: dashboard\data\api_token.txt
input int             InpTimeoutMs        = 60000;           // AI request timeout (ms)
input int             InpRetryMinutes     = 5;               // After an AI error: no new orders for N minutes

input group "=== General ==="
input ulong           InpMagic            = 260120;          // Magic number
input string          InpComment          = "AIEA_M5";       // Order comment

input group "=== Money management ==="
input ENUM_LOT_MODE   InpLotMode          = LOT_RISK_PERCENT; // Lot mode
input double          InpFixedLot         = 0.01;            // Fixed lot (if Fixed)
input double          InpRiskPercent      = 0.5;             // Risk % per trade (if Risk %)
input double          InpMaxLot           = 1.00;            // Max lot cap
input bool            InpForceMinLot      = false;           // Use min lot if risk-% lot is too small

input group "=== SL / TP ==="
input double          InpSlBufferAtr      = 0.30;            // SL = zone invalidation +/- ATR x
input double          InpMinSlAtr         = 0.80;            // Min SL distance (ATR x)
input double          InpMaxSlAtr         = 3.00;            // Max SL distance (ATR x) - wider = no trade
input double          InpRR               = 2.0;             // TP = SL distance x

input group "=== Breakeven / trailing ==="
input bool            InpUseBreakeven     = true;            // Use breakeven
input double          InpBeTriggerR       = 1.0;             // BE when profit >= R x
input double          InpBeLockPrice      = 0.10;            // BE lock-in in price ($)
input bool            InpUseTrailing      = true;            // Use trailing stop
input double          InpTrailStartR      = 1.5;             // Trailing starts at profit R x
input double          InpTrailDistAtr     = 1.0;             // Trailing distance (ATR x)

input group "=== Entry confirmation ==="
input double          InpMaxEntryDistAtr  = 0.50;            // Max distance price <-> entry (ATR x), else AI WAIT
input int             InpSignalMaxAgeBars = 2;               // Confirmation bar must be this fresh (closed M5 bars)
input double          InpBreakoutCloseAtr = 0.10;            // Breakout: close beyond level by ATR x

input group "=== AI call control (cost) ==="
input double          InpZoneNearAtr      = 1.5;             // Ask AI only if price is within N x ATR of a zone...
input int             InpEventLookback    = 6;               // ...or a BOS/CHOCH/sweep/break happened in N bars
input int             InpMinBarsBetween   = 3;               // Min closed M5 bars between two AI calls
input int             InpNoneCooldownBars = 3;               // Bars to wait after AI answered NONE
input int             InpMaxCallsPerDay   = 40;              // Max AI calls per day (0 = off)

input group "=== Analysis ==="
input int             InpPivot            = 3;               // Swing pivot strength (bars each side)
input int             InpSendBars         = 36;              // Closed M5 bars sent to the AI
input int             InpZonesPerSide     = 3;               // Zones per side sent to the AI
input double          InpObImpulseAtr     = 1.0;             // Order block: displacement after it (ATR x)

input group "=== Filters ==="
input double          InpMaxSpreadPrice   = 0.80;            // Max spread in price ($) (0 = off)
input bool            InpUseSession       = true;            // Use trading session
input int             InpStartHour        = 8;               // Session start hour (server time)
input int             InpEndHour          = 22;              // Session end hour (server time)
input double          InpMaxSlippagePrice = 0.50;            // Max slippage in price ($)
input int             InpMaxTradesPerDay  = 10;              // Max entries per day (0 = off)
input double          InpMaxDailyLossPct  = 3.0;             // Max daily loss % (0 = off)
input bool            InpDebug            = true;            // Print decisions (Experts tab)

//--- types
struct Zone
  {
   string            id;
   string            kind;     // support | resistance | bull_ob | bear_ob
   string            tf;
   double            lo;
   double            hi;
   int               touches;
   int               age;      // M5 bars since the zone was made / last touched
   string            flip;     // broken_up | broken_down | ""
   int               flipAgo;
  };

struct Swing
  {
   int               idx;
   double            price;
   bool              isHigh;
  };

struct Structure
  {
   int               trend;    // 1 up, -1 down, 0 range
   int               bosDir;   double bosLevel;   int bosAgo;
   int               chochDir; double chochLevel; int chochAgo;
   string            sweeps;   // JSON array
   int               sweepAgo; // newest sweep, bars ago (999 = none)
   double            lastHigh; double lastLow;
  };

struct Plan
  {
   bool              active;
   string            id;
   int               dir;      // 1 buy, -1 sell
   string            setup;    // breakout | retest | smc_retest
   string            zoneId;
   double            zLo, zHi, entry, inval;
   int               validBars;
   datetime          created;  // bar time of the AI call
   int               buyScore, sellScore;
   string            note;
  };

//--- globals
CTrade    trade;
datetime  g_lastBar = 0;
double    g_atr = 0.0;
Plan      g_plan;
string    g_status = "AI READY";
string    g_reason = "waiting for a closed M5 bar";
datetime  g_lastCallBar = 0;
datetime  g_noneUntilBar = 0;
datetime  g_failUntil = 0;
int       g_callsToday = 0;
int       g_callsDay = -1;
double    g_costToday = 0.0;
int       g_lastBuy = 0, g_lastSell = 0;
Zone      g_zones[];
Structure g_m5;

//+------------------------------------------------------------------+
int OnInit()
  {
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints((ulong)MathMax(1, MathRound(InpMaxSlippagePrice / _Point)));
   trade.SetTypeFillingBySymbol(_Symbol);
   g_plan.active = false;
   if(InpApiToken == "")
      Print("AIEA: InpApiToken is empty - copy it from dashboard\\data\\api_token.txt");
   if(MQLInfoInteger(MQL_TESTER))
      Print("AIEA: WebRequest does not work in the Strategy Tester - the EA will not trade there.");
   EventSetTimer(1);
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   Comment("");
  }

void OnTimer() { UpdatePanel(); }

//+------------------------------------------------------------------+
void OnTick()
  {
   int tradesToday = 0; double closedPL = 0.0; datetime lastEntry = 0;
   GetTodayStats(tradesToday, closedPL, lastEntry);
   double dayPL       = closedPL + GetFloatingPL();
   double dayStartBal = AccountInfoDouble(ACCOUNT_BALANCE) - closedPL;
   bool lossHit = (InpMaxDailyLossPct > 0 && dayStartBal > 0 && dayPL <= -dayStartBal * InpMaxDailyLossPct / 100.0);

   ManagePositions();

   datetime bar = iTime(_Symbol, PERIOD_M5, 0);
   bool newBar = (bar != 0 && bar != g_lastBar);
   if(newBar)
     {
      g_lastBar = bar;
      ResetDailyCounter();
      if(!Analyze()) { g_status = "NO DATA"; return; }
     }
   if(g_atr <= 0) return;

   //--- gates for NEW orders (open positions keep their SL/TP/trailing whatever happens here)
   string gate = "";
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))                gate = "Algo Trading button is OFF";
   else if(!MQLInfoInteger(MQL_TRADE_ALLOWED))                     gate = "'Allow Algo Trading' unchecked";
   else if(lossHit)                                                gate = "Daily loss limit hit";
   else if(InpMaxTradesPerDay > 0 && tradesToday >= InpMaxTradesPerDay) gate = "Max trades per day reached";
   else if(CountPositions() > 0)                                   gate = "1 position open - managing it";
   else if(!InSession())                                           gate = "Outside session hours";
   else if(TimeCurrent() < g_failUntil)                            gate = "AI unavailable - no new orders";
   if(gate != "")
     {
      if(g_plan.active && CountPositions() == 0) DropPlan("cancelled", gate);
      if(TimeCurrent() >= g_failUntil) g_status = "AI PAUSED";
      g_reason = gate;
      return;
     }

   if(g_plan.active)
     {
      CheckPlan(newBar);
      return;
     }
   if(newBar && WantAI())
      AskAI();
  }

//+------------------------------------------------------------------+
//| Market analysis (closed bars only)                               |
//+------------------------------------------------------------------+
MqlRates g_m5r[], g_h1r[], g_h4r[];
string   g_h1ctx = "", g_h4ctx = "";

bool Analyze()
  {
   ArraySetAsSeries(g_m5r, false); ArraySetAsSeries(g_h1r, false); ArraySetAsSeries(g_h4r, false);
   int n5 = CopyRates(_Symbol, PERIOD_M5, 1, 300, g_m5r);
   int n1 = CopyRates(_Symbol, PERIOD_H1, 1, 260, g_h1r);
   int n4 = CopyRates(_Symbol, PERIOD_H4, 1, 260, g_h4r);
   if(n5 < 120 || n1 < 60 || n4 < 60) { g_reason = "Not enough M5/H1/H4 history"; return false; }

   g_atr = Atr(g_m5r, n5, 14);
   double atrH1 = Atr(g_h1r, n1, 14);
   if(g_atr <= 0 || atrH1 <= 0) { g_reason = "ATR is zero"; return false; }

   Swing s5[], s1[];
   FindSwings(g_m5r, n5, s5);
   FindSwings(g_h1r, n1, s1);
   ReadStructure(g_m5r, n5, s5, g_m5);

   ArrayResize(g_zones, 0);
   double price = g_m5r[n5 - 1].close;
   BuildSRZones(s5, n5, "M5", g_atr * 0.25, 1, price);
   BuildSRZones(s1, n1, "H1", atrH1 * 0.20, 12, price);
   BuildOrderBlocks(price);
   KeepNearest(price);

   g_h1ctx = TfContext(g_h1r, n1, atrH1);
   g_h4ctx = TfContext(g_h4r, n4, Atr(g_h4r, n4, 14));
   return true;
  }

double Atr(const MqlRates &r[], const int n, const int len)
  {
   if(n <= len) return 0.0;
   double s = 0.0;
   for(int i = n - len; i < n; i++)
      s += MathMax(r[i].high - r[i].low, MathMax(MathAbs(r[i].high - r[i - 1].close), MathAbs(r[i].low - r[i - 1].close)));
   return s / len;
  }

double Ema(const MqlRates &r[], const int n, const int len)
  {
   double a = 2.0 / (len + 1.0), e = r[0].close;
   for(int i = 1; i < n; i++) e = a * r[i].close + (1.0 - a) * e;
   return e;
  }

void FindSwings(const MqlRates &r[], const int n, Swing &out[])
  {
   ArrayResize(out, 0);
   int p = MathMax(1, InpPivot);
   for(int i = p; i < n - p; i++)
     {
      bool hi = true, lo = true;
      for(int k = 1; k <= p && (hi || lo); k++)
        {
         if(r[i - k].high >= r[i].high || r[i + k].high > r[i].high) hi = false;
         if(r[i - k].low  <= r[i].low  || r[i + k].low  < r[i].low)  lo = false;
        }
      if(hi) { int m = ArraySize(out); ArrayResize(out, m + 1); out[m].idx = i; out[m].price = r[i].high; out[m].isHigh = true; }
      if(lo) { int m = ArraySize(out); ArrayResize(out, m + 1); out[m].idx = i; out[m].price = r[i].low;  out[m].isHigh = false; }
     }
  }

//--- BOS / CHOCH(MSS) / liquidity sweeps, walking the bars oldest -> newest
void ReadStructure(const MqlRates &r[], const int n, const Swing &sw[], Structure &s)
  {
   int p = MathMax(1, InpPivot);
   s.trend = 0; s.bosDir = 0; s.bosAgo = 999; s.bosLevel = 0; s.chochDir = 0; s.chochAgo = 999; s.chochLevel = 0;
   s.sweeps = ""; s.sweepAgo = 999; s.lastHigh = 0; s.lastLow = 0;
   double sh = 0, sl = 0; bool shUsed = true, slUsed = true;
   int next = 0, nsw = ArraySize(sw), sweepsN = 0;
   string sweeps[];
   for(int i = 0; i < n; i++)
     {
      while(next < nsw && sw[next].idx + p < i)   // swing confirmed p bars after the pivot
        {
         if(sw[next].isHigh) { sh = sw[next].price; shUsed = false; }
         else                { sl = sw[next].price; slUsed = false; }
         next++;
        }
      int ago = n - 1 - i;
      if(!shUsed && sh > 0)
        {
         if(r[i].close > sh)
           {
            if(s.trend == -1) { s.chochDir = 1; s.chochLevel = sh; s.chochAgo = ago; }
            else              { s.bosDir = 1;   s.bosLevel = sh;   s.bosAgo = ago; }
            s.trend = 1; shUsed = true;
           }
         else if(r[i].high > sh)
           {
            int m = ArraySize(sweeps); ArrayResize(sweeps, m + 1);
            sweeps[m] = StringFormat("{\"side\":\"buy_side\",\"level\":%s,\"bars_ago\":%d}", D(sh), ago);
            if(ago < s.sweepAgo) s.sweepAgo = ago;
            shUsed = true;   // liquidity taken
           }
        }
      if(!slUsed && sl > 0)
        {
         if(r[i].close < sl)
           {
            if(s.trend == 1) { s.chochDir = -1; s.chochLevel = sl; s.chochAgo = ago; }
            else             { s.bosDir = -1;   s.bosLevel = sl;   s.bosAgo = ago; }
            s.trend = -1; slUsed = true;
           }
         else if(r[i].low < sl)
           {
            int m = ArraySize(sweeps); ArrayResize(sweeps, m + 1);
            sweeps[m] = StringFormat("{\"side\":\"sell_side\",\"level\":%s,\"bars_ago\":%d}", D(sl), ago);
            if(ago < s.sweepAgo) s.sweepAgo = ago;
            slUsed = true;
           }
        }
     }
   s.lastHigh = sh; s.lastLow = sl;
   int total = ArraySize(sweeps), from = MathMax(0, total - 3);
   for(int k = from; k < total; k++) s.sweeps += (k > from ? "," : "") + sweeps[k];
   s.sweeps = "[" + s.sweeps + "]";
  }

//--- support / resistance: clusters of swing points
void BuildSRZones(const Swing &sw[], const int n, const string tf, const double tol, const int m5PerBar, const double price)
  {
   int nsw = ArraySize(sw);
   bool used[]; ArrayResize(used, nsw); ArrayInitialize(used, false);
   for(int i = nsw - 1; i >= 0; i--)
     {
      if(used[i]) continue;
      double lo = sw[i].price, hi = sw[i].price; int touches = 1, newest = sw[i].idx;
      used[i] = true;
      for(int j = i - 1; j >= 0; j--)
         if(!used[j] && sw[j].price >= lo - tol && sw[j].price <= hi + tol)
           {
            lo = MathMin(lo, sw[j].price); hi = MathMax(hi, sw[j].price); touches++; used[j] = true;
           }
      double minW = g_atr * 0.2;
      if(hi - lo < minW) { double mid = (hi + lo) / 2; lo = mid - minW / 2; hi = mid + minW / 2; }
      Zone z;
      z.tf = tf; z.lo = lo; z.hi = hi; z.touches = touches; z.age = (n - 1 - newest) * m5PerBar;
      if(lo > price)      z.kind = "resistance";
      else if(hi < price) z.kind = "support";
      else                z.kind = (price - lo > hi - price) ? "support" : "resistance";
      SetFlip(z);
      AddZone(z);
     }
  }

//--- did a closed M5 bar break through the zone in the last InpEventLookback bars (and price stays beyond)?
void SetFlip(Zone &z)
  {
   z.flip = ""; z.flipAgo = 999;
   int n = ArraySize(g_m5r);
   double last = g_m5r[n - 1].close, need = g_atr * InpBreakoutCloseAtr;
   for(int k = n - 1; k >= MathMax(1, n - InpEventLookback); k--)
     {
      if(g_m5r[k].close > z.hi + need && g_m5r[k - 1].close <= z.hi && last > z.hi)
        { z.flip = "broken_up";   z.flipAgo = n - 1 - k; return; }
      if(g_m5r[k].close < z.lo - need && g_m5r[k - 1].close >= z.lo && last < z.lo)
        { z.flip = "broken_down"; z.flipAgo = n - 1 - k; return; }
     }
  }

//--- order blocks: last opposite candle before a displacement, not yet invalidated
void BuildOrderBlocks(const double price)
  {
   int n = ArraySize(g_m5r), bulls = 0, bears = 0;
   for(int i = n - 4; i >= MathMax(1, n - 150) && (bulls < InpZonesPerSide || bears < InpZonesPerSide); i--)
     {
      double hiAfter = MathMax(g_m5r[i + 1].close, MathMax(g_m5r[i + 2].close, g_m5r[i + 3].close));
      double loAfter = MathMin(g_m5r[i + 1].close, MathMin(g_m5r[i + 2].close, g_m5r[i + 3].close));
      bool bull = g_m5r[i].close < g_m5r[i].open && hiAfter > g_m5r[i].high + g_atr * InpObImpulseAtr;
      bool bear = g_m5r[i].close > g_m5r[i].open && loAfter < g_m5r[i].low  - g_atr * InpObImpulseAtr;
      if(!bull && !bear) continue;
      Zone z;
      z.tf = "M5"; z.touches = 0; z.flip = ""; z.flipAgo = 999; z.age = n - 1 - i;
      if(bull) { z.kind = "bull_ob"; z.lo = g_m5r[i].low;  z.hi = g_m5r[i].open; }
      else     { z.kind = "bear_ob"; z.lo = g_m5r[i].open; z.hi = g_m5r[i].high; }
      bool dead = false;
      for(int j = i + 1; j < n && !dead; j++)
        {
         if(bull && g_m5r[j].close < z.lo) dead = true;
         if(bear && g_m5r[j].close > z.hi) dead = true;
         if(j > i + 3 && ((bull && g_m5r[j].low <= z.hi) || (bear && g_m5r[j].high >= z.lo))) z.touches++;
        }
      if(dead || (bull && bulls >= InpZonesPerSide) || (bear && bears >= InpZonesPerSide)) continue;
      if(bull) bulls++; else bears++;
      AddZone(z);
     }
  }

void AddZone(const Zone &z)
  {
   int m = ArraySize(g_zones);
   ArrayResize(g_zones, m + 1);
   g_zones[m] = z;
  }

//--- keep the nearest InpZonesPerSide zones of each kind and give them ids S1, R1, BullOB1, BearOB1 ...
void KeepNearest(const double price)
  {
   string kinds[4] = {"support", "resistance", "bull_ob", "bear_ob"};
   string pref[4]  = {"S", "R", "BullOB", "BearOB"};
   Zone out[];
   for(int k = 0; k < 4; k++)
     {
      bool taken[]; ArrayResize(taken, ArraySize(g_zones)); ArrayInitialize(taken, false);
      for(int c = 1; c <= InpZonesPerSide; c++)
        {
         int best = -1; double bestD = DBL_MAX;
         for(int i = 0; i < ArraySize(g_zones); i++)
           {
            if(taken[i] || g_zones[i].kind != kinds[k]) continue;
            double d = MathMin(MathAbs(price - g_zones[i].lo), MathAbs(price - g_zones[i].hi));
            if(d < bestD) { bestD = d; best = i; }
           }
         if(best < 0) break;
         taken[best] = true;
         int m = ArraySize(out); ArrayResize(out, m + 1);
         out[m] = g_zones[best];
         out[m].id = pref[k] + IntegerToString(c);
        }
     }
   ArrayResize(g_zones, ArraySize(out));
   for(int i = 0; i < ArraySize(out); i++) g_zones[i] = out[i];
  }

string TfContext(const MqlRates &r[], const int n, const double atr)
  {
   Swing sw[]; Structure s;
   FindSwings(r, n, sw);
   ReadStructure(r, n, sw, s);
   double e50 = Ema(r, n, 50), e200 = Ema(r, n, 200), c = r[n - 1].close;
   string trend = (c > e50 && e50 > e200) ? "up" : ((c < e50 && e50 < e200) ? "down" : "mixed");
   return StringFormat("{\"close\":%s,\"ema50\":%s,\"ema200\":%s,\"ema_trend\":\"%s\",\"structure\":\"%s\",\"atr\":%s,"
                       "\"swing_high\":%s,\"swing_low\":%s,\"last_bos\":%s,\"last_choch\":%s}",
                       D(c), D(e50), D(e200), trend, TrendName(s.trend), D(atr), D(s.lastHigh), D(s.lastLow),
                       EventJson(s.bosDir, s.bosLevel, s.bosAgo), EventJson(s.chochDir, s.chochLevel, s.chochAgo));
  }

//+------------------------------------------------------------------+
//| AI call                                                          |
//+------------------------------------------------------------------+
bool WantAI()
  {
   int n = ArraySize(g_m5r);
   if(n == 0) return false;
   if(InpMaxSpreadPrice > 0 && SpreadPrice() > InpMaxSpreadPrice)
     { g_status = "AI WAIT"; g_reason = StringFormat("Spread %.2f > %.2f", SpreadPrice(), InpMaxSpreadPrice); return false; }
   if(InpMaxCallsPerDay > 0 && g_callsToday >= InpMaxCallsPerDay)
     { g_status = "AI PAUSED"; g_reason = "Max AI calls per day reached"; return false; }
   if(g_lastCallBar > 0 && BarsSince(g_lastCallBar) < InpMinBarsBetween)
     { g_reason = "Min bars between AI calls"; return false; }
   if(g_noneUntilBar > 0 && g_lastBar < g_noneUntilBar)
     { g_reason = "Cooldown after AI NONE"; return false; }

   // only pay for an AI call when something worth judging is near
   double price = g_m5r[n - 1].close, near = DBL_MAX;
   bool event = (g_m5.bosAgo < InpEventLookback || g_m5.chochAgo < InpEventLookback || g_m5.sweepAgo < InpEventLookback);
   for(int i = 0; i < ArraySize(g_zones); i++)
     {
      near = MathMin(near, MathMin(MathAbs(price - g_zones[i].lo), MathAbs(price - g_zones[i].hi)));
      if(g_zones[i].flip != "") event = true;
     }
   if(!event && near > InpZoneNearAtr * g_atr)
     { g_status = "AI IDLE"; g_reason = "No zone or structure event near price - AI not called"; return false; }
   return true;
  }

void AskAI()
  {
   g_lastCallBar = g_lastBar;
   g_callsToday++;
   string resp;
   if(!PostJson("/api/ea/decide", BuildRequest(), InpTimeoutMs, resp))
     {
      AiFailed(resp);
      return;
     }
   if(JsonGet(resp, "ok") != "true")
     {
      AiFailed(JsonGet(resp, "error"));
      return;
     }
   g_costToday += StringToDouble(JsonGet(resp, "cost_usd"));
   g_lastBuy  = (int)StringToInteger(JsonGet(resp, "buy_score"));
   g_lastSell = (int)StringToInteger(JsonGet(resp, "sell_score"));
   string decision = JsonGet(resp, "decision");
   string note = JsonGet(resp, "note"), reject = JsonGet(resp, "reject");
   if(decision != "BUY" && decision != "SELL")
     {
      g_status = "AI NONE";
      g_reason = (reject != "" ? reject : "no setup") + (note != "" ? " | " + note : "");
      g_noneUntilBar = g_lastBar + InpNoneCooldownBars * PeriodSeconds(PERIOD_M5);
      if(InpDebug) PrintFormat("AI NONE  buy=%d sell=%d  %s", g_lastBuy, g_lastSell, g_reason);
      return;
     }
   g_plan.active    = true;
   g_plan.id        = JsonGet(resp, "plan_id");
   g_plan.dir       = (decision == "BUY") ? 1 : -1;
   g_plan.setup     = JsonGet(resp, "setup");
   g_plan.zoneId    = JsonGet(resp, "zone_id");
   g_plan.zLo       = StringToDouble(JsonGet(resp, "zone_low"));
   g_plan.zHi       = StringToDouble(JsonGet(resp, "zone_high"));
   g_plan.entry     = StringToDouble(JsonGet(resp, "entry"));
   g_plan.inval     = StringToDouble(JsonGet(resp, "invalidation"));
   g_plan.validBars = (int)StringToInteger(JsonGet(resp, "valid_bars"));
   g_plan.created   = g_lastBar;
   g_plan.buyScore  = g_lastBuy;
   g_plan.sellScore = g_lastSell;
   g_plan.note      = note;
   g_status = "AI WAIT";
   g_reason = StringFormat("%s %s @ %s (%s) - waiting for confirmation", decision, g_plan.setup, D(g_plan.entry), g_plan.zoneId);
   if(InpDebug) PrintFormat("AI PLAN %s  buy=%d sell=%d  %s  %s", g_plan.id, g_lastBuy, g_lastSell, g_reason, note);
   CheckPlan(true);
  }

void AiFailed(const string why)
  {
   g_failUntil = TimeCurrent() + InpRetryMinutes * 60;
   g_status = "AI OFFLINE";
   g_reason = "AI error: " + why + StringFormat(" - no new orders for %d min", InpRetryMinutes);
   Print(g_reason);
  }

string BuildRequest()
  {
   int n = ArraySize(g_m5r);
   string bars = "";
   for(int i = MathMax(0, n - InpSendBars); i < n; i++)
      bars += (bars == "" ? "" : ",") + StringFormat("[%s,%s,%s,%s]", D(g_m5r[i].open), D(g_m5r[i].high), D(g_m5r[i].low), D(g_m5r[i].close));
   string zones = "";
   for(int i = 0; i < ArraySize(g_zones); i++)
     {
      Zone z = g_zones[i];
      zones += (i > 0 ? "," : "") + StringFormat("{\"id\":\"%s\",\"kind\":\"%s\",\"tf\":\"%s\",\"low\":%s,\"high\":%s,\"touches\":%d,\"age_bars\":%d,\"flip\":\"%s\"%s}",
                                                 z.id, z.kind, z.tf, D(z.lo), D(z.hi), z.touches, z.age, z.flip,
                                                 z.flip != "" ? StringFormat(",\"flip_bars_ago\":%d", z.flipAgo) : "");
     }
   MqlTick t; SymbolInfoTick(_Symbol, t);
   string structure = StringFormat("{\"trend\":\"%s\",\"swing_high\":%s,\"swing_low\":%s,\"last_bos\":%s,\"last_choch\":%s,\"sweeps\":%s}",
                                   TrendName(g_m5.trend), D(g_m5.lastHigh), D(g_m5.lastLow),
                                   EventJson(g_m5.bosDir, g_m5.bosLevel, g_m5.bosAgo),
                                   EventJson(g_m5.chochDir, g_m5.chochLevel, g_m5.chochAgo), g_m5.sweeps);
   return StringFormat("{\"symbol\":\"%s\",\"server_time\":\"%s\",\"bar_time\":\"%s\",\"price\":{\"bid\":%s,\"ask\":%s,\"spread\":%s},"
                       "\"atr_m5\":%s,\"m5_ema20\":%s,\"m5_ema50\":%s,\"m5_bars_ohlc\":[%s],\"zones\":[%s],\"structure\":%s,\"h1\":%s,\"h4\":%s}",
                       _Symbol, TimeToString(TimeCurrent(), TIME_DATE | TIME_MINUTES), TimeToString(g_m5r[n - 1].time, TIME_DATE | TIME_MINUTES),
                       D(t.bid), D(t.ask), D(t.ask - t.bid), D(g_atr), D(Ema(g_m5r, n, 20)), D(Ema(g_m5r, n, 50)),
                       bars, zones, structure, g_h1ctx, g_h4ctx);
  }

//+------------------------------------------------------------------+
//| Confirmation before the market order                              |
//+------------------------------------------------------------------+
void CheckPlan(const bool newBar)
  {
   int n = ArraySize(g_m5r);
   if(n < 2) return;
   int dir = g_plan.dir;
   if(newBar)
     {
      if(BarsSince(g_plan.created) > g_plan.validBars) { DropPlan("expired", "plan expired (signal no longer fresh)"); return; }
      double c = g_m5r[n - 1].close;
      if((dir > 0 && c < g_plan.inval) || (dir < 0 && c > g_plan.inval))
        { DropPlan("cancelled", "setup invalid: closed beyond the zone"); return; }
     }

   //--- 1) a closed M5 bar confirms the setup, 2) it is fresh
   int ago = ConfirmBarAgo();
   if(ago < 0)
     {
      g_status = "AI WAIT";
      g_reason = StringFormat("%s %s %s: waiting for a closed M5 confirmation bar", dir > 0 ? "BUY" : "SELL", g_plan.setup, g_plan.zoneId);
      return;
     }

   //--- 3) distance from the entry
   MqlTick t;
   if(!SymbolInfoTick(_Symbol, t)) return;
   double px = dir > 0 ? t.ask : t.bid;
   double dist = MathAbs(px - g_plan.entry);
   if(dist > InpMaxEntryDistAtr * g_atr)
     {
      g_status = "AI WAIT";
      g_reason = StringFormat("price %.2f is %.2f from entry %s (max %.2f) - not chasing", px, dist, D(g_plan.entry), InpMaxEntryDistAtr * g_atr);
      return;
     }
   if((dir > 0 && t.bid <= g_plan.inval) || (dir < 0 && t.ask >= g_plan.inval))
     { g_status = "AI WAIT"; g_reason = "price is beyond the invalidation level"; return; }
   if(InpMaxSpreadPrice > 0 && SpreadPrice() > InpMaxSpreadPrice)
     { g_status = "AI WAIT"; g_reason = StringFormat("Spread %.2f > %.2f", SpreadPrice(), InpMaxSpreadPrice); return; }

   //--- 4) SL / TP / lot
   double sl = dir > 0 ? g_plan.inval - InpSlBufferAtr * g_atr : g_plan.inval + InpSlBufferAtr * g_atr;
   double slDist = MathAbs(px - sl);
   slDist = MathMax(slDist, MathMax(InpMinSlAtr * g_atr, MinStopDistance()));
   if(slDist > InpMaxSlAtr * g_atr)
     { g_status = "AI WAIT"; g_reason = StringFormat("SL %.2f wider than %.1f ATR", slDist, InpMaxSlAtr); return; }
   sl = NormalizeDouble(dir > 0 ? px - slDist : px + slDist, _Digits);
   double tp = NormalizeDouble(dir > 0 ? px + slDist * InpRR : px - slDist * InpRR, _Digits);
   double lot = CalcLot(dir, px, slDist);
   if(lot <= 0) { DropPlan("cancelled", "lot is zero (risk too small for min lot, or no margin)"); return; }

   string cmt = StringFormat("%s %s %d/%d", InpComment, g_plan.setup, g_plan.buyScore, g_plan.sellScore);
   bool ok = dir > 0 ? trade.Buy(lot, _Symbol, px, sl, tp, cmt) : trade.Sell(lot, _Symbol, px, sl, tp, cmt);
   uint rc = trade.ResultRetcode();
   if(!ok || (rc != TRADE_RETCODE_DONE && rc != TRADE_RETCODE_PLACED))
     {
      g_reason = StringFormat("Order failed: retcode=%u %s", rc, trade.ResultRetcodeDescription());
      Print(g_reason);
      return;   // plan stays; next tick re-checks every condition
     }
   PrintFormat("AI %s %.2f lots @ %s SL %s TP %s  plan %s (%s %s)", dir > 0 ? "BUY" : "SELL", lot, D(px), D(sl), D(tp),
               g_plan.id, g_plan.setup, g_plan.zoneId);
   string resp;
   PostJson("/api/ea/report", StringFormat("{\"plan_id\":\"%s\",\"status\":\"opened\",\"ticket\":%I64u,\"price\":%s,\"lot\":%.2f}",
                                           g_plan.id, trade.ResultOrder(), D(px), lot), 5000, resp);
   g_status = dir > 0 ? "AI BUY" : "AI SELL";
   g_reason = StringFormat("opened %s at %s", g_plan.setup, D(px));
   g_plan.active = false;
  }

//--- most recent closed bar (1..InpSignalMaxAgeBars ago, 0 = last closed) that confirms the plan, or -1
int ConfirmBarAgo()
  {
   int n = ArraySize(g_m5r);
   double tol = 0.1 * g_atr, mid = (g_plan.zLo + g_plan.zHi) / 2;
   for(int ago = 0; ago < MathMax(1, InpSignalMaxAgeBars); ago++)
     {
      int i = n - 1 - ago;
      if(i < 1) break;
      MqlRates b = g_m5r[i];
      bool ok;
      if(g_plan.setup == "breakout")
         ok = g_plan.dir > 0 ? (b.close > g_plan.zHi + InpBreakoutCloseAtr * g_atr && b.close > b.open)
                             : (b.close < g_plan.zLo - InpBreakoutCloseAtr * g_atr && b.close < b.open);
      else   // retest / smc_retest: touch the zone and close back in our direction
         ok = g_plan.dir > 0 ? (b.low <= g_plan.zHi + tol && b.close >= mid && b.close > b.open)
                             : (b.high >= g_plan.zLo - tol && b.close <= mid && b.close < b.open);
      if(ok) return ago;
     }
   return -1;
  }

void DropPlan(const string status, const string why)
  {
   if(!g_plan.active) return;
   string resp;
   PostJson("/api/ea/report", StringFormat("{\"plan_id\":\"%s\",\"status\":\"%s\",\"reason\":\"%s\"}", g_plan.id, status, JsonEscape(why)), 5000, resp);
   if(InpDebug) PrintFormat("AI plan %s %s: %s", g_plan.id, status, why);
   g_plan.active = false;
   g_status = "AI READY";
   g_reason = why;
  }

//+------------------------------------------------------------------+
//| Risk / trade management                                          |
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

//--- breakeven + ATR trailing; R = distance open -> TP / InpRR (= the initial SL distance)
void ManagePositions()
  {
   if(g_atr <= 0 || (!InpUseBreakeven && !InpUseTrailing)) return;
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double minDist = MinStopDistance();
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !IsMine()) continue;
      long   type = PositionGetInteger(POSITION_TYPE);
      double open = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl   = PositionGetDouble(POSITION_SL);
      double tp   = PositionGetDouble(POSITION_TP);
      double R    = (tp > 0 && InpRR > 0) ? MathAbs(tp - open) / InpRR : g_atr;
      if(R <= 0) continue;
      bool buy = (type == POSITION_TYPE_BUY);
      double profit = buy ? bid - open : open - ask;
      double newSl = 0.0;   // 0 = nothing to move
      if(InpUseBreakeven && profit >= InpBeTriggerR * R)
         newSl = buy ? open + InpBeLockPrice : open - InpBeLockPrice;
      if(InpUseTrailing && profit >= InpTrailStartR * R)
        {
         double trail = buy ? bid - InpTrailDistAtr * g_atr : ask + InpTrailDistAtr * g_atr;
         newSl = (newSl == 0) ? trail : (buy ? MathMax(newSl, trail) : MathMin(newSl, trail));
        }
      if(newSl == 0) continue;
      // never closer than the broker allows, and only ever towards profit
      newSl = buy ? MathMin(newSl, bid - minDist) : MathMax(newSl, ask + minDist);
      newSl = NormalizeDouble(newSl, _Digits);
      if(buy && newSl > sl + _Point)                  trade.PositionModify(ticket, newSl, tp);
      if(!buy && (sl == 0 || newSl < sl - _Point))    trade.PositionModify(ticket, newSl, tp);
     }
  }

bool IsMine()
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
//| Helpers                                                          |
//+------------------------------------------------------------------+
bool PostJson(const string path, const string body, const int timeoutMs, string &resp)
  {
   resp = "";
   if(MQLInfoInteger(MQL_TESTER)) { resp = "WebRequest is not available in the Strategy Tester"; return false; }
   char data[], result[];
   string rh;
   int len = StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   if(len > 0) ArrayResize(data, len - 1);   // drop the trailing \0
   string headers = "Content-Type: application/json\r\nX-AIT-Token: " + InpApiToken + "\r\n";
   ResetLastError();
   int code = WebRequest("POST", InpBridgeUrl + path, headers, timeoutMs, data, result, rh);
   if(code == -1)
     {
      int err = GetLastError();
      resp = (err == 4014) ? "URL not allowed: add " + InpBridgeUrl + " in Tools > Options > Expert Advisors"
                           : StringFormat("Dashboard not reachable (error %d) - is start.bat running?", err);
      return false;
     }
   resp = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   if(code != 200)
     {
      resp = StringFormat("HTTP %d %s", code, StringSubstr(resp, 0, 160));
      return false;
     }
   return true;
  }

//--- value of a top-level key in a flat JSON object ("" if missing); strings are unescaped
string JsonGet(const string js, const string key)
  {
   int p = StringFind(js, "\"" + key + "\":");
   if(p < 0) return "";
   p += StringLen(key) + 3;
   int len = StringLen(js);
   while(p < len && StringGetCharacter(js, p) == ' ') p++;
   if(p >= len) return "";
   if(StringGetCharacter(js, p) == '"')
     {
      string out = "";
      for(int i = p + 1; i < len; i++)
        {
         ushort ch = StringGetCharacter(js, i);
         if(ch == '\\' && i + 1 < len) { i++; ushort e = StringGetCharacter(js, i); out += (e == 'n') ? " " : ShortToString(e); continue; }
         if(ch == '"') break;
         out += ShortToString(ch);
        }
      return out;
     }
   int e = p;
   while(e < len)
     {
      ushort ch = StringGetCharacter(js, e);
      if(ch == ',' || ch == '}' || ch == ']') break;
      e++;
     }
   string v = StringSubstr(js, p, e - p);
   StringTrimLeft(v); StringTrimRight(v);
   return v;
  }

string JsonEscape(string s)
  {
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   return s;
  }

string D(const double v) { return DoubleToString(v, _Digits); }

string TrendName(const int t) { return t > 0 ? "up" : (t < 0 ? "down" : "range"); }

string EventJson(const int dir, const double level, const int ago)
  {
   if(dir == 0) return "null";
   return StringFormat("{\"dir\":\"%s\",\"level\":%s,\"bars_ago\":%d}", dir > 0 ? "up" : "down", D(level), ago);
  }

int BarsSince(const datetime t)
  {
   int s = iBarShift(_Symbol, PERIOD_M5, t, false);
   return s < 0 ? 0 : s;
  }

void ResetDailyCounter()
  {
   MqlDateTime d; TimeToStruct(TimeCurrent(), d);
   if(d.day_of_year != g_callsDay) { g_callsDay = d.day_of_year; g_callsToday = 0; g_costToday = 0.0; }
  }

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

void UpdatePanel()
  {
   if(MQLInfoInteger(MQL_TESTER) && !MQLInfoInteger(MQL_VISUAL_MODE)) return;
   string plan = g_plan.active
                 ? StringFormat("Plan: %s %s zone %s [%s-%s] entry %s inval %s, valid %d bars\n%s",
                                g_plan.dir > 0 ? "BUY" : "SELL", g_plan.setup, g_plan.zoneId, D(g_plan.zLo), D(g_plan.zHi),
                                D(g_plan.entry), D(g_plan.inval), g_plan.validBars, g_plan.note)
                 : "Plan: -";
   Comment(StringFormat("AI EA M5  |  %s\n%s  ATR(M5) %.2f  spread %.2f\nScore  BUY %d  /  SELL %d   (weight of evidence, not win %%)\n%s\nAI calls today: %d   AI cost today: %.4f USD\nM5 structure: %s   zones: %d\n%s",
                        g_status, _Symbol, g_atr, SpreadPrice(), g_lastBuy, g_lastSell, plan,
                        g_callsToday, g_costToday, TrendName(g_m5.trend), ArraySize(g_zones), g_reason));
  }
//+------------------------------------------------------------------+
