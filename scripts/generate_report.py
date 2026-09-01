from datetime import date, datetime, time as dt_time, timedelta
from email.utils import parsedate_to_datetime
from html import escape
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo
import json, math, os, re, time, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

import feedparser, pandas as pd, yfinance as yf
from bs4 import BeautifulSoup

KST=ZoneInfo("Asia/Seoul"); NY_TZ=ZoneInfo("America/New_York"); TEMPLATE=Path("report-template.html"); OUT=Path("site/index.html")
NEWS_CACHE=Path("work/news-cache.json")
NEWS_TIMEOUT=int(os.getenv("NEWS_TIMEOUT_SECONDS","15")); NEWS_ATTEMPTS=int(os.getenv("NEWS_ATTEMPTS","3"))
NEWS_QUERIES=[("macro","매크로","US economy inflation GDP dollar markets"),("geo","지정학","geopolitics oil market"),("fed","연준","Federal Reserve rates markets"),("market","마켓","Wall Street S&P Nasdaq Dow close")]
OFFICIAL_FEEDS=[("fed","연준","https://www.federalreserve.gov/feeds/press_all.xml")]
MAG7={"AAPL":"Apple","MSFT":"Microsoft","GOOGL":"Alphabet","AMZN":"Amazon","NVDA":"NVIDIA","META":"Meta","TSLA":"Tesla"}
FOMC_DATES={2026:[(1,28),(3,18),(4,29),(6,17),(7,29),(9,16),(10,28),(12,9)],2027:[(1,27),(3,17),(4,28),(6,9),(7,28),(9,15),(10,27),(12,8)]}
BLS_FALLBACK_2026=[(9,1,10,0,"구인·이직보고서(JOLTS)"),(9,3,8,30,"생산성·단위노동비용"),(9,4,8,30,"고용보고서(비농업 고용·실업률)"),(9,10,8,30,"생산자물가지수(PPI)"),(9,11,8,30,"소비자물가지수(CPI)"),(9,29,10,0,"구인·이직보고서(JOLTS)"),(10,2,8,30,"고용보고서(비농업 고용·실업률)"),(10,14,8,30,"소비자물가지수(CPI)"),(10,15,8,30,"생산자물가지수(PPI)"),(10,30,8,30,"고용비용지수(ECI)")]
ADP_FALLBACK_2026=[(9,2),(9,30),(11,4),(12,2)]

def close(ticker,period="6mo"):
    f=yf.download(ticker,period=period,progress=False,auto_adjust=False)
    s=f["Close"]
    if isinstance(s,pd.DataFrame): s=s.iloc[:,0]
    s=s.dropna().astype(float)
    if len(s)<2: raise RuntimeError(f"가격 데이터 부족: {ticker}")
    return s

def quote(ticker,period="6mo"):
    s=close(ticker,period); return float(s.iloc[-1]),(float(s.iloc[-1])/float(s.iloc[-2])-1)*100,s

def cls(v): return "up" if v>0 else "down" if v<0 else "flat"

def set_card(soup,label,value,change,fmt="{:,.2f}"):
    for node in [x for x in soup.select(".label,.macro-card span") if x.get_text(strip=True)==label]:
        card=node.find_parent("article"); val=card.select_one(".value") or card.find("strong"); delta=card.select_one(".up,.down,.flat")
        val.string=fmt.format(value); delta["class"]=[cls(change)]; delta.string=("▲" if change>0 else "▼" if change<0 else "–")+f" {abs(change):.2f}%"

