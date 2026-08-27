from datetime import datetime
from email.utils import parsedate_to_datetime
from html import escape
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo
import json, math, re, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

import feedparser, pandas as pd, yfinance as yf
from bs4 import BeautifulSoup

KST=ZoneInfo("Asia/Seoul"); TEMPLATE=Path("report-template.html"); OUT=Path("site/index.html")

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
    ma50=c.rolling(50).mean().iloc[-1]; hi=c.rolling(252,min_periods=200).max().iloc[-1]
    return {"adv":int((daily>0).sum()),"dec":int((daily<0).sum()),"total":int(valid.sum()),"above50":float((last[valid]>ma50[valid]).mean()*100),"highs":int((last[valid]>=hi[valid]*.999999).sum())}

def rsi(s,n=14):
    d=s.diff(); g=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean(); l=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean()
    return float((100-100/(1+g/l.replace(0,math.nan))).iloc[-1])

def rss(key,label,query):
    url=f'https://news.google.com/rss/search?q={urllib.parse.quote(query+" when:1d")}&hl=en-US&gl=US&ceid=US:en&_={int(time.time())}'; out=[]
    for e in feedparser.parse(url).entries[:2]:
        try: stamp=parsedate_to_datetime(e.published).astimezone(KST).strftime("%m월 %d일 %H:%M")
        except Exception: stamp="최근"
        source=getattr(getattr(e,"source",None),"title","Google News"); title=re.sub(r"\s+-\s+[^-]+$","",e.title).strip()
        out.append(f'<article class="news-item" data-news-category="{key}"><div class="news-source"><strong>{escape(source)}</strong><span>{stamp}</span><b class="news-category">{label}</b></div><a class="news-title" href="{escape(e.link)}" target="_blank" rel="noreferrer">{escape(title)}</a><a class="news-open" href="{escape(e.link)}" target="_blank" rel="noreferrer">↗</a></article>')
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
    sectors={"XLK":"기술","XLC":"커뮤니케이션","XLY":"경기소비재","XLF":"금융","XLI":"산업재","XLE":"에너지","XLV":"헬스케어","XLP":"필수소비재","XLU":"유틸리티","XLRE":"부동산","XLB":"소재"}; sch={k:quote(k,"1mo")[1] for k in sectors}; scale=max(1,max(abs(x) for x in sch.values()))
    rows="".join(f'<li><div class="row"><span>{sectors[k]} <small class="note">{k}</small></span><b class="{cls(v)}">{v:+.2f}%</b></div><div class="bar"><span style="width:{25+70*abs(v)/scale:.0f}%"></span></div></li>' for k,v in sorted(sch.items(),key=lambda x:x[1],reverse=True)); n=soup.select_one("#sector-list"); n.clear(); n.append(BeautifulSoup(rows,"html.parser"))
    br=sp500_breadth(); n=soup.select_one("#market-temperature"); n.clear(); n.append(BeautifulSoup(f'<li class="row"><span>상승 / 하락</span><b><span class="up">{br["adv"]}</span> / <span class="down">{br["dec"]}</span></b></li><li class="row"><span>50일선 상회</span><b>{br["above50"]:.1f}%</b></li><li class="row"><span>52주 신고가</span><b>{br["highs"]}</b></li><li class="row"><span>유효 구성종목</span><b>{br["total"]}</b></li>',"html.parser"))
    sp,nas=indices["S&P 500"],indices["NASDAQ"]; lead=("장기금리와 달러가 함께 낮아지며 기술주에 우호적인 위험선호 환경이 형성됐습니다." if nas[1]>0 and yields["US 10Y"][1]<0 and macro["DOLLAR INDEX"][1]<0 else "금리와 달러의 동반 상승이 주식 밸류에이션을 압박한 위험회피 장세였습니다." if sp[1]<0 and yields["US 10Y"][1]>0 and macro["DOLLAR INDEX"][1]>0 else "미국 증시는 상승 마감했지만 금리·달러 흐름을 함께 확인해야 하는 장세였습니다." if sp[1]>0 else "미국 증시는 하락 마감해 위험선호가 약해졌습니다.")
    detail=f'S&P 500 {sp[1]:+.2f}%, 나스닥 {nas[1]:+.2f}%, 10년물 {yields["US 10Y"][1]:+.1f}bp, DXY {macro["DOLLAR INDEX"][1]:+.2f}%, VIX {macro["VIX"][1]:+.2f}%. 상승 종목 비중은 {br["adv"]/br["total"]*100:.1f}%입니다.'; n=soup.select_one("#daily-summary"); n.clear(); n.append(BeautifulSoup(f'<strong>{lead}</strong> {detail}',"html.parser")); soup.select_one("#sample-warning").decompose()
    sp20=float(sp[2].rolling(20).mean().iloc[-1]); nrsi=rsi(nas[2]); n=soup.select_one("#technical-list"); n.clear(); n.append(BeautifulSoup(f'<li class="row"><span>S&P 500 · 20일선</span><b>{"상회" if sp[0]>sp20 else "하회"} ({sp20:,.0f})</b></li><li class="row"><span>NASDAQ · RSI (14)</span><b>{nrsi:.1f}</b></li><li class="row"><span>VIX</span><b>{macro["VIX"][0]:.2f}</b></li>',"html.parser"))
    script=soup.find("script",string=re.compile("const chartSeries")); script.string=re.sub(r"const chartSeries=\{.*?\};","const chartSeries="+json.dumps(series)+";",script.string,flags=re.S)
    feed=soup.select_one(".news-feed"); feed.clear(); feed.append(BeautifulSoup("".join(rss(*g) for g in [("macro","매크로","US economy inflation GDP dollar markets"),("geo","지정학","geopolitics oil market"),("fed","연준","Federal Reserve rates markets"),("market","마켓","Wall Street S&P Nasdaq Dow close")]),"html.parser"))
    soup.select_one("header .meta").string=now.strftime("%Y.%m.%d %H:%M KST"); n=soup.select_one("#data-quality"); n.clear(); price_date=sp[2].index[-1].date(); n.append(BeautifulSoup(f'<li><b>가격 기준일</b><div class="note">미국 정규장 {price_date} 종가 · Yahoo Finance</div></li><li><b>국채 기준일</b><div class="note">{treasury_date} · U.S. Treasury</div></li><li><b>TGA 기준일</b><div class="note">{tga_date or "조회 실패"} · U.S. Treasury Fiscal Data (일간)</div></li><li><b>정합성 검사</b><div class="note">핵심지수 3개·매크로 7개·S&P 구성종목 {br["total"]}개 · 보조 오류 {len(failures)}건</div></li>',"html.parser"))
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(str(soup),encoding="utf-8"); print(f"미국 증시 마감: S&P 500 {sp[0]:,.2f} ({sp[1]:+.2f}%). {lead}")

if __name__=="__main__": main()
