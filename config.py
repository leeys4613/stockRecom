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
    "USDCNH": ("USD/CNH", "CNH=X"),
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