def treasury_yields(year):
    q=urllib.parse.urlencode({"data":"daily_treasury_yield_curve","field_tdr_date_value":year})
    req=urllib.request.Request(f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?{q}",headers={"User-Agent":"us-market-close-report/2.0"})
    with urllib.request.urlopen(req,timeout=30) as r: root=ET.fromstring(r.read())
    ns={"a":"http://www.w3.org/2005/Atom","m":"http://schemas.microsoft.com/ado/2007/08/dataservices/metadata","d":"http://schemas.microsoft.com/ado/2007/08/dataservices"}; rows=[]
    fields={"US 2Y":"BC_2YEAR","US 10Y":"BC_10YEAR","US 30Y":"BC_30YEAR"}
    for entry in root.findall("a:entry",ns):
        p=entry.find("a:content/m:properties",ns)
        if p is None: continue
        dn=p.find("d:NEW_DATE",ns); rates={}
        for label,field in fields.items():
            n=p.find(f"d:{field}",ns); rates[label]=float(n.text) if n is not None and n.text else None
        if dn is not None and dn.text and all(v is not None for v in rates.values()): rows.append((pd.Timestamp(dn.text).date(),rates))
    if len(rows)<2: raise RuntimeError("미 재무부 수익률 데이터 부족")
    rows.sort(key=lambda x:x[0]); prev,cur=rows[-2],rows[-1]
    return cur[0],{k:(v,(v-prev[1][k])*100) for k,v in cur[1].items()}

def special(soup,label,value_text,change,unit,decimals=1):
    for node in [x for x in soup.select(".macro-card span") if x.get_text(strip=True)==label]:
        card=node.find_parent("article"); card.find("strong").string=value_text; d=card.select_one(".up,.down,.flat")
        d["class"]=[cls(change)]; d.string=("▲" if change>0 else "▼" if change<0 else "–")+f" {abs(change):.{decimals}f}{unit}"

def treasury_tga():
    url="https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/dts/operating_cash_balance?sort=-record_date&page%5Bsize%5D=12"
    req=urllib.request.Request(url,headers={"User-Agent":"us-market-close-report/2.0"})
    with urllib.request.urlopen(req,timeout=30) as r: rows=json.load(r)["data"]
    rows=[x for x in rows if x["account_type"]=="Treasury General Account (TGA) Closing Balance"]
    if len(rows)<2: raise RuntimeError("Fiscal Data TGA 데이터 부족")
    a,b=rows[0],rows[1]; av=float(a["open_today_bal"])/1000; bv=float(b["open_today_bal"])/1000
    return pd.Timestamp(a["record_date"]).date(),av,av-bv

def replace_stock(soup,ticker,change):
    m=soup.find("small",string=re.compile(rf"\b{re.escape(ticker)}\b"))
    if m:
        b=m.find_parent("li").find("b"); b["class"]=[cls(change)]; b.string=f"{change:+.2f}%"

def sp500_breadth():
    req=urllib.request.Request("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",headers={"User-Agent":"us-market-close-report/2.0"})
    with urllib.request.urlopen(req,timeout=30) as r: tickers=pd.read_html(StringIO(r.read().decode("utf-8")))[0]["Symbol"].astype(str).str.replace(".","-",regex=False).tolist()
    f=yf.download(tickers,period="1y",progress=False,auto_adjust=False,threads=True); c=f["Close"].ffill(); last,prev=c.iloc[-1],c.iloc[-2]; valid=last.notna()&prev.notna(); daily=last[valid]/prev[valid]-1
    ma20=c.rolling(20).mean().iloc[-1]; ma50=c.rolling(50).mean().iloc[-1]; ma200=c.rolling(200).mean().iloc[-1]; hi=c.rolling(252,min_periods=200).max().iloc[-1]
    return {"adv":int((daily>0).sum()),"dec":int((daily<0).sum()),"total":int(valid.sum()),"above20":float((last[valid]>ma20[valid]).mean()*100),"above50":float((last[valid]>ma50[valid]).mean()*100),"above200":float((last[valid]>ma200[valid]).mean()*100),"highs":int((last[valid]>=hi[valid]*.999999).sum())}

def rsi(s,n=14):
    d=s.diff(); g=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean(); l=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean()
    return float((100-100/(1+g/l.replace(0,math.nan))).iloc[-1])

def macd(s):
    line=s.ewm(span=12,adjust=False).mean()-s.ewm(span=26,adjust=False).mean(); signal=line.ewm(span=9,adjust=False).mean(); hist=line-signal
    return float(line.iloc[-1]),float(signal.iloc[-1]),float(hist.iloc[-1]),float(hist.iloc[-2])

def fetch_text(url,timeout=20):
    req=urllib.request.Request(url,headers={"User-Agent":"us-market-close-report/2.1 (+https://github.com/ewmhb/us-market-close-report)"})
    with urllib.request.urlopen(req,timeout=timeout) as response: return response.read().decode("utf-8","replace")

def bls_events(start,end):
    events=[]
    try:
        raw=fetch_text("https://www.bls.gov/schedule/news_release/bls.ics"); lines=[]
        for line in raw.replace("\r\n","\n").split("\n"):
            if line.startswith((" ","\t")) and lines: lines[-1]+=line[1:]
            else: lines.append(line)
        current=None
        for line in lines:
            if line=="BEGIN:VEVENT": current={}
            elif line=="END:VEVENT" and current:
                summary=current.get("SUMMARY",""); raw_date=current.get("DTSTART","")
                try:
                    if raw_date.endswith("Z"): when=datetime.strptime(raw_date,"%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC")).astimezone(NY_TZ)
                    elif "T" in raw_date: when=datetime.strptime(raw_date[:15],"%Y%m%dT%H%M%S").replace(tzinfo=NY_TZ)
                    else: when=datetime.combine(datetime.strptime(raw_date[:8],"%Y%m%d").date(),dt_time(8,30),NY_TZ)
                    if start<=when.date()<=end:
                        labels={"Employment Situation":"고용보고서(비농업 고용·실업률)","Consumer Price Index":"소비자물가지수(CPI)","Producer Price Index":"생산자물가지수(PPI)","Job Openings and Labor Turnover Survey":"구인·이직보고서(JOLTS)","Productivity and Costs":"생산성·단위노동비용","Employment Cost Index":"고용비용지수(ECI)"}
                        label=next((ko for en,ko in labels.items() if en in summary),None)
                        if label: events.append({"when":when,"title":label,"source":"BLS","impact":"금리·달러·주식 변동성"})
                except Exception: pass
                current=None
            elif current is not None and ":" in line:
                key,value=line.split(":",1); current[key.split(";",1)[0]]=value.replace("\\,",",")
    except Exception as exc: print(f"CALENDAR_WARNING source='BLS' error={type(exc).__name__}: {exc}")
    if not events and start.year==2026:
        for month,day,hour,minute,title in BLS_FALLBACK_2026:
            d=date(2026,month,day)
            if start<=d<=end: events.append({"when":datetime.combine(d,dt_time(hour,minute),NY_TZ),"title":title,"source":"BLS 공식 일정(내장 백업)","impact":"금리·달러·주식 변동성"})
    return events

