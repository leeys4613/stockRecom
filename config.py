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
    # 파생 지표(symbol=None): 전일 변동률이 아니라 값 자체(추세·수준)를 점수에 쓴다. 하루 변동률만으로는
    # 급락 직후 반등일에 점수가 과대평가되는 문제를 보완한다
    "KOSPI10D": ("KOSPI 10거래일 변화", None),
    "KOSPIDD": ("KOSPI 10일 고점 대비", None),
    "VIXAVG": ("VIX 10일 평균 (20=중립)", None),
    # AI 투자 사이클(데이터센터 건설 속도) 시장 반응: AI 대형주 8종목(빅테크·반도체·데이터센터) 평균
    "AI1D": ("AI 대형주 1일 변화", None),
    "AI10D": ("AI 대형주 10거래일 변화", None),
    "AIDD": ("AI 대형주 60일 고점 대비", None),
    # 메모리 가격 기대의 시장 프록시: 메모리주(Micron·SK하이닉스·Nanya·Winbond·SanDisk·Kioxia·메모리 ETF) 평균
    "MEM10D": ("메모리주 10거래일 변화", None),
    # 데이터센터 건설 속도의 간접 지표: 전력·전기 인프라주(변압기·전력기기·발전·송배전). 가중치 0 = 점수에는 반영하지 않는 참고 지표
    "POWER10D": ("전력 인프라 10거래일 변화 (참고)", None),
}
DERIVED_UNITS = {"KOSPI10D": "%", "KOSPIDD": "%", "VIXAVG": "", "AI1D": "%", "AI10D": "%", "AIDD": "%", "MEM10D": "%", "POWER10D": "%"}
SCORE_CENTER = {"VIXAVG": 20.0}      # 점수 입력 = 값 - 중립값 (VIX 10일 평균이 20 이면 0점)
KOSPI_SYMBOL = "^KS11"
TREND_DAYS = 10   # 추세 지표의 관측 기간(거래일): KOSPI 변화·고점 대비·VIX 평균

# 데이터 신선도 기준(시간): 이보다 오래되면 stale
STALE_AFTER_HOURS = 6

# --- 점수 엔진 초기안 (⚠ 사람 확인 필요) ---
# 가중치: 양수 = 지표가 오르면 점수↑, 음수 = 오르면 점수↓. 절대값 합 = 1.0
SCORE_WEIGHTS = {
    "SOX": 0.1155,
    "USDKRW": -0.105,
    "US10Y": -0.063,
    "USDJPY": 0.021,
    "USDCNH": -0.042,
    "BTC": 0.021,
    "WTI": -0.042,
    "VIX": -0.0525,      # VIX 전일 변화율
    "SPX": 0.042,
    "DXY": -0.021,
    "VIXAVG": -0.07,     # VIX 10일 평균 수준: 높을수록 위험 국면
    "KOSPI10D": 0.056,   # KOSPI 최근 10거래일 변화
    "KOSPIDD": 0.049,    # KOSPI 10일 고점 대비 낙폭
    # AI 투자 사이클(데이터센터 건설 속도·메모리 가격)이 최근 주가를 크게 좌우한다는 판단으로 비중 30% (사용자 요청)
    "AI1D": 0.06,        # AI 대형주 바스켓 1일 평균 변화
    "AI10D": 0.08,       # AI 대형주 바스켓 10거래일 평균 변화
    "AIDD": 0.09,        # AI 대형주 바스켓 60일 고점 대비 평균 낙폭
    "MEM10D": 0.07,      # 메모리주 10거래일 평균 변화 (메모리 가격 기대 프록시)
    "POWER10D": 0.0,     # 전력 인프라(참고용). 가중치 0 이면 점수에 반영하지 않는다 — 리밸런싱 때 설정 화면에서 올린다
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
    "VIXAVG": 10.0,   # 중립(20) 대비 ±10pt 이면 포화
    "KOSPI10D": 8.0,   # 10거래일 ±8%
    "KOSPIDD": 10.0,   # 10일 고점 대비 -10%
    "AI1D": 3.0,       # 바스켓 평균 하루 ±3%
    "AI10D": 8.0,      # 10거래일 ±8%
    "AIDD": 30.0,      # 60일 고점 대비 -30%
    "MEM10D": 12.0,    # 메모리주는 변동이 커서 10거래일 ±12% 에서 포화
    "POWER10D": 10.0,  # 전력 인프라 10거래일 ±10% (참고용이라 가중치를 올릴 때만 의미)
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
    # AI 투자 사이클(데이터센터 건설 속도): 영문 기사. AI 가 -3~+3 단계로 판정
    "ai_capex": ("AI 설비투자·데이터센터 (영문)", '"AI bubble" OR "data center" OR datacenter OR "AI capex" OR hyperscaler OR "AI spending"'),
    "ai_stocks": ("AI 인프라 종목 (영문)", "CoreWeave OR IREN OR Micron OR TSMC OR Nvidia OR \"SK hynix\" AI stock"),
    # 메모리 가격(DRAM·HBM·NAND) 방향: 헤드라인에 가격·매출 수치가 실린다. 가격 상승=긍정, 하락·공급과잉=부정
    # 데이터센터 건설 속도의 제약(전력·변압기·계통 연결·지역 규제) — 'AI 시장 반응' 판정의 근거로 쓴다
    "ai_power_en": ("데이터센터 전력·그리드 (영문)", '"data center" power grid OR electricity demand OR transformer shortage OR "interconnection queue" OR utility AI'),
    "ai_power_kr": ("데이터센터 전력·전력기기 (한국어)", "변압기 OR 전력기기 OR 데이터센터 전력 OR 송전 OR 전력망"),
    "mem_kr": ("메모리 가격 (한국어)", "D램 가격 OR 고정거래가격 OR HBM 가격 OR 낸드 가격 OR 메모리 가격"),
    "mem_en": ("메모리 가격 (영문)", "DRAM prices OR \"DRAM contract price\" OR HBM prices OR NAND prices OR TrendForce memory"),
}
NEWS_LOCALE = {"ai_capex": "hl=en-US&gl=US&ceid=US:en", "ai_stocks": "hl=en-US&gl=US&ceid=US:en",
               "ai_power_en": "hl=en-US&gl=US&ceid=US:en", "mem_en": "hl=en-US&gl=US&ceid=US:en"}
