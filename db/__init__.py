import sqlite3
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS macro_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','failed')),
    value REAL,
    prev_value REAL,
    change_pct REAL,
    error TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_macro_key_time ON macro_snapshots(key, fetched_at);

CREATE TABLE IF NOT EXISTS news_fetches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','failed')),
    error TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_fetch_topic ON news_fetches(topic, fetched_at);

CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetch_id INTEGER NOT NULL REFERENCES news_fetches(id),
    title TEXT NOT NULL,
    link TEXT,
    source TEXT,
    published TEXT
);

-- 점수 가중치 세트: 불변 버전. 저장하면 새 행이 생기고 가장 큰 id 가 현재 적용 버전이다.
CREATE TABLE IF NOT EXISTS weight_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    label TEXT NOT NULL,
    weights TEXT NOT NULL,      -- JSON {key: 부호 있는 정규화 가중치, 절대값 합 1}
    saturation TEXT NOT NULL,   -- JSON {key: 포화 변동률(%)}
    policy TEXT                 -- JSON {"us": 상한, "kr": 상한}. NULL = 정책 반영 없음(구 버전)
);

-- 구간별 주식 비중 규칙: 불변 버전. 가장 큰 id 가 현재 적용 버전. (점수 계산과 무관하므로 가중치와 별도 버전)
CREATE TABLE IF NOT EXISTS allocation_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    label TEXT NOT NULL,
    bands TEXT NOT NULL         -- JSON [[점수하한, 라벨, 주식최소%, 주식최대%], ...] 점수 하한 내림차순
);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    llm_status TEXT NOT NULL CHECK (llm_status IN ('ok','failed','skipped')),
    provider TEXT,
    base_score REAL,
    adjustment INTEGER NOT NULL DEFAULT 0,
    final_score REAL,
    coverage REAL,
    missing TEXT,        -- JSON list
    summary TEXT,        -- JSON list (3 lines)
    risk_comment TEXT,
    error TEXT,
    inputs TEXT,         -- JSON: LLM에 준 입력 스냅샷 (사후 검증용)
    weight_set_id INTEGER REFERENCES weight_sets(id),
    allocation_set_id INTEGER REFERENCES allocation_sets(id),
    policy_us REAL,      -- 미국 정책 반영 점수
    policy_kr REAL,      -- 한국 증시 부양정책 반영 점수
    policy TEXT          -- JSON: 단계/근거 헤드라인
);

-- AI 투자 사이클 트래커: 빅테크/반도체/데이터센터/메모리 프록시 종목의 가격 스냅샷
CREATE TABLE IF NOT EXISTS ai_tracker_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    grp TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','failed')),
    error TEXT,
    fetched_at TEXT NOT NULL,
    price REAL, chg1d REAL, chg10d REAL, dd60 REAL
);
CREATE INDEX IF NOT EXISTS idx_ai_ticker_time ON ai_tracker_snapshots(ticker, fetched_at);

-- 빅테크 분기 설비투자(CAPEX, 양수 USD). 데이터센터 건설 속도의 수요 측 지표
CREATE TABLE IF NOT EXISTS capex_quarters (
    ticker TEXT NOT NULL,
    period_end TEXT NOT NULL,
    capex REAL NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (ticker, period_end)
);

-- 관심종목 AI 적합도: 대중심리 점수 + 주도섹터 점수 → 추천 점수 (현재 분석 기준, 종목별 이력)
CREATE TABLE IF NOT EXISTS stock_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    code TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','failed')),
    error TEXT,
    analysis_id INTEGER,
    crowd_effect INTEGER, crowd_score REAL, crowd_evidence TEXT, crowd_note TEXT,
    sector_effect INTEGER, sector_score REAL, sector_groups TEXT, sector_note TEXT,
    score REAL,
    comment TEXT,
    metrics TEXT     -- JSON: price, chg1d, chg10d, dd60, headlines 수 등 (판정 당시 입력 요약)
);
CREATE INDEX IF NOT EXISTS idx_stock_views_code ON stock_views(code, id);