def adp_events(start,end):
    events=[]
    try:
        text=BeautifulSoup(fetch_text("https://adpemploymentreport.com/"),"html.parser").get_text(" ",strip=True)
        block=text.split("Upcoming Reports:",1)[1].split("Upcoming reports (weekly",1)[0]
        for month,day,year in re.findall(r"([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})",block):
            d=datetime.strptime(f"{month} {day} {year}","%B %d %Y").date()
            if start<=d<=end: events.append({"when":datetime.combine(d,dt_time(8,15),NY_TZ),"title":"ADP 민간고용","source":"ADP","impact":"고용보고서 사전 심리"})
    except Exception as exc: print(f"CALENDAR_WARNING source='ADP' error={type(exc).__name__}: {exc}")
    if not events and start.year==2026:
        for month,day in ADP_FALLBACK_2026:
            d=date(2026,month,day)
            if start<=d<=end: events.append({"when":datetime.combine(d,dt_time(8,15),NY_TZ),"title":"ADP 민간고용","source":"ADP 공식 일정(내장 백업)","impact":"고용보고서 사전 심리"})
    return events

def fomc_events(start,end):
    events=[]
    for year in range(start.year,end.year+1):
        for month,day in FOMC_DATES.get(year,[]):
            d=date(year,month,day)
            if start<=d<=end: events.append({"when":datetime.combine(d,dt_time(14),NY_TZ),"title":"FOMC 금리결정·파월 기자회견","source":"Federal Reserve","impact":"금리·달러·성장주"})
    return events

