//+------------------------------------------------------------------+
//| QuantPilot v4 - read-only MT5 M1 collector, server-owned cursor    |
//| Protocol v2 requires the matching v4 Python server.               |
//| No orders. Raw broker timestamps are NOT claimed to be UTC.       |
//+------------------------------------------------------------------+
#property copyright "QuantPilot"
#property version   "4.00"
#property strict

input string InpApiBase = "https://forex.example.com"; // origin; no /v1 or /v2 suffix
input string InpApiKey = "";                         // 32-128 ASCII letters/digits/_/-
input string InpSourceId = "quantpilot-main";         // unique per installation/feed
input string InpSymbols = "XAUUSD,EURUSD,GBPUSD,GBPJPY,.US30Cash,.US500Cash,.USTECHCash,.DE40Cash";
input int InpTimerSeconds = 1;                       // one symbol per timer event
input int InpHistoryBars = 3000;                     // FIXED bootstrap, full window required
input datetime InpBootstrapFrom = 0;                // optional exact M1 open time in BROKER time
input int InpBatchBars = 200;                        // 1..500
input int InpHttpTimeoutMs = 5000;
input int InpStateRefreshSeconds = 60;
input int InpHeartbeatSeconds = 30;
input int InpMaxBackoffSeconds = 120;
input bool InpRequireAllSymbols = true;
input bool InpSendQuote = true;
input int InpQuoteMaxAgeSeconds = 120;
input bool InpSendIndicators = true;                 // ATR14 RSI14 EMA20/50/200 (M1)
input int InpReconcileBars = 3000;                    // 0 disables historical repair
input int InpReconcileIntervalSeconds = 60;          // one repair batch per symbol/interval
input bool InpVerbose = false;

struct SymbolState
{
   string symbol;
   int digits;
   double point;
   int handles[5];
   bool ready;
   string db_id;
   datetime start_t;
   datetime last_t;
   datetime repair_t;
   ulong sync_at;
   ulong heartbeat_at;
   ulong retry_at;
   ulong repair_at;
   ulong log_at;
   int failures;
};
SymbolState g_states[];
int g_next=0;
string g_base="";
string g_broker="";
long g_login=0; // local switch detection only; NEVER transmitted or used as source

bool SafeId(string value,int min_len,int max_len)
{
   int n=StringLen(value);
   if(n<min_len || n>max_len) return false;
   for(int i=0;i<n;i++)
   {
      ushort c=StringGetCharacter(value,i);
      if(!((c>=48 && c<=57)||(c>=65 && c<=90)||(c>=97 && c<=122)||c==95||c==45||c==46))
         return false;
   }
   return true;
}

string EscapeJson(string value)
{
   string out="";
   for(int i=0;i<StringLen(value);i++)
   {
      ushort c=StringGetCharacter(value,i);
      if(c==34) out+="\\\"";
      else if(c==92) out+="\\\\";
      else if(c<32) out+=StringFormat("\\u%04X",(uint)c);
      else out+=StringSubstr(value,i,1);
   }
   return out;
}

// This bounded flat-object parser reads only the server protocol's scalar fields.
// It tolerates JSON spaces; duplicates, nested objects and malformed scalars fail.
int JsonValueStart(string json,string key)
{
   string needle="\""+key+"\"";
   int p=StringFind(json,needle);
   if(p<0 || StringFind(json,needle,p+StringLen(needle))>=0) return -1;
   p+=StringLen(needle);
   while(p<StringLen(json) && StringGetCharacter(json,p)<=32) p++;
   if(p>=StringLen(json) || StringGetCharacter(json,p)!=58) return -1;
   p++;
   while(p<StringLen(json) && StringGetCharacter(json,p)<=32) p++;
   return p;
}

bool JsonDelimiter(string json,int pos)
{
   while(pos<StringLen(json) && StringGetCharacter(json,pos)<=32) pos++;
   if(pos>=StringLen(json)) return false;
   ushort c=StringGetCharacter(json,pos);
   return c==44 || c==125;
}