-- 종목 마스터: 검색(자동완성)용 코드/이름/시장. 기본 내장 40개 + 사용자가 올린 상장법인 목록
CREATE TABLE IF NOT EXISTS stock_master (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    market TEXT NOT NULL CHECK (market IN ('KS','KQ')),
    name_norm TEXT NOT NULL,
    choseong TEXT NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 관심종목 (Top Picks 후보 범위). 사용자가 관리한다.
CREATE TABLE IF NOT EXISTS watchlist (
    code TEXT PRIMARY KEY,
    market TEXT NOT NULL CHECK (market IN ('KS','KQ')),
    name TEXT NOT NULL,
    added_at TEXT NOT NULL
);

-- 증권사 컨센서스 일별 스냅샷 (목표가 추이를 직접 계산하기 위해 계속 쌓는다)
CREATE TABLE IF NOT EXISTS consensus_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','failed')),
    error TEXT,
    fetched_at TEXT NOT NULL,
    price REAL, target_mean REAL, target_high REAL, target_low REAL, target_median REAL,
    rec_mean REAL, rec_key TEXT, n_analysts INTEGER,
    strong_buy INTEGER, buy INTEGER, hold INTEGER, sell INTEGER, strong_sell INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cons_code_time ON consensus_snapshots(code, fetched_at);

-- 예측 사후 결과: 기준 종가(분석 시점에 알려진 마지막 KOSPI 종가) 대비 1/5 거래일 후 수익률
CREATE TABLE IF NOT EXISTS outcomes (
    analysis_id INTEGER PRIMARY KEY REFERENCES analyses(id),
    ref_date TEXT NOT NULL,
    ref_close REAL NOT NULL,
    d1_date TEXT, d1_close REAL, d1_ret REAL,
    d5_date TEXT, d5_close REAL, d5_ret REAL,
    updated_at TEXT NOT NULL
);
"""


def connect(path=None):
    conn = sqlite3.connect(str(path or config.DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn):
    """기존 DB 보강: analyses.weight_set_id 컬럼 추가, v1(초기안) 시드, 기존 분석은 v1 로 표시."""
    import json
    from datetime import datetime, timezone

    ws_cols = [r["name"] for r in conn.execute("PRAGMA table_info(weight_sets)")]
    if "policy" not in ws_cols:
        conn.execute("ALTER TABLE weight_sets ADD COLUMN policy TEXT")
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(analyses)")]
    for col, typ in (("policy_us", "REAL"), ("policy_kr", "REAL"), ("policy", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE analyses ADD COLUMN {col} {typ}")
    if "weight_set_id" not in cols:
        conn.execute("ALTER TABLE analyses ADD COLUMN weight_set_id INTEGER REFERENCES weight_sets(id)")
    if conn.execute("SELECT COUNT(*) FROM weight_sets").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO weight_sets (created_at,label,weights,saturation,policy) VALUES (?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), "v1 초기안",
             json.dumps(config.SCORE_WEIGHTS), json.dumps(config.SCORE_SATURATION_PCT),
             json.dumps(config.POLICY_CAPS_DEFAULT)))
        # 이 시점까지 쌓인 분석은 모두 초기안으로 계산된 것이다
        conn.execute("UPDATE analyses SET weight_set_id=1 WHERE weight_set_id IS NULL")
    # 정책 반영이 도입되기 전의 DB(어떤 가중치 세트에도 정책 상한이 없음): 새 지표/정책 반영이 포함된 새 버전을 추가한다.
    # 기존 버전(v1)은 그대로 두므로 과거 분석과 성과 비교가 섞이지 않는다.
    if conn.execute("SELECT COUNT(*) FROM weight_sets WHERE policy IS NOT NULL").fetchone()[0] == 0:
        nxt = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM weight_sets").fetchone()[0]
        conn.execute(
            "INSERT INTO weight_sets (created_at,label,weights,saturation,policy) VALUES (?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), f"v{nxt} 위험지표·정책 반영",
             json.dumps(config.SCORE_WEIGHTS), json.dumps(config.SCORE_SATURATION_PCT),
             json.dumps(config.POLICY_CAPS_DEFAULT)))
    # 새 지표가 추가됐는데 현재 적용 중인 가중치 세트에 없으면: 기존 상대 비중을 유지한 채 새 지표를 기본 비중으로 넣은
    # 새 버전을 추가한다 (사용자가 조정한 비중을 덮어쓰지 않고, 과거 분석/성과 비교는 이전 버전으로 남는다).
    latest = conn.execute("SELECT * FROM weight_sets ORDER BY id DESC LIMIT 1").fetchone()
    lw, lsat = json.loads(latest["weights"]), json.loads(latest["saturation"])
    # 가중치 0 인 새 지표(참고용)는 새 버전을 만들지 않는다: 점수가 같아서 성과 표본을 쪼갤 이유가 없고, 없는 키는 점수에서 무시된다
    missing_keys = [k for k in config.SCORE_WEIGHTS if k not in lw and config.SCORE_WEIGHTS[k] != 0]
    if missing_keys:
        new_share = sum(abs(config.SCORE_WEIGHTS[k]) for k in missing_keys)
        merged = {k: round(w * (1 - new_share), 6) for k, w in lw.items() if k in config.SCORE_WEIGHTS}
        merged.update({k: config.SCORE_WEIGHTS[k] for k in missing_keys})
        # 정책 상한: 이어받되, 새로 생긴 반응 영역(ai, mem 등)은 기본 상한을 채운다 (정책 반영이 없던 구 DB 는 NULL 유지)
        if latest["policy"]:
            pol = json.loads(latest["policy"])
            for k, v in config.POLICY_CAPS_DEFAULT.items():
                pol.setdefault(k, v)
            merged_policy = json.dumps(pol)
        else:
            merged_policy = None
        sat = {**config.SCORE_SATURATION_PCT, **{k: v for k, v in lsat.items() if k in config.SCORE_WEIGHTS}}
        nxt = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM weight_sets").fetchone()[0]
        conn.execute(
            "INSERT INTO weight_sets (created_at,label,weights,saturation,policy) VALUES (?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), f"v{nxt} 지표 확장 (+{len(missing_keys)})",
             json.dumps(merged), json.dumps(sat), merged_policy))
    if "allocation_set_id" not in cols:
        conn.execute("ALTER TABLE analyses ADD COLUMN allocation_set_id INTEGER REFERENCES allocation_sets(id)")
    if conn.execute("SELECT COUNT(*) FROM allocation_sets").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO allocation_sets (created_at,label,bands) VALUES (?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), "v1 초기안",
             json.dumps([list(b) for b in config.ALLOCATION_BANDS], ensure_ascii=False)))
        conn.execute("UPDATE analyses SET allocation_set_id=1 WHERE allocation_set_id IS NULL")
    import stockmaster

    stockmaster.seed_if_empty(conn)


def init_db(path=None):
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def session(path=None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