# 구글 뉴스 RSS 는 최신순이 아니라 관련도순이라 오래된 기사가 섞인다 → 기간 제한(검색어 + 발행일 필터)
NEWS_DEFAULT_MAX_AGE_DAYS = 3
NEWS_MAX_AGE_DAYS = {"us_policy": 3, "kr_policy": 7, "ai_capex": 3, "ai_stocks": 3, "ai_power_en": 3, "ai_power_kr": 3, "mem_kr": 5, "mem_en": 5}
NEWS_PER_TOPIC_OVERRIDE = {"us_policy": 8, "kr_policy": 8, "ai_capex": 8, "ai_stocks": 8, "ai_power_en": 6, "ai_power_kr": 6, "mem_kr": 8, "mem_en": 8}
POLICY_TOPICS = {"us": ["us_policy"], "kr": ["kr_policy"], "ai": ["ai_capex", "ai_stocks", "ai_power_en", "ai_power_kr"], "mem": ["mem_kr", "mem_en"]}
POLICY_LEVEL_MAX = 3                      # AI 판정 단계: -3(매우 부정) ~ +3(매우 긍정)
POLICY_CAPS_DEFAULT = {"us": 10.0, "kr": 6.0, "ai": 6.0, "mem": 6.0}  # 정책 반영 점수 상한(±점). 단계/3 * 상한
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


# --- AI 투자 사이클 트래커 (데이터센터 건설 속도에 대한 시장 반응) ---
# 티커 -> (이름, 종목군). 수요=빅테크(설비투자 주체), 공급망=반도체, 인프라=데이터센터/AI 클라우드
AI_TICKERS = {
    "AMZN": ("Amazon", "bigtech"), "META": ("Meta", "bigtech"), "GOOGL": ("Alphabet", "bigtech"),
    "MSFT": ("Microsoft", "bigtech"),
    "MU": ("Micron", "semis"), "TSM": ("TSMC", "semis"), "000660.KS": ("SK하이닉스", "semis"),
    "IREN": ("IREN", "datacenter"), "CRWV": ("CoreWeave", "datacenter"),
    # 메모리 가격에 민감한 프록시(바스켓에는 넣지 않고 메모리 지표/트래커에만 사용)
    "2408.TW": ("Nanya(DRAM)", "memory"), "2344.TW": ("Winbond", "memory"),
    "SNDK": ("SanDisk(NAND)", "memory"), "285A.T": ("Kioxia(NAND)", "memory"), "DRAM": ("메모리 ETF", "memory"),
    # 데이터센터 건설 속도의 간접 지표(전력·전기 인프라): 미국(전력·냉각·발전·송배전) + 한국 전력기기(변압기 등)
    "VRT": ("Vertiv", "power"), "ETN": ("Eaton", "power"), "GEV": ("GE Vernova", "power"),
    "CEG": ("Constellation", "power"), "VST": ("Vistra", "power"), "PWR": ("Quanta", "power"),
    "267260.KS": ("HD현대일렉트릭", "power"), "010120.KS": ("LS ELECTRIC", "power"), "298040.KS": ("효성중공업", "power"),
    "034020.KS": ("두산에너빌리티", "power"), "103590.KS": ("일진전기", "power"),
}
AI_BASKET_GROUPS = ("bigtech", "semis", "datacenter")
MEMORY_PROXY = ["MU", "000660.KS", "2408.TW", "2344.TW", "SNDK", "285A.T", "DRAM"]
AI_GROUP_NAMES = {"bigtech": "빅테크(수요)", "semis": "반도체(공급망)", "datacenter": "데이터센터(인프라)", "memory": "메모리 프록시", "power": "전력·전기 인프라"}
AI_DD_WINDOW = 60                  # 고점 대비 낙폭 관측 기간(거래일)
AI_MIN_TICKERS = 6                 # 바스켓 지표는 최소 이 종목 수가 성공해야 계산 (메모리 프록시는 MEM_MIN_TICKERS)
MEM_MIN_TICKERS = 4
POWER_MIN_TICKERS = 6
CAPEX_TICKERS = ["MSFT", "AMZN", "GOOGL", "META"]
CAPEX_REFRESH_HOURS = 20
CAPEX_QOQ_STRONG = 5.0              # 분기 대비 이 이상 늘면 '확대 지속', 0~이 값이면 '증가율 둔화', 음수면 '감소'
CAPEX_QOQ_DECEL_PP = 3.0           # 직전 QoQ 보다 이만큼(%p) 낮아지면 '증가율 둔화' 추가 표시
CAPEX_ANOMALY_QOQ = 1.8            # 직전 분기 대비 이 배수 이상이면 데이터 이상 의심 표시