bool JsonLong(string json,string key,long &value)
{
   int p=JsonValueStart(json,key);
   if(p<0) return false;
   int first=p;
   while(p<StringLen(json))
   {
      ushort c=StringGetCharacter(json,p);
      if(c<48 || c>57) break;
      p++;
   }
   if(p==first || p-first>13 || !JsonDelimiter(json,p)) return false;
   value=StringToInteger(StringSubstr(json,first,p-first));
   return value>=0;
}

bool JsonTrue(string json,string key)
{
   int p=JsonValueStart(json,key);
   return p>=0 && StringSubstr(json,p,4)=="true" && JsonDelimiter(json,p+4);
}

bool JsonDbId(string json,string &value)
{
   int p=JsonValueStart(json,"db_id");
   if(p<0 || StringGetCharacter(json,p)!=34) return false;
   value=StringSubstr(json,p+1,32);
   if(StringLen(value)!=32 || StringGetCharacter(json,p+33)!=34 || !JsonDelimiter(json,p+34)) return false;
   for(int i=0;i<32;i++)
   {
      ushort c=StringGetCharacter(value,i);
      if(!((c>=48 && c<=57)||(c>=97 && c<=102))) return false;
   }
   return true;
}

void LogIssue(SymbolState &s,string message)
{
   ulong now=GetTickCount64();
   if(s.log_at==0 || now>=s.log_at)
   {
      Print("QuantPilot ",s.symbol,": ",message);
      s.log_at=now+60000;
   }
}

void Fail(SymbolState &s)
{
   s.failures++;
   int seconds=2;
   for(int i=1;i<s.failures && seconds<InpMaxBackoffSeconds;i++) seconds*=2;
   if(seconds>InpMaxBackoffSeconds) seconds=InpMaxBackoffSeconds;
   s.retry_at=GetTickCount64()+(ulong)seconds*1000;
}

void Success(SymbolState &s)
{
   s.failures=0;
   s.retry_at=0;
}

bool HttpPost(SymbolState &s,string path,string payload,string &response)
{
   char body[],reply[];
   int count=StringToCharArray(payload,body,0,WHOLE_ARRAY,CP_UTF8);
   if(count<2) { Fail(s); return false; }
   ArrayResize(body,count-1);
   string headers="Content-Type: application/json\r\nX-API-Key: "+InpApiKey+"\r\n";
   string response_headers;
   ResetLastError();
   int status=WebRequest("POST",g_base+path,headers,InpHttpTimeoutMs,body,reply,response_headers);
   int error=GetLastError();
   response=CharArrayToString(reply,0,WHOLE_ARRAY,CP_UTF8);
   if(status!=200 || StringLen(response)>2048 || !JsonTrue(response,"accepted"))
   {
      // Never print payload, headers, API key or an untrusted raw response.
      LogIssue(s,StringFormat("HTTP=%d MQLerror=%d path=%s; state will be refreshed",status,error,path));
      s.ready=false; // also recovers an ACK lost after a successful DB commit
      Fail(s);
      return false;
   }
   return true;
}

string Identity(SymbolState &s)
{
   return StringFormat("\"source\":\"%s\",\"symbol\":\"%s\",\"broker\":\"%s\",\"timeframe\":\"M1\",\"digits\":%d,\"point\":%s",
      EscapeJson(InpSourceId),EscapeJson(s.symbol),EscapeJson(g_broker),s.digits,DoubleToString(s.point,16));
}

bool RefreshState(SymbolState &s,datetime initial_start)
{
   string response;
   string payload="{"+Identity(s)+StringFormat(",\"initial_start_t\":%I64d}",(long)initial_start);
   if(!HttpPost(s,"/v2/mt5/state",payload,response)) return false;
   string id;
   long start=0,last=0;
   if(!JsonDbId(response,id) || !JsonLong(response,"start_t",start) || !JsonLong(response,"last_t",last)
      || (start==0 && last!=0) || (last!=0 && last<start)
      || (start!=0 && (start<1000000000 || start%60!=0))
      || (last!=0 && (last<1000000000 || last%60!=0))
      || (start>0 && !JsonTrue(response,"initialized")))
   {
      s.ready=false; LogIssue(s,"invalid state response"); Fail(s); return false;
   }
   if(s.db_id!=id || s.start_t!=(datetime)start || s.last_t!=(datetime)last) s.repair_t=0;
   s.db_id=id;
   s.start_t=(datetime)start;
   s.last_t=(datetime)last;
   s.ready=true;
   s.sync_at=GetTickCount64()+(ulong)InpStateRefreshSeconds*1000;
   Success(s);
   return true;
}

