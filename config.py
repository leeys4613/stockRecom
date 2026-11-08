"""설정: 경로, 지표 목록, 점수 가중치/임계값 (가중치는 초기안 — 사람 확인 필요)."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = Path(os.getenv("STOCKRECOM_DB") or BASE_DIR / "stockrecom.db")

# 매크로 지표: key -> (표시명, yfinance 심볼)
MACRO_INDICATORS = {
    "US10Y": ("미국채 10년", "^TNX"),
    "USDKRW": ("USD/KRW", "KRW=X"),
    "USDJPY": ("USD/JPY", "JPY=X"),
    "USDCNH": ("USD/CNY (CNH 대용)", "CNY=X"),
    "BTC": ("비트코인", "BTC-USD"),
    "SOX": ("필라델피아 반도체", "^SOX"),
    "WTI": ("WTI 원유", "CL=F"),
}

# 데이터 신선도 기준(시간): 이보다 오래되면 stale
STALE_AFTER_HOURS = 6

# --- 점수 엔진 초기안 (⚠ 사람 확인 필요) ---
# 가중치: 양수 = 지표 상승이 국장에 우호적, 음수 = 비우호적. 절대값 합 = 1.0
SCORE_WEIGHTS = {
    "SOX": 0.30,
    "USDKRW": -0.25,
    "US10Y": -0.15,
    "USDJPY": 0.05,
    "USDCNH": -0.10,
    "BTC": 0.05,
    "WTI": -0.10,
}
# 지표 방향(고정): +1 = 지표 상승이 국장에 우호적, -1 = 비우호적. 사용자 조정은 크기만 허용한다
SCORE_DIRECTION = {k: (1 if w >= 0 else -1) for k, w in SCORE_WEIGHTS.items()}
# 일간 변동률(%)이 이 값이면 지표 점수가 ±1(포화)이 된다
SCORE_SATURATION_PCT = {
    "SOX": 3.0,
    "USDKRW": 1.0,
    "US10Y": 3.0,
    "USDJPY": 1.0,
    "USDCNH": 0.7,
    "BTC": 5.0,
    "WTI": 4.0,
}
# 점수 구간 -> 주식 비중 범위(%). (하한점수, 라벨, 주식 최소, 주식 최대) 내림차순 검사
ALLOCATION_BANDS = [
    (70, "우호", 50, 70),
    (55, "다소 우호", 40, 55),
    (45, "중립", 30, 45),
    (30, "다소 비우호", 15, 30),
    (0, "비우호", 0, 15),
]
EQUITY_CAP_PCT = 70  # PRD: 주식 상한 70%

# --- 뉴스 (Google News RSS) ---
# 주제 key -> (표시명, 검색어)
NEWS_TOPICS = {
    "us_macro": ("미국 매크로·연준", "연준 OR 미국 금리 OR 미국채 OR 미국 물가"),
    "japan": ("일본·BOJ·엔화", "일본은행 OR BOJ OR 엔화 OR 엔캐리"),
    "korea": ("한국 증시·수급", "코스피 외국인 OR 원달러 환율"),
    "bitcoin": ("비트코인", "비트코인"),
}
NEWS_PER_TOPIC = 5
NEWS_TIMEOUT_SEC = 15

# --- LLM ---
# claude_cli: Pro 구독 로그인(`claude -p`) 사용, 키 불필요 / anthropic_api: ANTHROPIC_API_KEY 필요
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "claude_cli")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
LLM_TIMEOUT_SEC = 180
LLM_RETRIES = 1  # 검증 실패/일시 오류 시 추가 시도 횟수
LLM_MAX_ADJUSTMENT = 10  # PRD: LLM 정성 보정 ±10점

# --- 스케줄 (KST, 거래일에만) ---
SCHEDULER_ENABLED = os.getenv("STOCKRECOM_SCHEDULER", "1") == "1"
SCHEDULE_TIMES = [(7, 30), (10, 30), (13, 30), (16, 30)]
TIMEZONE = "Asia/Seoul"
# 신호등: 점수 >= GREEN 초록, >= YELLOW 노랑, 그 미만 빨강 (PRD 3.3)
SIGNAL_GREEN = 60
SIGNAL_YELLOW = 40
