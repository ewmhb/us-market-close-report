from datetime import datetime
from email.utils import parsedate_to_datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo
import re, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

import feedparser
import yfinance as yf
from bs4 import BeautifulSoup

KST=ZoneInfo("Asia/Seoul")
TEMPLATE=Path("report-template.html"); OUT=Path("site/index.html")

def history(ticker, period="6mo"):
    frame=yf.download(ticker,period=period,progress=False,auto_adjust=False)
    close=frame["Close"].dropna(); values=close.to_numpy().reshape(-1)
    if len(values)<2: raise RuntimeError(f"가격 데이터 부족: {ticker}")
    return float(values[-1]),(float(values[-1])/float(values[-2])-1)*100,[float(x) for x in values[-60:]]

def set_card(soup,label,value,change,fmt="{:,.2f}"):
    nodes=[x for x in soup.select(".label,.macro-card span") if x.get_text(strip=True)==label]
    for node in nodes:
        card=node.find_parent(["article"]); val=card.select_one(".value") or card.find("strong")
        delta=card.select_one(".up,.down,.flat")
        val.string=fmt.format(value); delta["class"]=["up" if change>0 else "down" if change<0 else "flat"]
        delta.string=("▲" if change>0 else "▼" if change<0 else "–")+f" {abs(change):.2f}%"

def treasury_yields(year):
    query=urllib.parse.urlencode({"data":"daily_treasury_yield_curve","field_tdr_date_value":year})
    request=urllib.request.Request(
        f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?{query}",
        headers={"User-Agent":"us-market-close-report/1.0"},
    )
    with urllib.request.urlopen(request,timeout=30) as response:
        root=ET.fromstring(response.read())
    ns={"atom":"http://www.w3.org/2005/Atom","m":"http://schemas.microsoft.com/ado/2007/08/dataservices/metadata","d":"http://schemas.microsoft.com/ado/2007/08/dataservices"}
    rows=[]
    for entry in root.findall("atom:entry",ns):
        props=entry.find("atom:content/m:properties",ns)
        if props is None: continue
        def value(name):
            node=props.find(f"d:{name}",ns)
            return float(node.text) if node is not None and node.text else None
        date_node=props.find("d:NEW_DATE",ns)
        if date_node is None or not date_node.text: continue
        yields={label:value(field) for label,field in {"US 2Y":"BC_2YEAR","US 10Y":"BC_10YEAR","US 30Y":"BC_30YEAR"}.items()}
        if all(rate is not None for rate in yields.values()): rows.append((date_node.text,yields))
    if len(rows)<2: raise RuntimeError("미 재무부 수익률 데이터가 부족합니다.")
    rows.sort(key=lambda row:row[0])
    previous,current=rows[-2][1],rows[-1][1]
    return {label:(rate,(rate-previous[label])*100) for label,rate in current.items()}

def set_yield_card(soup,label,value,change_bp):
    nodes=[x for x in soup.select(".macro-card span") if x.get_text(strip=True)==label]
    for node in nodes:
        card=node.find_parent("article"); card.find("strong").string=f"{value:.2f}%"
        delta=card.select_one(".up,.down,.flat")
        delta["class"]=["up" if change_bp>0 else "down" if change_bp<0 else "flat"]
        delta.string=("▲" if change_bp>0 else "▼" if change_bp<0 else "–")+f" {abs(change_bp):.1f}bp"

def replace_stock(soup,ticker,change):
    marker=soup.find("small",string=re.compile(rf"\b{re.escape(ticker)}\b"))
    if marker:
        value=marker.find_parent("li").find("b"); value["class"]=["up" if change>0 else "down" if change<0 else "flat"]
        value.string=f"{change:+.2f}%"

def rss(category,query,limit=3):
    url=f'https://news.google.com/rss/search?q={urllib.parse.quote(query+" when:1d")}&hl=en-US&gl=US&ceid=US:en&_={int(time.time())}'
    items=[]
    for entry in feedparser.parse(url).entries[:limit]:
        try: stamp=parsedate_to_datetime(entry.published).astimezone(KST).strftime("%m월 %d일 %H:%M")
        except Exception: stamp="최근"
        source=getattr(getattr(entry,"source",None),"title","Google News")
        title=re.sub(r"\s+-\s+[^-]+$","",entry.title).strip()
        items.append((category,source,stamp,title,entry.link))
    return items