string QuoteJson(SymbolState &s)
{
   if(!InpSendQuote || !TerminalInfoInteger(TERMINAL_CONNECTED)) return "null";
   MqlTick tick;
   if(!SymbolInfoTick(s.symbol,tick) || !MathIsValidNumber(tick.bid) || !MathIsValidNumber(tick.ask)
      || tick.bid<=0 || tick.ask<tick.bid || tick.time_msc<1000000000000
      || tick.time_msc/1000!=(long)tick.time) return "null";
   datetime now=TimeTradeServer();
   long age=(long)now-(long)tick.time;
   if(now<=0 || age< -5 || age>InpQuoteMaxAgeSeconds) return "null";
   string bid=DoubleToString(tick.bid,s.digits),ask=DoubleToString(tick.ask,s.digits);
   if(StringToDouble(bid)<=0 || StringToDouble(ask)<StringToDouble(bid)) return "null";
   return StringFormat("{\"time_msc\":%I64d,\"bid\":%s,\"ask\":%s}",tick.time_msc,
                       bid,ask);
}

bool Heartbeat(SymbolState &s,string status,datetime latest)
{
   ulong now=GetTickCount64();
   if(now<s.heartbeat_at) return true;
   string payload="{"+Identity(s)+StringFormat(",\"db_id\":\"%s\",\"latest_closed_t\":%I64d,\"status\":\"%s\",\"quote\":%s}",
                          s.db_id,(long)latest,status,QuoteJson(s));
   string response,id;
   long last=0;
   if(!HttpPost(s,"/v2/mt5/heartbeat",payload,response)) return false;
   if(!JsonDbId(response,id) || id!=s.db_id || !JsonLong(response,"last_t",last))
   { s.ready=false; Fail(s); return false; }
   if((datetime)last!=s.last_t) s.ready=false; // another instance or restoration changed state
   s.heartbeat_at=GetTickCount64()+(ulong)InpHeartbeatSeconds*1000;
   Success(s);
   return true;
}

bool ValidBar(MqlRates &b)
{
   return b.time>=1000000000 && (long)b.time%60==0 && MathIsValidNumber(b.open)
      && MathIsValidNumber(b.high) && MathIsValidNumber(b.low) && MathIsValidNumber(b.close)
      && b.open>0 && b.high>0 && b.low>0 && b.close>0
      && b.low<=MathMin(b.open,b.close) && b.high>=MathMax(b.open,b.close) && b.high>=b.low
      && b.tick_volume>=0 && b.real_volume>=0 && b.spread>=0;
}

string IndicatorNumber(double value,int kind)
{
   // Null represents warm-up/unavailable values, never zero substituted for missing data.
   if(!MathIsValidNumber(value) || value==EMPTY_VALUE) return "null";
   if(kind==0 && value<0) return "null";
   if(kind==1 && (value<0 || value>100)) return "null";
   if(kind>=2 && value<=0) return "null";
   return DoubleToString(value,12);
}

bool LoadIndicator(int handle,int start_pos,int count,double &values[])
{
   if(handle==INVALID_HANDLE || BarsCalculated(handle)<start_pos+count) return false;
   ArraySetAsSeries(values,false);
   return CopyBuffer(handle,0,start_pos,count,values)==count;
}

