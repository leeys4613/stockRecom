"""종목 마스터(코드·이름·시장) 검색과 업로드 파싱.

종목 목록의 출처:
  - 기본 내장(data/seed_stocks.csv): 주요 40종목. 코드는 Yahoo 조회로 하나씩 검증했다.
  - 사용자 업로드: KRX/KIND 에서 사용자가 직접 내려받은 상장법인 목록 파일(xlsx / HTML 형식 xls / csv).
KRX 사이트를 자동으로 수집하지 않는다.
"""
import csv
import io
import re
from datetime import datetime, timezone

import config

MARKETS = {"KS": "KOSPI", "KQ": "KOSDAQ"}
CODE_RE = re.compile(r"^[0-9A-Z]{6}$")
SEED_PATH = config.BASE_DIR / "data" / "seed_stocks.csv"
MIN_UPLOAD_ROWS = 100  # 전체 상장 목록으로 보기엔 너무 적으면 거부 (기본 목록을 실수로 덮어쓰는 것 방지)
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"


class StockMasterError(ValueError):
    pass


def norm(s):
    """검색용 정규화: 대소문자·공백 무시."""
    return "".join(str(s or "").split()).casefold()


def choseong(s):
    """'삼성전자' -> 'ㅅㅅㅈㅈ'. 한글 이외 문자는 소문자로 유지, 공백 제거."""
    out = []
    for ch in str(s or ""):
        if "가" <= ch <= "힣":
            out.append(_CHO[(ord(ch) - 0xAC00) // 588])
        elif not ch.isspace():
            out.append(ch.casefold())
    return "".join(out)


def _like(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------- 저장 ----------
def _insert(conn, rows, source):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.executemany(
        "INSERT OR REPLACE INTO stock_master (code,name,market,name_norm,choseong,source,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        [(r["code"], r["name"], r["market"], norm(r["name"]), choseong(r["name"]), source, now) for r in rows])


def seed_if_empty(conn):
    """기본 목록 적재. 비어 있으면 전부, 기본 목록만 쓰는 중이면 새로 추가된 종목만 보충한다(업로드한 전체 목록은 건드리지 않음)."""
    if not SEED_PATH.exists():
        return
    have = {r[0]: r[1] for r in conn.execute("SELECT code, source FROM stock_master")}
    if have and any(s != "seed" for s in have.values()):
        return
    with open(SEED_PATH, encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if CODE_RE.match(r["code"]) and r["market"] in MARKETS and r["code"] not in have]
    if rows:
        _insert(conn, rows, "seed")


def replace_all(conn, rows, source="upload"):
    if len(rows) < MIN_UPLOAD_ROWS:
        raise StockMasterError(
            f"종목이 {len(rows)}개뿐입니다. 전체 상장법인 목록 파일이 맞는지 확인하세요 (최소 {MIN_UPLOAD_ROWS}개)")
    conn.execute("DELETE FROM stock_master")
    _insert(conn, rows, source)
    return len(rows)


def info(conn):
    n = conn.execute("SELECT COUNT(*) FROM stock_master").fetchone()[0]
    srcs = [r[0] for r in conn.execute("SELECT DISTINCT source FROM stock_master")]
    last = conn.execute("SELECT MAX(updated_at) FROM stock_master").fetchone()[0]
    return dict(count=n, source=srcs[0] if len(srcs) == 1 else "mixed", updated_at=last)


def get(conn, code):
    r = conn.execute("SELECT code,name,market FROM stock_master WHERE code=?", (code,)).fetchone()
    return dict(r) if r else None


# ---------- 검색 ----------
def search(conn, q, limit=8):
    """코드/이름/초성 통합 검색. 순위: 코드 일치 > 코드 접두 > 이름 일치 > 이름 접두 > 이름 포함 > 초성."""
    q = (q or "").strip()[:30]
    qn = norm(q)
    if not qn:
        return []
    qu = qn.upper()
    is_cho = all(ch in _CHO for ch in qn)
    pat_name, pat_code = f"%{_like(qn)}%", f"{_like(qu)}%"
    rows = conn.execute(
        "SELECT code,name,market,name_norm,choseong FROM stock_master "
        "WHERE name_norm LIKE ? ESCAPE '\\' OR code LIKE ? ESCAPE '\\' OR choseong LIKE ? ESCAPE '\\' LIMIT 500",
        (pat_name, pat_code, f"%{_like(qn)}%" if is_cho else "\x00")).fetchall()
    scored = []
    for r in rows:
        if r["code"] == qu:
            rank = 0
        elif r["code"].startswith(qu):
            rank = 1
        elif r["name_norm"] == qn:
            rank = 2
        elif r["name_norm"].startswith(qn):
            rank = 3
        elif qn in r["name_norm"]:
            rank = 4
        elif is_cho and r["choseong"].startswith(qn):
            rank = 5
        elif is_cho and qn in r["choseong"]:
            rank = 6
        else:
            continue
        scored.append((rank, len(r["name"]), r["name"], dict(code=r["code"], name=r["name"], market=r["market"])))
    scored.sort(key=lambda t: t[:3])
    return [t[3] for t in scored[:limit]]


# ---------- 업로드 파싱 ----------
_NAME_COLS = {"회사명", "종목명", "한글종목약명", "한글종목명", "종목약명", "한글약명", "name"}
_CODE_COLS = {"종목코드", "단축코드", "code", "종목번호", "ticker", "티커"}
_MARKET_COLS = {"시장구분", "시장", "market", "시장명"}


def _decode(raw):
    for enc in ("utf-8-sig", "utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise StockMasterError("파일 인코딩을 읽을 수 없습니다 (UTF-8 또는 CP949/EUC-KR 이어야 함)")


def _market_of(v):
    s = norm(v)
    if not s:
        return None
    if "코스닥" in s or "kosdaq" in s or s in ("kq", "ksq"):
        return "KQ"
    if s.startswith("유가") or "코스피" in s or "kospi" in s or s in ("ks", "stk"):   # KIND 는 "유가"로 줄여 쓴다
        return "KS"
    return None  # 코넥스·기타는 Yahoo(.KS/.KQ)로 조회할 수 없어 제외


def _code_of(v):
    if v is None or (isinstance(v, float) and v != v):
        return None
    s = str(v).strip().upper()
    if s.endswith(".0"):
        s = s[:-2]
    if s.isdigit():
        s = s.zfill(6)
    return s if CODE_RE.match(s) else None


def parse_file(filename, raw):
    """파일 -> [{'code','name','market'}]. 형식: xlsx / HTML 형식 xls(KIND) / csv. 컬럼은 이름으로 찾는다."""
    import pandas as pd

    if len(raw) > MAX_UPLOAD_BYTES:
        raise StockMasterError("파일이 너무 큽니다 (최대 8MB)")
    name = (filename or "").lower()
    try:
        if name.endswith(".xlsx") or raw[:2] == b"PK":
            df = pd.read_excel(io.BytesIO(raw), dtype=str)
        else:
            text = _decode(raw)
            if text.lstrip()[:1] == "<":
                tables = pd.read_html(io.StringIO(text))
                if not tables:
                    raise StockMasterError("파일에서 표를 찾지 못했습니다")
                df = tables[0].astype(str)
            else:
                df = pd.read_csv(io.StringIO(text), dtype=str, sep=None, engine="python")
    except StockMasterError:
        raise
    except Exception as e:
        raise StockMasterError(f"파일을 읽지 못했습니다 ({type(e).__name__})") from e

    cols = {norm(c): c for c in df.columns}
    pick = lambda names: next((cols[n] for n in (norm(x) for x in names) if n in cols), None)  # noqa: E731
    c_name, c_code, c_mkt = pick(_NAME_COLS), pick(_CODE_COLS), pick(_MARKET_COLS)
    missing = [label for label, c in (("회사명/종목명", c_name), ("종목코드", c_code), ("시장구분", c_mkt)) if c is None]
    if missing:
        raise StockMasterError(f"필요한 컬럼이 없습니다: {', '.join(missing)} (찾은 컬럼: {', '.join(map(str, df.columns))[:120]})")

    out, seen = [], set()
    for _, r in df.iterrows():
        code, market = _code_of(r[c_code]), _market_of(r[c_mkt])
        nm = " ".join(str(r[c_name]).split())[:40] if r[c_name] == r[c_name] else ""
        if not code or not market or not nm or nm.lower() == "nan" or code in seen:
            continue
        seen.add(code)
        out.append(dict(code=code, name=nm, market=market))
    return out