def news_html():
    groups=[("macro","매크로","US economy inflation GDP dollar markets"),("geo","지정학","geopolitics oil Ukraine Iran market"),("fed","연준","Federal Reserve rates Jackson Hole markets"),("market","마켓","Wall Street S&P Nasdaq Dow close")]
    rows=[]
    for key,label,query in groups:
        for _,source,stamp,title,link in rss(key,query,2):
            rows.append(f'<article class="news-item" data-news-category="{key}"><div class="news-source"><strong>{escape(source)}</strong><span>{stamp}</span><b class="news-category">{label}</b></div><a class="news-title" href="{escape(link)}" target="_blank" rel="noreferrer">{escape(title)}</a><a class="news-open" href="{escape(link)}" target="_blank" rel="noreferrer" aria-label="기사 열기">↗</a></article>')
    return "".join(rows)

def main():
    now=datetime.now(KST); soup=BeautifulSoup(TEMPLATE.read_text(encoding="utf-8"),"html.parser")
    index_map={"S&P 500":"^GSPC","NASDAQ":"^IXIC","DOW JONES":"^DJI"}; series={}; snapshots={}
    for label,ticker in index_map.items():
        try:
            value,change,points=history(ticker)
            snapshots[label]=(value,change)
            series[{"S&P 500":"spx","NASDAQ":"nasdaq","DOW JONES":"dow"}[label]]=points
            set_card(soup,label,value,change)
        except Exception as exc:
            print(f"{label}: {exc}")
    macro={"GOLD · 금":("GC=F","${:,.2f}"),"BITCOIN":("BTC-USD","${:,.0f}"),"DOLLAR INDEX":("DX-Y.NYB","{:,.2f}"),"USD / KRW":("KRW=X","₩{:,.2f}"),"WTI":("CL=F","${:,.2f}"),"BRENT":("BZ=F","${:,.2f}"),"VIX":("^VIX","{:,.2f}")}
    for label,(ticker,fmt) in macro.items():
        try: value,change,_=history(ticker,"10d"); set_card(soup,label,value,change,fmt)
        except Exception as exc: print(f"{label}: {exc}")
    yields=treasury_yields(now.year)
    for label,(value,change_bp) in yields.items(): set_yield_card(soup,label,value,change_bp)
    stocks={"AAPL":"AAPL","MSFT":"MSFT","GOOGL":"GOOGL","AMZN":"AMZN","NVDA":"NVDA","META":"META","TSLA":"TSLA","IONQ":"IONQ","QNT":"QNT","INFQ":"INFQ","RKLB":"RKLB","RDW":"RDW","ASTS":"ASTS"}
    for code,ticker in stocks.items():
        try: _,change,_=history(ticker,"10d"); replace_stock(soup,code,change)
        except Exception as exc: print(f"{code}: {exc}")
    script=soup.find("script",string=re.compile("const chartSeries"))
    if script:
        replacement="const chartSeries="+str(series).replace("'",'"')+";"
        script.string=re.sub(r"const chartSeries=\{.*?\};",replacement,script.string,flags=re.S)
    feed=soup.select_one(".news-feed")
    try: feed.clear(); feed.append(BeautifulSoup(news_html(),"html.parser"))
    except Exception as exc: print(f"뉴스 조회 실패: {exc}")
    meta=soup.select_one("header .meta"); meta.string=now.strftime("%Y.%m.%d %H:%M KST")
    sp=snapshots.get("S&P 500")
    rates=f"미 국채 10년 {yields['US 10Y'][0]:.2f}%, 30년 {yields['US 30Y'][0]:.2f}%."
    summary=(f"미국 증시 마감: S&P 500 {sp[0]:,.2f} ({sp[1]:+.2f}%). {rates}" if sp else f"미국 증시 장마감 리포트가 업데이트되었습니다. {rates}")
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(str(soup),encoding="utf-8"); print(summary)

if __name__=="__main__": main()