def earnings_date(ticker):
    try:
        calendar=yf.Ticker(ticker).calendar
        value=calendar.get("Earnings Date") if isinstance(calendar,dict) else None
        if value is None and hasattr(calendar,"loc") and "Earnings Date" in calendar.index: value=calendar.loc["Earnings Date"].iloc[0]
        if isinstance(value,(list,tuple)): value=value[0] if value else None
        if hasattr(value,"to_pydatetime"): value=value.to_pydatetime()
        if isinstance(value,datetime): return value.date()
        if isinstance(value,date): return value
    except Exception as exc: print(f"CALENDAR_WARNING source={ticker!r} error={type(exc).__name__}: {exc}")
    return None

def render_weekly_calendar(soup,now):
    today=now.astimezone(NY_TZ).date(); start=today-timedelta(days=today.weekday()); end=start+timedelta(days=6)
    events=bls_events(start,end)+adp_events(start,end)+fomc_events(start,end)
    earnings=[]
    for ticker,name in MAG7.items():
        d=earnings_date(ticker)
        if d: earnings.append((d,ticker,name))
        if d and start<=d<=end: events.append({"when":datetime.combine(d,dt_time(16),NY_TZ),"title":f"{name}({ticker}) 실적발표 예정","source":"기업 일정","impact":"빅테크·지수 영향"})
    events.sort(key=lambda x:x["when"]); days="월화수목금토일"
    rows="".join(f'<li class="row"><span><b>{e["when"].month}/{e["when"].day}({days[e["when"].weekday()]}) {e["when"].strftime("%H:%M")} ET</b><br><small class="note">{escape(e["source"])} · {escape(e["impact"])}</small></span><strong>{escape(e["title"])}</strong></li>' for e in events)
    if not rows: rows='<li class="note">이번 주에 확인된 핵심 미국 경제지표·FOMC·매그니피센트 7 실적 일정이 없습니다.</li>'
    n=soup.select_one("#weekly-events"); n.clear(); n.append(BeautifulSoup(rows,"html.parser")); soup.select_one("#weekly-range").string=f'{start.strftime("%Y.%m.%d")}–{end.strftime("%m.%d")} · 미국 동부시간(ET) 기준'
    upcoming=sorted(x for x in earnings if x[0]>=today)
    erows="".join(f'<li class="row"><span>{name} <small class="note">{ticker}</small></span><b>{d.month}/{d.day} 예정</b></li>' for d,ticker,name in upcoming)
    if not erows: erows='<li class="note">현재 데이터 공급자가 확인한 향후 실적 예정일이 없습니다.</li>'
    n=soup.select_one("#earnings-calendar"); n.clear(); n.append(BeautifulSoup(erows,"html.parser"))

def fetch_rss(key,label,url,name,limit=2):
    error="unknown error"
    for attempt in range(1,NEWS_ATTEMPTS+1):
        try:
            req=urllib.request.Request(url,headers={"User-Agent":"us-market-close-report/2.1 (+https://github.com/ewmhb/us-market-close-report)","Accept":"application/rss+xml, application/xml;q=0.9, */*;q=0.1"})
            with urllib.request.urlopen(req,timeout=NEWS_TIMEOUT) as response:
                status=getattr(response,"status",200); body=response.read()
            if status!=200: raise RuntimeError(f"HTTP {status}")
            parsed=feedparser.parse(body)
            if parsed.bozo and not parsed.entries: raise RuntimeError(f"RSS parse error: {parsed.bozo_exception}")
            items=[]
            for entry in parsed.entries[:limit]:
                published=getattr(entry,"published","")
                try: published_iso=parsedate_to_datetime(published).astimezone(KST).isoformat()
                except Exception: published_iso=datetime.now(KST).isoformat()
                source=getattr(getattr(entry,"source",None),"title",name)
                title=re.sub(r"\s+-\s+[^-]+$","",getattr(entry,"title","")).strip()
                link=getattr(entry,"link",url)
                if title: items.append({"category":key,"label":label,"source":source,"published":published_iso,"title":title,"link":link})
            print(f"NEWS source={name!r} status={status} entries={len(items)} attempt={attempt}")
            return items,None
        except Exception as exc:
            error=f"{type(exc).__name__}: {exc}"
            print(f"NEWS_WARNING source={name!r} attempt={attempt}/{NEWS_ATTEMPTS} error={error}")
            if attempt<NEWS_ATTEMPTS: time.sleep(2**(attempt-1))
    return [],f"{name}: {error}"

