from flask import Flask, render_template, request, jsonify
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timezone
import requests
import json
import re

app = Flask(__name__)

_NAVER_AC_URL = "https://m.stock.naver.com/front-api/search/autoComplete"
_NAVER_AC_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://m.stock.naver.com/",
}


KR_TICKER_RE = re.compile(r"^\d{6}\.(KS|KQ)$")


def _naver_to_yahoo_ticker(item: dict):
    """Naver autoComplete item → Yahoo 티커. 한국 KOSPI/KOSDAQ만 허용."""
    code = (item.get("code") or "").strip()
    if not code or not code.isdigit() or len(code) != 6:
        return None
    type_code = (item.get("typeCode") or "").upper()
    if type_code == "KOSPI":
        return f"{code}.KS"
    if type_code == "KOSDAQ":
        return f"{code}.KQ"
    return None


def _naver_search(query: str):
    try:
        r = requests.get(
            _NAVER_AC_URL,
            params={"query": query, "target": "stock,index,marketindicator,coin,ipo"},
            headers=_NAVER_AC_HEADERS,
            timeout=5,
        )
        if r.status_code != 200:
            return []
        items_raw = ((r.json().get("result") or {}).get("items")) or []
    except Exception:
        return []

    items = []
    seen = set()
    for it in items_raw:
        if (it.get("category") or "stock") != "stock":
            continue
        ticker = _naver_to_yahoo_ticker(it)
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        items.append({
            "ticker": ticker,
            "name": it.get("name", ""),
            "exchange": it.get("typeName") or it.get("typeCode") or "",
            "type": "Equity",
        })
        if len(items) >= 8:
            break
    return items


@app.route("/api/search", methods=["POST"])
def search():
    data = request.get_json()
    query = (data.get("query") or "").strip()
    if len(query) < 1:
        return jsonify([])
    # 한국 KOSPI/KOSDAQ 종목만 노출
    return jsonify(_naver_search(query))


