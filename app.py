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


def _has_korean(text: str) -> bool:
    return bool(re.search(r"[\uac00-\ud7a3]", text))


def _naver_to_yahoo_ticker(item: dict):
    """Naver autoComplete item → Yahoo Finance 티커 형식으로 변환."""
    code = (item.get("code") or "").strip()
    if not code:
        return None
    type_code = (item.get("typeCode") or "").upper()
    if type_code == "KOSPI":
        return f"{code}.KS"
    if type_code == "KOSDAQ":
        return f"{code}.KQ"
    if type_code in ("NASDAQ", "NYSE", "NYSE_ARCA", "AMEX", "OTC"):
        return code
    if type_code == "HONG_KONG":
        return f"{code.lstrip('0') or '0'}.HK"
    if type_code in ("TOKYO", "OSAKA"):
        return f"{code}.T"
    if type_code == "LONDON":
        return f"{code}.L"
    if type_code in ("SHANGHAI",):
        return f"{code}.SS"
    if type_code in ("SHENZHEN",):
        return f"{code}.SZ"
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


def _yahoo_search(query: str):
    try:
        result = yf.Search(query, max_results=8, news_count=0)
    except Exception:
        return []
    items = []
    for q in (result.quotes or []):
        if q.get("quoteType") in ("EQUITY", "ETF", "MUTUALFUND"):
            items.append({
                "ticker": q.get("symbol", ""),
                "name": q.get("longname") or q.get("shortname") or "",
                "exchange": q.get("exchDisp") or q.get("exchange") or "",
                "type": q.get("typeDisp") or q.get("quoteType") or "",
            })
    return items


@app.route("/api/search", methods=["POST"])
def search():
    data = request.get_json()
    query = (data.get("query") or "").strip()
    if len(query) < 1:
        return jsonify([])

    # 한글 → Naver, 영문/숫자 → Yahoo 우선. 빈 결과면 다른 소스로 fallback.
    if _has_korean(query):
        items = _naver_search(query) or _yahoo_search(query)
    else:
        items = _yahoo_search(query) or _naver_search(query)

    return jsonify(items)


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

    # 최근 뉴스 (최신 날짜순 정렬 후 5개)
    try:
        news_raw = tk.news or []
        news_parsed = []
        for n in news_raw:
            content = n.get("content", {})
            pub_str = content.get("pubDate", "")
            try:
                pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            except Exception:
                pub_dt = datetime.min.replace(tzinfo=timezone.utc)
            news_parsed.append({
                "title": content.get("title", ""),
                "publisher": content.get("provider", {}).get("displayName", ""),
                "url": content.get("canonicalUrl", {}).get("url", ""),
                "published": pub_str,
                "published_dt": pub_dt,
                "summary": content.get("summary", ""),
            })
        news_parsed.sort(key=lambda x: x["published_dt"], reverse=True)
        news = [
            {k: v for k, v in item.items() if k != "published_dt"}
            for item in news_parsed[:5]
        ]
    except Exception:
        news = []

    # 기관/외국인 수급 (미국주식은 institutional holders로 대체)
    try:
        inst_holders = tk.institutional_holders
        if inst_holders is not None and not inst_holders.empty:
            top_holders = inst_holders.head(5).to_dict("records")
        else:
            top_holders = []
    except Exception:
        top_holders = []

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
    result = get_stock_analysis(ticker)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
