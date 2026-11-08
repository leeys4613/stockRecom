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
    # 미국 정치·정책 충격이 시장에 가장 먼저 나타나는 위험/불확실성 지표
    "VIX": ("VIX 공포지수", "^VIX"),
    "SPX": ("S&P 500", "^GSPC"),
    "DXY": ("달러인덱스", "DX-Y.NYB"),
}

# 데이터 신선도 기준(시간): 이보다 오래되면 stale
STALE_AFTER_HOURS = 6

# --- 점수 엔진 초기안 (⚠ 사람 확인 필요) ---
# 가중치: 양수 = 지표가 오르면 점수↑, 음수 = 오르면 점수↓. 절대값 합 = 1.0
SCORE_WEIGHTS = {
    "SOX": 0.22,
    "USDKRW": -0.20,
    "US10Y": -0.12,
    "USDJPY": 0.04,
    "USDCNH": -0.08,
    "BTC": 0.04,
    "WTI": -0.08,
    "VIX": -0.10,
    "SPX": 0.08,
    "DXY": -0.04,
}
# 지표 방향(고정): +1 = 지표가 오르면 점수↑, -1 = 오르면 점수↓. 사용자 조정은 크기만 허용한다
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
    "VIX": 20.0,
    "SPX": 2.0,
    "DXY": 0.7,
}
# 점수 구간 -> 주식 비중 범위(%). (하한점수, 라벨, 주식 최소, 주식 최대) 내림차순 검사
# 라벨은 화면에서 "매수 가능성 <라벨>"로 표시된다 (확률이 아니라 점수 구간)
ALLOCATION_BANDS = [
    (70, "높음", 50, 70),
    (55, "다소 높음", 40, 55),
    (45, "중립", 30, 45),
    (30, "다소 낮음", 15, 30),
    (0, "낮음", 0, 15),
]
EQUITY_CAP_PCT = 70  # PRD: 주식 상한 70%

# --- 뉴스 (Google News RSS) ---
# 주제 key -> (표시명, 검색어)
NEWS_TOPICS = {
    "us_macro": ("미국 매크로·연준", "연준 OR 미국 금리 OR 미국채 OR 미국 물가"),
    "japan": ("일본·BOJ·엔화", "일본은행 OR BOJ OR 엔화 OR 엔캐리"),
    "korea": ("한국 증시·수급", "코스피 외국인 OR 원달러 환율"),
    "bitcoin": ("비트코인", "비트코인"),
    # 정책 반영용(AI 가 -3~+3 단계로만 판정, 점수 반영폭은 상한이 있음)
    "us_policy": ("미국 정책·정치", "트럼프 OR 백악관 OR 관세 OR 무역협상 OR 반도체 수출규제 OR 대중 제재 OR 연준 인사 OR 셧다운 OR 중간선거"),
    "kr_policy": ("한국 증시 부양·자본시장 정책", "증시 부양 OR 밸류업 OR 상법 개정 OR 배당소득 분리과세 OR 공매도 OR 자본시장 활성화"),
}
# 구글 뉴스 RSS 는 최신순이 아니라 관련도순이라 오래된 기사가 섞인다 → 기간 제한(검색어 + 발행일 필터)
NEWS_DEFAULT_MAX_AGE_DAYS = 3
NEWS_MAX_AGE_DAYS = {"us_policy": 3, "kr_policy": 7}
NEWS_PER_TOPIC_OVERRIDE = {"us_policy": 8, "kr_policy": 8}
POLICY_TOPICS = {"us": "us_policy", "kr": "kr_policy"}
POLICY_LEVEL_MAX = 3                      # AI 판정 단계: -3(매우 부정) ~ +3(매우 긍정)
POLICY_CAPS_DEFAULT = {"us": 10.0, "kr": 6.0}  # 정책 반영 점수 상한(±점). 단계/3 * 상한
NEWS_PER_TOPIC = 5
NEWS_TIMEOUT_SEC = 15

# --- LLM ---
# claude_cli: Pro 구독 로그인(`claude -p`) 사용, 키 불필요 / anthropic_api: ANTHROPIC_API_KEY 필요
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "claude_cli")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
LLM_TIMEOUT_SEC = 180
LLM_RETRIES = 1  # 검증 실패/일시 오류 시 추가 시도 횟수
LLM_MAX_ADJUSTMENT = 5   # 정책 반영 항목이 따로 생겨 일반 정성 보정은 ±5점으로 축소 (이중 반영 방지)

# --- 스케줄 (KST, 거래일에만) ---
SCHEDULER_ENABLED = os.getenv("STOCKRECOM_SCHEDULER", "1") == "1"
SCHEDULE_TIMES = [(7, 30), (10, 30), (13, 30), (16, 30)]
TIMEZONE = "Asia/Seoul"
# 신호등: 점수 >= GREEN 초록, >= YELLOW 노랑, 그 미만 빨강 (PRD 3.3)
SIGNAL_GREEN = 60
SIGNAL_YELLOW = 40


# --- 증권사 컨센서스 / Top Picks (yfinance, 크롤링 없음) ---
# 네이버 증권·한경컨센서스는 robots.txt 가 전체 자동 수집을 금지(Disallow: /)하므로 사용하지 않는다.
REPORTS_DELAY_SEC = 0.6          # 종목 간 조회 간격 (예의상 요청 속도 제한)
WATCHLIST_MAX = 50
REPORTS_SCHEDULE = (16, 40)      # 거래일 장 마감 후 하루 1회 스냅샷
PICK_TOP_N = 5
PICK_MIN_ANALYSTS = 5            # 애널리스트 수가 이보다 적으면 후보 제외
PICK_STALE_DAYS = 3              # 스냅샷이 이보다 오래되면 후보 제외 (주말 감안)
PICK_MAX_UPSIDE = 1.0            # 목표가 괴리율이 +100% 를 넘으면 데이터 이상으로 보고 제외
PICK_TREND_MIN_DAYS = 5          # 목표가 추이는 이 일수 이상 지난 스냅샷과 비교
PICK_UPSIDE_SAT = 0.5            # 괴리율 +50% 에서 포화
PICK_TREND_SAT = 0.10            # 목표가 변화 +10% 에서 포화
PICK_WEIGHTS = {"upside": 0.40, "opinion": 0.35, "trend": 0.25}