def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_bollinger(series, period=20, std_dev=2):
    mid = series.rolling(period).mean()
    std = series.rolling(period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return upper, mid, lower


def _get_investor_trading(ticker: str):
    """한국 주식(.KS/.KQ) 최근 5거래일 외국인/기관 순매매. 개인은 -(외+기) 추정."""
    m = re.match(r"^(\d{6})\.(KS|KQ)$", ticker.upper())
    if not m:
        return []
    code = m.group(1)
    try:
        r = requests.get(
            f"https://finance.naver.com/item/frgn.naver?code={code}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        r.encoding = "euc-kr"
        html = r.text
    except Exception:
        return []

    table_m = re.search(
        r'<table[^>]*summary="[^"]*외국인 기관 순매매[^"]*"[^>]*>(.*?)</table>',
        html, re.S,
    )
    if not table_m:
        return []
    rows = re.findall(r"<tr onMouseOver[^>]*>(.*?)</tr>", table_m.group(1), re.S)

    def to_int(s):
        s = re.sub(r"<[^>]+>", "", s).strip().replace(",", "").replace("+", "")
        try:
            return int(s)
        except ValueError:
            return 0

    items = []
    for row in rows[:5]:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 9:
            continue
        date = re.sub(r"<[^>]+>", "", cells[0]).strip()
        if not date:
            continue
        close = to_int(cells[1])
        volume = to_int(cells[4])
        inst_net = to_int(cells[5])
        foreign_net = to_int(cells[6])
        individual_net = -(inst_net + foreign_net)
        items.append({
            "date": date,
            "close": close,
            "volume": volume,
            "individual": individual_net,
            "foreign": foreign_net,
            "institutional": inst_net,
        })
    return items


def _get_naver_news(ticker: str, limit: int = 5):
    """Naver Finance 종목 뉴스 페이지 스크래핑 (한국어 뉴스)."""
    m = KR_TICKER_RE.match(ticker.upper())
    if not m:
        return []
    code = ticker.split(".")[0]
    try:
        r = requests.get(
            "https://finance.naver.com/item/news_news.naver",
            params={"code": code, "page": 1, "sm": "title_entity_id.basic", "clusterId": ""},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"},
            timeout=5,
        )
        r.encoding = "euc-kr"
        html = r.text
    except Exception:
        return []

    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)
    items = []
    seen_ids = set()
    for row in rows:
        if "relation_lst" in row:
            continue  # 연관 기사 묶음 제외
        m_a = re.search(r'<a[^>]*href="(/item/news_read[^"]+)"[^>]*class="tit"[^>]*>(.*?)</a>', row, re.S)
        if not m_a:
            continue
        href = m_a.group(1)
        title = _unescape_html(re.sub(r"<[^>]+>", "", m_a.group(2)).strip())
        m_office = re.search(r'class="info"[^>]*>(.*?)</td>', row, re.S)
        m_date = re.search(r'class="date"[^>]*>(.*?)</td>', row, re.S)
        office = re.sub(r"<[^>]+>", "", m_office.group(1)).strip() if m_office else ""
        date = re.sub(r"<[^>]+>", "", m_date.group(1)).strip() if m_date else ""

        ar_id = re.search(r"article_id=(\d+)", href)
        of_id = re.search(r"office_id=(\d+)", href)
        if not (ar_id and of_id):
            continue
        key = (of_id.group(1), ar_id.group(1))
        if key in seen_ids:
            continue
        seen_ids.add(key)
        url = f"https://n.news.naver.com/article/{of_id.group(1)}/{ar_id.group(1)}"

        items.append({
            "title": title,
            "publisher": office,
            "url": url,
            "published": date,
            "summary": "",
        })
        if len(items) >= limit:
            break
    return items


def _unescape_html(s: str) -> str:
    return (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&#39;", "'").replace("&apos;", "'")
             .replace("&hellip;", "…").replace("&lsquo;", "'").replace("&rsquo;", "'")
             .replace("&ldquo;", '"').replace("&rdquo;", '"').replace("&nbsp;", " "))


def _get_recent_dividends(tk, hist):
    """최근 1년 배당 이벤트. 배당락일 기준, 시가배당률 계산."""
    try:
        divs = tk.dividends
        if divs is None or divs.empty:
            return []
        tz = divs.index.tz
        cutoff = pd.Timestamp.now(tz=tz) - pd.Timedelta(days=365)
        recent = divs[divs.index >= cutoff]
        if recent.empty:
            return []

        hist_close = hist["Close"]
        if hist_close.index.tz is None and tz is not None:
            divs_idx = recent.index.tz_localize(None)
        else:
            divs_idx = recent.index

        items = []
        for orig_dt, amount in zip(recent.index, recent.values):
            lookup_dt = orig_dt.tz_localize(None) if (hist_close.index.tz is None and orig_dt.tz is not None) else orig_dt
            try:
                prev = hist_close[hist_close.index <= lookup_dt]
                close_at = float(prev.iloc[-1]) if not prev.empty else None
            except Exception:
                close_at = None
            yield_pct = (float(amount) / close_at * 100) if close_at else None
            items.append({
                "date": str(orig_dt.date()) if hasattr(orig_dt, "date") else str(orig_dt)[:10],
                "amount": round(float(amount), 4),
                "close_at": round(close_at, 2) if close_at else None,
                "yield_pct": round(yield_pct, 3) if yield_pct else None,
            })
        items.sort(key=lambda x: x["date"], reverse=True)
        return items
    except Exception:
        return []


def get_stock_analysis(ticker: str):
    tk = yf.Ticker(ticker)
    info = tk.info

    # 가격 히스토리 (6개월)
    hist = tk.history(period="6mo")
    if hist.empty:
        return {"error": f"티커 '{ticker}'에 대한 데이터를 찾을 수 없습니다."}

    close = hist["Close"]
    volume = hist["Volume"]

    # 기본 정보
    name = info.get("longName") or info.get("shortName") or ticker
    # 네이버에서 한글명 조회 (KOSPI/KOSDAQ 한정)
    code = ticker.split(".")[0]
    naver_results = _naver_search(code) if code.isdigit() else []
    name_kr = next(
        (r["name"] for r in naver_results if r["ticker"] == ticker.upper()),
        None,
    ) or name
    currency = info.get("currency", "")
    current_price = float(close.iloc[-1])
    prev_close = float(close.iloc[-2]) if len(close) > 1 else current_price
    change_pct = (current_price - prev_close) / prev_close * 100

    # RSI
    rsi_series = calc_rsi(close)
    rsi = float(rsi_series.iloc[-1]) if not pd.isna(rsi_series.iloc[-1]) else None

    # 볼린저 밴드
    bb_upper, bb_mid, bb_lower = calc_bollinger(close)
    bb_u = float(bb_upper.iloc[-1]) if not pd.isna(bb_upper.iloc[-1]) else None
    bb_m = float(bb_mid.iloc[-1]) if not pd.isna(bb_mid.iloc[-1]) else None
    bb_l = float(bb_lower.iloc[-1]) if not pd.isna(bb_lower.iloc[-1]) else None

    bb_position = "N/A"
    if bb_u and bb_l and bb_m:
        if current_price >= bb_u:
            bb_position = "상단 돌파 (과매수 신호)"
        elif current_price >= bb_m:
            bb_position = "중앙~상단 (강세 구간)"
        elif current_price >= bb_l:
            bb_position = "하단~중앙 (약세 구간)"
        else:
            bb_position = "하단 이탈 (과매도 신호)"

    # 거래량 분석
    vol_20avg = float(volume.rolling(20).mean().iloc[-1])
    vol_today = float(volume.iloc[-1])
    vol_ratio = vol_today / vol_20avg if vol_20avg > 0 else 1.0

    # 변동성 (주간/월간)
    week_ago = close.iloc[-6] if len(close) >= 6 else close.iloc[0]
    month_ago = close.iloc[-22] if len(close) >= 22 else close.iloc[0]
    week_chg = (current_price - float(week_ago)) / float(week_ago) * 100
    month_chg = (current_price - float(month_ago)) / float(month_ago) * 100

    # 52주 고/저
    high_52w = info.get("fiftyTwoWeekHigh")
    low_52w = info.get("fiftyTwoWeekLow")

    # 목표주가 / 애널리스트
    target_mean = info.get("targetMeanPrice")
    target_high = info.get("targetHighPrice")
    target_low = info.get("targetLowPrice")
    recommendation = info.get("recommendationKey", "N/A")
    analyst_count = info.get("numberOfAnalystOpinions")

    # 공매도 비율
    short_ratio = info.get("shortRatio")
    short_pct = info.get("shortPercentOfFloat")

    # 시가총액 / PER / PBR
    market_cap = info.get("marketCap")
    pe_ratio = info.get("trailingPE")
    pb_ratio = info.get("priceToBook")
    dividend_yield = info.get("dividendYield")

    # 최근 뉴스 (Naver Finance — 국내 한국어 뉴스만)
    news = _get_naver_news(ticker, limit=5)

    # 기관/외국인 수급 (미국주식은 institutional holders로 대체)
    try:
        inst_holders = tk.institutional_holders
        if inst_holders is not None and not inst_holders.empty:
            top_holders = inst_holders.head(5).to_dict("records")
        else:
            top_holders = []
    except Exception:
        top_holders = []

    # 투자자별 매매동향 (한국 주식만) / 최근 1년 배당
    investor_trading = _get_investor_trading(ticker)
    dividends_recent = _get_recent_dividends(tk, hist)

    # 최근 20일 가격 데이터 (차트용)
    recent_20 = hist.tail(60).reset_index()
    chart_data = [
        {
            "date": str(row["Date"].date()) if hasattr(row["Date"], "date") else str(row["Date"])[:10],
            "open": round(float(row["Open"]), 2),
            "high": round(float(row["High"]), 2),
            "low": round(float(row["Low"]), 2),
            "close": round(float(row["Close"]), 2),
            "volume": int(row["Volume"]),
        }
        for _, row in recent_20.iterrows()
    ]

    # RSI 시리즈 (차트용)
    rsi_chart = []
    for i, (date, val) in enumerate(rsi_series.tail(60).items()):
        if not pd.isna(val):
            d = str(date.date()) if hasattr(date, "date") else str(date)[:10]
            rsi_chart.append({"date": d, "rsi": round(float(val), 2)})

    def fmt_price(v):
        if v is None:
            return "N/A"
        return f"{v:,.2f} {currency}"

    def fmt_num(v, suffix=""):
        if v is None:
            return "N/A"
        if v >= 1e12:
            return f"{v/1e12:.2f}T{suffix}"
        if v >= 1e9:
            return f"{v/1e9:.2f}B{suffix}"
        if v >= 1e6:
            return f"{v/1e6:.2f}M{suffix}"
        return f"{v:,.0f}{suffix}"

    return {
        "ticker": ticker.upper(),
        "name": name,
        "name_kr": name_kr,
        "currency": currency,
        "current_price": fmt_price(current_price),
        "change_pct": round(change_pct, 2),
        "market_cap": fmt_num(market_cap),
        "pe_ratio": round(pe_ratio, 2) if pe_ratio else "N/A",
        "pb_ratio": round(pb_ratio, 2) if pb_ratio else "N/A",
        "dividend_yield": f"{dividend_yield*100:.2f}%" if dividend_yield else "N/A",
        "high_52w": fmt_price(high_52w),
        "low_52w": fmt_price(low_52w),
        "rsi": round(rsi, 2) if rsi else "N/A",
        "rsi_signal": (
            "과매수 (70 이상)" if rsi and rsi >= 70
            else "과매도 (30 이하)" if rsi and rsi <= 30
            else "중립 구간"
        ) if rsi else "N/A",
        "bb_upper": fmt_price(bb_u),
        "bb_mid": fmt_price(bb_m),
        "bb_lower": fmt_price(bb_l),
        "bb_position": bb_position,
        "vol_today": fmt_num(vol_today),
        "vol_20avg": fmt_num(vol_20avg),
        "vol_ratio": round(vol_ratio, 2),
        "week_chg": round(week_chg, 2),
        "month_chg": round(month_chg, 2),
        "target_mean": fmt_price(target_mean),
        "target_high": fmt_price(target_high),
        "target_low": fmt_price(target_low),
        "recommendation": recommendation.upper().replace("-", " ") if recommendation != "N/A" else "N/A",
        "analyst_count": analyst_count or "N/A",
        "short_ratio": round(short_ratio, 2) if short_ratio else "N/A",
        "short_pct": f"{short_pct*100:.2f}%" if short_pct else "N/A",
        "top_holders": top_holders,
        "news": news,
        "investor_trading": investor_trading,
        "dividends_recent": dividends_recent,
        "chart_data": chart_data,
        "rsi_chart": rsi_chart,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json()
    ticker = (data.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify({"error": "티커를 입력하세요."}), 400
    if not KR_TICKER_RE.match(ticker):
        return jsonify({"error": "한국 KOSPI/KOSDAQ 종목만 지원합니다."}), 400
    result = get_stock_analysis(ticker)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