def collect_news():
    items=[]; errors=[]; source_success=0
    for key,label,query in NEWS_QUERIES:
        url=f'https://news.google.com/rss/search?q={urllib.parse.quote(query+" when:1d")}&hl=en-US&gl=US&ceid=US:en'
        found,error=fetch_rss(key,label,url,f"Google News/{key}")
        items.extend(found); errors.extend([error] if error else []); source_success+=bool(found)
    if not items:
        for key,label,url in OFFICIAL_FEEDS:
            found,error=fetch_rss(key,label,url,"Federal Reserve",limit=4)
            cutoff=datetime.now(KST)-timedelta(days=2)
            found=[x for x in found if datetime.fromisoformat(x["published"])>=cutoff]
            items.extend(found); errors.extend([error] if error else []); source_success+=bool(found)
    deduped=[]; seen=set()
    for item in items:
        key=re.sub(r"\W+","",item["title"].lower())
        if key and key not in seen: seen.add(key); deduped.append(item)
    if deduped:
        NEWS_CACHE.parent.mkdir(parents=True,exist_ok=True)
        NEWS_CACHE.write_text(json.dumps({"saved_at":datetime.now(KST).isoformat(),"items":deduped},ensure_ascii=False,indent=2),encoding="utf-8")
        return deduped,{"sources_ok":source_success,"sources_total":len(NEWS_QUERIES),"cached":False,"errors":errors}
    if NEWS_CACHE.exists():
        cached=json.loads(NEWS_CACHE.read_text(encoding="utf-8")); age=datetime.now(KST)-datetime.fromisoformat(cached["saved_at"])
        if age<=timedelta(hours=48) and cached.get("items"):
            print(f"NEWS_WARNING using_cache=true age_hours={age.total_seconds()/3600:.1f} entries={len(cached['items'])}")
            return cached["items"],{"sources_ok":0,"sources_total":len(NEWS_QUERIES),"cached":True,"errors":errors}
    raise RuntimeError("뉴스 수집 결과가 0건이며 48시간 이내 정상 캐시도 없습니다: "+"; ".join(errors or ["all feeds returned zero entries"]))

def render_news(items):
    out=[]
    for item in items:
        try: stamp=datetime.fromisoformat(item["published"]).astimezone(KST).strftime("%m월 %d일 %H:%M")
        except Exception: stamp="최근"
        out.append(f'<article class="news-item" data-news-category="{escape(item["category"])}"><div class="news-source"><strong>{escape(item["source"])}</strong><span>{stamp}</span><b class="news-category">{escape(item["label"])}</b></div><a class="news-title" href="{escape(item["link"])}" target="_blank" rel="noreferrer">{escape(item["title"])}</a><a class="news-open" href="{escape(item["link"])}" target="_blank" rel="noreferrer">↗</a></article>')
    return "".join(out)