bool MakeBarsJson(SymbolState &s,MqlRates &bars[],int offset,int count,string &json)
{
   json="";
   if(count<1) return false;
   // Resolve by timestamp; positional snapshots can shift when a new minute opens.
   int oldest=iBarShift(s.symbol,PERIOD_M1,bars[offset].time,true);
   int newest=iBarShift(s.symbol,PERIOD_M1,bars[offset+count-1].time,true);
   if(newest<1 || oldest-newest+1!=count) return false;
   datetime current=iTime(s.symbol,PERIOD_M1,0);
   if(current==0) return false;
   double atr[],rsi[],ema20[],ema50[],ema200[];
   if(InpSendIndicators)
   {
      if(!LoadIndicator(s.handles[0],newest,count,atr) || !LoadIndicator(s.handles[1],newest,count,rsi)
         || !LoadIndicator(s.handles[2],newest,count,ema20) || !LoadIndicator(s.handles[3],newest,count,ema50)
         || !LoadIndicator(s.handles[4],newest,count,ema200))
      {
         LogIssue(s,"indicator buffers are not ready; candles are retained for retry");
         Heartbeat(s,"indicator_wait",iTime(s.symbol,PERIOD_M1,1));
         return false;
      }
   }
   // No sending if positional indicator reads raced a new candle or history rebuild.
   if(iTime(s.symbol,PERIOD_M1,0)!=current || iTime(s.symbol,PERIOD_M1,newest)!=bars[offset+count-1].time
      || iTime(s.symbol,PERIOD_M1,oldest)!=bars[offset].time) return false;
   MqlRates verify[];
   ArraySetAsSeries(verify,false);
   if(CopyRates(s.symbol,PERIOD_M1,newest,count,verify)!=count) return false;
   for(int i=0;i<count;i++)
   {
      int at=offset+i;
      if(verify[i].time!=bars[at].time || verify[i].open!=bars[at].open || verify[i].high!=bars[at].high
         || verify[i].low!=bars[at].low || verify[i].close!=bars[at].close
         || verify[i].tick_volume!=bars[at].tick_volume || verify[i].real_volume!=bars[at].real_volume
         || verify[i].spread!=bars[at].spread) return false;
      if(!ValidBar(bars[at]))
      {
         LogIssue(s,"invalid broker candle; cursor NOT advanced");
         Heartbeat(s,"invalid_bar",iTime(s.symbol,PERIOD_M1,1));
         return false;
      }
      if(i>0 && bars[at].time<=bars[at-1].time) return false;
      if(i>0) json+=",";
      json+=StringFormat("{\"t\":%I64d,\"o\":%s,\"h\":%s,\"l\":%s,\"c\":%s,\"volume\":%I64d,\"real_volume\":%I64d,\"spread\":%d",
         (long)bars[at].time,DoubleToString(bars[at].open,s.digits),DoubleToString(bars[at].high,s.digits),
         DoubleToString(bars[at].low,s.digits),DoubleToString(bars[at].close,s.digits),
         bars[at].tick_volume,bars[at].real_volume,bars[at].spread);
      if(InpSendIndicators)
         json+=",\"atr14\":"+IndicatorNumber(atr[i],0)+",\"rsi14\":"+IndicatorNumber(rsi[i],1)
            +",\"ema20\":"+IndicatorNumber(ema20[i],2)+",\"ema50\":"+IndicatorNumber(ema50[i],3)
            +",\"ema200\":"+IndicatorNumber(ema200[i],4);
      json+="}";
   }
   return iTime(s.symbol,PERIOD_M1,0)==current && SeriesInfoInteger(s.symbol,PERIOD_M1,SERIES_SYNCHRONIZED)!=0;
}

bool SendBars(SymbolState &s,MqlRates &bars[],int offset,int count,bool repair)
{
   string json;
   if(!MakeBarsJson(s,bars,offset,count,json)) return false;
   datetime latest=iTime(s.symbol,PERIOD_M1,1);
   if(latest<bars[offset+count-1].time) return false;
   string payload="{"+Identity(s)+StringFormat(",\"db_id\":\"%s\",\"expected_last_t\":%I64d,\"latest_closed_t\":%I64d,\"bars\":[%s],\"quote\":%s}",
                         s.db_id,(long)s.last_t,(long)latest,json,repair?"null":QuoteJson(s));
   string response,id;
   long last=0,accepted=0;
   string path=repair?"/v2/mt5/reconcile":"/v2/mt5/bars";
   if(!HttpPost(s,path,payload,response)) return false;
   datetime expected=repair?s.last_t:bars[offset+count-1].time;
   if(!JsonDbId(response,id) || id!=s.db_id || !JsonLong(response,"last_t",last)
      || !JsonLong(response,"accepted_count",accepted) || (datetime)last!=expected || accepted!=count)
   {
      s.ready=false; LogIssue(s,"ACK does not match database/cursor/batch"); Fail(s); return false;
   }
   if(repair) s.repair_t=bars[offset+count-1].time;
   else s.last_t=(datetime)last;
   if(!repair) s.heartbeat_at=GetTickCount64()+(ulong)InpHeartbeatSeconds*1000;
   Success(s);
   if(InpVerbose) PrintFormat("QuantPilot %s %s ACK %d through %s",s.symbol,repair?"repair":"ingest",count,TimeToString((datetime)last));
   return true;
}

