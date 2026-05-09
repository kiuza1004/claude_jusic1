# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt    # 의존성 설치
python app.py                      # 개발 서버 (http://localhost:5000, debug=True)
```

테스트 스위트는 없음. API 동작 검증이 필요할 때는 `app.test_client()`로 일회성 스크립트를 작성해 확인하고 커밋하지 말 것.

## Architecture

단일 Flask 앱 + 단일 HTML 템플릿 구조. 라우트는 3개뿐이며 모두 `app.py`에 있음.

```
사용자 입력 → /api/search (자동완성)
            → 사용자가 항목 선택 → selectedTicker 저장
            → /api/analyze (분석 데이터)
            → 프론트에서 Chart.js로 렌더
```

### 두 단계 검색 흐름의 이유
프론트엔드(`templates/index.html`)는 자동완성 드롭다운에서 선택한 티커를 `selectedTicker` 변수에 저장한다. 사용자가 입력을 변경하면 이 변수는 초기화되며, 그래야 종목명만 바꾸고 재분석할 때 이전 티커로 잘못 조회되지 않는다 (이전 버그 — 커밋 `3e8ce2f`). `analyzeSelected()`는 `selectedTicker`가 있으면 그대로 사용하고, 없으면 `/api/search`를 다시 호출해 첫 결과로 fallback한다.

### 검색의 듀얼 소스 (중요)
`yf.Search()`가 사용하는 Yahoo Finance 검색 API는 **한글 쿼리에 HTTP 400** "Invalid Search Query"를 반환한다. 이를 우회하려고 Naver Finance 자동완성 API(`m.stock.naver.com/front-api/search/autoComplete`)를 듀얼 소스로 사용한다.

`/api/search`의 분기:
- **한글 쿼리** → Naver 우선, 빈 결과면 Yahoo fallback
- **영문/숫자 쿼리** → Yahoo 우선, 빈 결과면 Naver fallback

Naver 응답의 `code`/`typeCode`는 `_naver_to_yahoo_ticker()`가 Yahoo 형식으로 변환한다 (KOSPI→`.KS`, KOSDAQ→`.KQ`, NASDAQ/NYSE는 그대로, HKG→`.HK` 등). Naver는 USA/JPN/HKG 종목도 한글명으로 검색 가능 ("테슬라" → TSLA). 비공식 API이므로 응답 스키마가 바뀔 가능성에 주의.

### 백엔드/프론트엔드 책임 분리
- **백엔드 (`get_stock_analysis`)**: yfinance로 6개월 시세를 받아 RSI(14)/볼린저밴드(20,2)/거래량 비율/변동성을 계산하고, **표시용 문자열까지 포맷해서** JSON으로 반환 (`fmt_price`, `fmt_num`). 프론트는 통화/단위 처리를 하지 않는다.
- **프론트엔드**: 백엔드가 보낸 raw `chart_data`(60거래일 OHLCV)와 `rsi_chart`로 Chart.js 차트를 그린다. 볼린저밴드는 차트용으로 프론트에서 다시 계산한다 (백엔드가 보낸 마지막 시점 BB 값과 별개).

### 데이터 모양
`/api/analyze` 응답은 평탄한 객체. `top_holders`(기관 보유)와 `news`(최근 5건)는 yfinance가 반환하지 않거나 비면 빈 배열로 폴백한다 — 프론트는 항상 길이 체크 후 렌더해야 한다.