def main():
    now=datetime.now(KST); soup=BeautifulSoup(TEMPLATE.read_text(encoding="utf-8"),"html.parser"); failures=[]; indices={}; series={}
    for label,ticker,key in [("S&P 500","^GSPC","spx"),("NASDAQ","^IXIC","nasdaq"),("DOW JONES","^DJI","dow")]:
        v,ch,s=quote(ticker); indices[label]=(v,ch,s); series[key]=[round(float(x),4) for x in s.tail(60)]; set_card(soup,label,v,ch)
    macro_map={"GOLD · 금":("GC=F","${:,.2f}"),"BITCOIN":("BTC-USD","${:,.0f}"),"DOLLAR INDEX":("DX-Y.NYB","{:,.2f}"),"USD / KRW":("KRW=X","₩{:,.2f}"),"WTI":("CL=F","${:,.2f}"),"BRENT":("BZ=F","${:,.2f}"),"VIX":("^VIX","{:,.2f}")}; macro={}
    for label,(ticker,fmt) in macro_map.items():
        v,ch,s=quote(ticker,"1mo"); macro[label]=(v,ch,s); set_card(soup,label,v,ch,fmt)
    treasury_date,yields=treasury_yields(now.year)
    for label,(v,ch) in yields.items(): special(soup,label,f"{v:.2f}%",ch,"bp",1)
    spreads={"2s10s":(yields["US 10Y"][0]-yields["US 2Y"][0])*100,"10s30s":(yields["US 30Y"][0]-yields["US 10Y"][0])*100}
    special(soup,"2Y–10Y",f'{spreads["2s10s"]:+.0f}bp',spreads["2s10s"],"bp",0); special(soup,"10Y–30Y",f'{spreads["10s30s"]:+.0f}bp',spreads["10s30s"],"bp",0)
    try: tga_date,tga,tga_ch=treasury_tga(); special(soup,"TGA",f"${tga:,.0f}B",tga_ch,"B",0)
    except Exception as e: failures.append(f"TGA: {e}"); tga_date=None
    substitutions={"QNT":("Rigetti","NASDAQ · RGTI"),"INFQ":("D-Wave","NYSE · QBTS")}
    for old,(name,new_marker) in substitutions.items():
        marker=soup.find("small",string=re.compile(rf"\b{old}\b"))
        if marker:
            marker.string=new_marker; marker.parent.contents[0].replace_with(name+" ")
    for ticker in ["AAPL","MSFT","GOOGL","AMZN","NVDA","META","TSLA","IONQ","RGTI","QBTS","RKLB","RDW","ASTS"]:
        try: replace_stock(soup,ticker,quote(ticker,"1mo")[1])
        except Exception as e: failures.append(f"{ticker}: {e}")
    # Display order follows broad US equity market-cap weight, not daily return.
    # Semiconductors are a technology industry rather than a GICS sector, so SMH is shown separately.
    sectors={"XLK":"정보기술","SMH":"반도체","XLF":"금융","XLC":"커뮤니케이션","XLY":"경기소비재","XLI":"산업재","XLV":"헬스케어","XLP":"필수소비재","XLE":"에너지","XLU":"유틸리티","XLRE":"부동산","XLB":"소재"}; sch={k:quote(k,"1mo")[1] for k in sectors}; scale=max(1,max(abs(x) for x in sch.values()))
    rows="".join(f'<li><div class="row"><span>{name} <small class="note">{k}</small></span><b class="{cls(sch[k])}">{sch[k]:+.2f}%</b></div><div class="bar"><span style="width:{25+70*abs(sch[k])/scale:.0f}%"></span></div></li>' for k,name in sectors.items()); n=soup.select_one("#sector-list"); n.clear(); n.append(BeautifulSoup(rows,"html.parser"))
    br=sp500_breadth(); n=soup.select_one("#market-temperature"); n.clear(); n.append(BeautifulSoup(f'<li class="row"><span>상승 / 하락</span><b><span class="up">{br["adv"]}</span> / <span class="down">{br["dec"]}</span></b></li><li class="row"><span>50일선 상회</span><b>{br["above50"]:.1f}%</b></li><li class="row"><span>52주 신고가</span><b>{br["highs"]}</b></li><li class="row"><span>유효 구성종목</span><b>{br["total"]}</b></li>',"html.parser"))
    sp,nas=indices["S&P 500"],indices["NASDAQ"]; lead=("장기금리와 달러가 함께 낮아지며 기술주에 우호적인 위험선호 환경이 형성됐습니다." if nas[1]>0 and yields["US 10Y"][1]<0 and macro["DOLLAR INDEX"][1]<0 else "금리와 달러의 동반 상승이 주식 밸류에이션을 압박한 위험회피 장세였습니다." if sp[1]<0 and yields["US 10Y"][1]>0 and macro["DOLLAR INDEX"][1]>0 else "미국 증시는 상승 마감했지만 금리·달러 흐름을 함께 확인해야 하는 장세였습니다." if sp[1]>0 else "미국 증시는 하락 마감해 위험선호가 약해졌습니다.")
    detail=f'S&P 500 {sp[1]:+.2f}%, 나스닥 {nas[1]:+.2f}%, 10년물 {yields["US 10Y"][1]:+.1f}bp, DXY {macro["DOLLAR INDEX"][1]:+.2f}%, VIX {macro["VIX"][1]:+.2f}%. 상승 종목 비중은 {br["adv"]/br["total"]*100:.1f}%입니다.'; n=soup.select_one("#daily-summary"); n.clear(); n.append(BeautifulSoup(f'<strong>{lead}</strong> {detail}',"html.parser")); soup.select_one("#sample-warning").decompose()
    sp_tech=close("^GSPC","1y"); nas_tech=close("^IXIC","1y"); sp20=float(sp_tech.rolling(20).mean().iloc[-1]); sp50=float(sp_tech.rolling(50).mean().iloc[-1]); sp200=float(sp_tech.rolling(200).mean().iloc[-1]); nrsi=rsi(nas_tech); _,_,mh,mh_prev=macd(nas_tech); rv20=float(sp_tech.pct_change().tail(20).std()*math.sqrt(252)*100); high52=float(sp_tech.tail(252).max()); drawdown=(sp[0]/high52-1)*100
    trend="정배열" if sp[0]>sp20>sp50>sp200 else "역배열" if sp[0]<sp20<sp50<sp200 else "혼조"; macd_text="상승 강화" if mh>0 and mh>mh_prev else "상승 둔화" if mh>0 else "하락 완화" if mh>mh_prev else "하락 강화"
    score=sum([sp[0]>sp20,sp[0]>sp50,sp[0]>sp200,45<=nrsi<=70,mh>0,br["above50"]>=50,macro["VIX"][0]<20]); regime="상승 추세" if score>=6 else "조정 경계" if score<=2 else "중립·방향 확인"
    technical_html=f'<li class="row"><span>S&P 500 · 추세 배열</span><b>{trend} · 20/50/200일 {sp20:,.0f}/{sp50:,.0f}/{sp200:,.0f}</b></li><li class="row"><span>S&P 500 · 50일선 괴리</span><b>{(sp[0]/sp50-1)*100:+.2f}%</b></li><li class="row"><span>NASDAQ · RSI(14)</span><b>{nrsi:.1f} · {"과매수" if nrsi>=70 else "과매도" if nrsi<=30 else "중립"}</b></li><li class="row"><span>NASDAQ · MACD</span><b>{macd_text} · 히스토그램 {mh:+.1f}</b></li><li class="row"><span>S&P 시장 폭</span><b>20일 {br["above20"]:.1f}% · 50일 {br["above50"]:.1f}% · 200일 {br["above200"]:.1f}%</b></li><li class="row"><span>변동성·고점 위치</span><b>VIX {macro["VIX"][0]:.2f} · 실현변동성 {rv20:.1f}% · 52주 고점 대비 {drawdown:.1f}%</b></li><li class="row"><span>종합 기술 신호</span><strong class="{cls(score-4)}">{regime} ({score}/7)</strong></li>'
    n=soup.select_one("#technical-list"); n.clear(); n.append(BeautifulSoup(technical_html,"html.parser"))
    script=soup.find("script",string=re.compile("const chartSeries")); script.string=re.sub(r"const chartSeries=\{.*?\};","const chartSeries="+json.dumps(series)+";",script.string,flags=re.S)
    news,news_quality=collect_news(); feed=soup.select_one(".news-feed"); feed.clear(); feed.append(BeautifulSoup(render_news(news),"html.parser"))
    if news_quality["errors"]: failures.extend(news_quality["errors"])
    render_weekly_calendar(soup,now)
    soup.select_one("header .meta").string=now.strftime("%Y.%m.%d %H:%M KST")
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(str(soup),encoding="utf-8"); print(f"미국 증시 마감: S&P 500 {sp[0]:,.2f} ({sp[1]:+.2f}%). {lead}")

if __name__=="__main__": main()