bool Bootstrap(SymbolState &s)
{
   if(InpBootstrapFrom>0)
   {
      MqlRates probe[];
      CopyRates(s.symbol,PERIOD_M1,InpBootstrapFrom,(datetime)(InpBootstrapFrom+60),probe);
      if(SeriesInfoInteger(s.symbol,PERIOD_M1,SERIES_SYNCHRONIZED)==0
         || iBarShift(s.symbol,PERIOD_M1,InpBootstrapFrom,true)<1)
      {
         LogIssue(s,"requested bootstrap date unavailable; use an actual M1 candle open time");
         Heartbeat(s,"waiting_history",iTime(s.symbol,PERIOD_M1,1));
         return false;
      }
      return RefreshState(s,InpBootstrapFrom);
   }
   MqlRates initial[];
   ArraySetAsSeries(initial,false);
   ResetLastError();
   int got=CopyRates(s.symbol,PERIOD_M1,1,InpHistoryBars,initial);
   if(got!=InpHistoryBars || SeriesInfoInteger(s.symbol,PERIOD_M1,SERIES_SYNCHRONIZED)==0)
   {
      LogIssue(s,StringFormat("waiting for FULL bootstrap history: %d/%d; check Max bars in chart",got,InpHistoryBars));
      Heartbeat(s,"waiting_history",iTime(s.symbol,PERIOD_M1,1));
      return false;
   }
   if(initial[0].time<=0 || initial[got-1].time!=iTime(s.symbol,PERIOD_M1,1)) return false;
   // Freeze the requested starting candle before any upload. Never ACK a partial bootstrap.
   return RefreshState(s,initial[0].time);
}

void RepairHistory(SymbolState &s)
{
   if(InpReconcileBars==0 || GetTickCount64()<s.repair_at) return;
   MqlRates bars[];
   ArraySetAsSeries(bars,false);
   int got=CopyRates(s.symbol,PERIOD_M1,1,InpReconcileBars,bars);
   if(got!=InpReconcileBars || SeriesInfoInteger(s.symbol,PERIOD_M1,SERIES_SYNCHRONIZED)==0)
   {
      LogIssue(s,"repair history not ready; no cursor advancement");
      s.repair_at=GetTickCount64()+(ulong)InpReconcileIntervalSeconds*1000; // avoid re-reading a large range every timer cycle
      return;
   }
   int first=-1;
   for(int i=0;i<got;i++)
      if(bars[i].time>=s.start_t && bars[i].time>s.repair_t && bars[i].time<=s.last_t) { first=i; break; }
   if(first<0)
   {
      s.repair_t=0;
      s.repair_at=GetTickCount64()+(ulong)InpReconcileIntervalSeconds*1000;
      return;
   }
   int count=0;
   for(int i=first;i<got && count<InpBatchBars && bars[i].time<=s.last_t;i++) count++;
   if(SendBars(s,bars,first,count,true))
      s.repair_at=GetTickCount64()+(ulong)InpReconcileIntervalSeconds*1000;
}

void ProcessSymbol(SymbolState &s)
{
   ulong now=GetTickCount64();
   if(now<s.retry_at) return;
   if(!s.ready || now>=s.sync_at)
   { RefreshState(s,0); return; }
   if(!TerminalInfoInteger(TERMINAL_CONNECTED))
   { LogIssue(s,"MT5 disconnected from broker"); Heartbeat(s,"disconnected",0); return; }
   if(s.start_t==0) { Bootstrap(s); return; }
   datetime anchor=s.last_t>0?s.last_t:s.start_t;
   // Date-based probe triggers download of the saved anchor after a long outage.
   MqlRates probe[];
   CopyRates(s.symbol,PERIOD_M1,anchor,(datetime)(anchor+60),probe);
   if(SeriesInfoInteger(s.symbol,PERIOD_M1,SERIES_SYNCHRONIZED)==0)
   { Heartbeat(s,"waiting_history",0); return; }
   int shift=iBarShift(s.symbol,PERIOD_M1,anchor,true);
   datetime latest=iTime(s.symbol,PERIOD_M1,1);
   if(latest<=0) { Heartbeat(s,"waiting_history",0); return; }
   if(shift<1)
   {
      LogIssue(s,"saved anchor unavailable in broker history; HALTED, no automatic skip");
      Heartbeat(s,"history_anchor_missing",latest);
      return;
   }
   int pending=s.last_t>0?shift-1:shift;
   if(pending==0)
   {
      if(!Heartbeat(s,"ok",latest)) return;
      RepairHistory(s);
      return;
   }
   int selected=pending<InpBatchBars?pending:InpBatchBars;
   // Include the acknowledged anchor as a guard; this also catches shifting positions.
   int requested=selected+(s.last_t>0?1:0);
   int start_pos=s.last_t>0?shift-selected:shift-selected+1;
   MqlRates bars[];
   ArraySetAsSeries(bars,false);
   int got=CopyRates(s.symbol,PERIOD_M1,start_pos,requested,bars);
   if(got!=requested || SeriesInfoInteger(s.symbol,PERIOD_M1,SERIES_SYNCHRONIZED)==0
      || bars[0].time!=anchor)
   { LogIssue(s,"partial/shifted history read; retained for retry"); Heartbeat(s,"waiting_history",latest); return; }
   int offset=s.last_t>0?1:0;
   SendBars(s,bars,offset,selected,false);
}

void ReleaseHandles()
{
   for(int i=0;i<ArraySize(g_states);i++)
      for(int j=0;j<5;j++)
         if(g_states[i].handles[j]!=INVALID_HANDLE)
         { IndicatorRelease(g_states[i].handles[j]); g_states[i].handles[j]=INVALID_HANDLE; }
}

int OnInit()
{
   g_base=InpApiBase;
   while(StringLen(g_base)>0 && StringSubstr(g_base,StringLen(g_base)-1,1)=="/")
      g_base=StringSubstr(g_base,0,StringLen(g_base)-1);
   if(StringFind(g_base,"https://")!=0 || StringLen(g_base)<9 || StringFind(g_base,"example.com")>=0
      || StringFind(g_base,"/",8)>=0 || StringFind(g_base,"?")>=0 || StringFind(g_base,"#")>=0
      || StringFind(g_base,"@")>=0 || StringFind(g_base," ")>=0
      || !SafeId(InpSourceId,1,80) || !SafeId(InpApiKey,32,128) || StringFind(InpApiKey,".")>=0)
   { Print("QuantPilot: real HTTPS origin, valid API key and unique source ID required"); return INIT_PARAMETERS_INCORRECT; }
   if(InpTimerSeconds<1 || InpTimerSeconds>60 || InpBatchBars<1 || InpBatchBars>500
      || InpHistoryBars<InpBatchBars || InpHistoryBars>100000 || InpHttpTimeoutMs<1000 || InpHttpTimeoutMs>60000
      || InpStateRefreshSeconds<30 || InpHeartbeatSeconds<10 || InpMaxBackoffSeconds<2 || InpMaxBackoffSeconds>600
      || InpQuoteMaxAgeSeconds<1 || InpReconcileBars<0 || InpReconcileBars>100000 || InpReconcileIntervalSeconds<10)
      return INIT_PARAMETERS_INCORRECT;
   if(InpBootstrapFrom>0 && ((long)InpBootstrapFrom<1000000000 || (long)InpBootstrapFrom%60!=0))
      return INIT_PARAMETERS_INCORRECT;
   if(MQLInfoInteger(MQL_TESTER))
   { Print("QuantPilot requires a live MT5 terminal; WebRequest is unavailable in Strategy Tester"); return INIT_FAILED; }
   g_broker=AccountInfoString(ACCOUNT_SERVER);
   g_login=AccountInfoInteger(ACCOUNT_LOGIN);
   if(StringLen(g_broker)==0 || g_login<=0)
   { Print("QuantPilot: log in to a broker account first"); return INIT_FAILED; }
   string raw[];
   int n=StringSplit(InpSymbols,StringGetCharacter(",",0),raw);
   bool missing=false;
   for(int i=0;i<n;i++)
   {
      string sym=raw[i]; StringTrimLeft(sym); StringTrimRight(sym);
      if(StringLen(sym)==0) continue;
      bool duplicate=false;
      for(int j=0;j<ArraySize(g_states);j++) if(g_states[j].symbol==sym) duplicate=true;
      if(duplicate) continue;
      if(!SymbolSelect(sym,true))
      { Print("QuantPilot unavailable symbol: ",sym); missing=true; continue; }
      int k=ArraySize(g_states);
      ArrayResize(g_states,k+1);
      // ArrayResize does not guarantee zeroed structure members: initialize every field explicitly.
      g_states[k].symbol=sym;
      g_states[k].ready=false;
      g_states[k].db_id="";
      g_states[k].start_t=0;
      g_states[k].last_t=0;
      g_states[k].repair_t=0;
      g_states[k].sync_at=0;
      g_states[k].heartbeat_at=0;
      g_states[k].retry_at=0;
      g_states[k].repair_at=0;
      g_states[k].log_at=0;
      g_states[k].failures=0;
      g_states[k].digits=(int)SymbolInfoInteger(sym,SYMBOL_DIGITS);
      g_states[k].point=SymbolInfoDouble(sym,SYMBOL_POINT);
      for(int j=0;j<5;j++) g_states[k].handles[j]=INVALID_HANDLE;
      if(g_states[k].digits<0 || g_states[k].digits>12 || !MathIsValidNumber(g_states[k].point) || g_states[k].point<=0)
      { Print("QuantPilot invalid instrument metadata: ",sym); ReleaseHandles(); return INIT_FAILED; }
      if(InpSendIndicators)
      {
         g_states[k].handles[0]=iATR(sym,PERIOD_M1,14);
         g_states[k].handles[1]=iRSI(sym,PERIOD_M1,14,PRICE_CLOSE);
         g_states[k].handles[2]=iMA(sym,PERIOD_M1,20,0,MODE_EMA,PRICE_CLOSE);
         g_states[k].handles[3]=iMA(sym,PERIOD_M1,50,0,MODE_EMA,PRICE_CLOSE);
         g_states[k].handles[4]=iMA(sym,PERIOD_M1,200,0,MODE_EMA,PRICE_CLOSE);
         for(int j=0;j<5;j++) if(g_states[k].handles[j]==INVALID_HANDLE)
         { Print("QuantPilot indicator initialization failed: ",sym); ReleaseHandles(); return INIT_FAILED; }
      }
   }
   if(ArraySize(g_states)==0 || (missing && InpRequireAllSymbols))
   { Print("QuantPilot: correct symbol names before starting"); ReleaseHandles(); return INIT_FAILED; }
   if(!EventSetTimer(InpTimerSeconds)) { ReleaseHandles(); return INIT_FAILED; }
   PrintFormat("QuantPilot v4 active: %d symbols, timer=%ds, source=%s; server controls progress",
                ArraySize(g_states),InpTimerSeconds,InpSourceId);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   ReleaseHandles();
}

void OnTimer()
{
   int count=ArraySize(g_states);
   if(count==0) return;
   if(AccountInfoString(ACCOUNT_SERVER)!=g_broker || AccountInfoInteger(ACCOUNT_LOGIN)!=g_login)
   {
      Print("QuantPilot: account changed; collector stopped. Reattach with the correct source ID.");
      EventKillTimer();
      return;
   }
   int selected=g_next;
   g_next=(g_next+1)%count;
   ProcessSymbol(g_states[selected]);
}
