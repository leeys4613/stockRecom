"""증권사 컨센서스 수집기 (yfinance). 관심종목만 조회하고 일별 스냅샷으로 저장한다.

크롤링은 하지 않는다: 네이버 증권/한경컨센서스는 robots.txt 가 전체 자동 수집을 금지한다.
종목별로 ok/failed + fetched_at 을 기록한다 (실패를 숨기지 않는다).
"""
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import config

CODE_RE = re.compile(r"^[0-9A-Z]{6}$")
MARKETS = {"KS": "KOSPI", "KQ": "KOSDAQ"}
_lock = threading.Lock()


class WatchlistError(ValueError):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def symbol(code, market):
    return f"{code}.{market}"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None  # NaN/inf 제외


def yf_fetch(sym):
    """yfinance 에서 컨센서스를 받아 표준 dict 로 반환. 조회 결과가 없으면 예외."""
    import yfinance as yf

    t = yf.Ticker(sym)
    info = t.get_info() or {}
    price = _num(info.get("currentPrice")) or _num(info.get("regularMarketPrice"))
    if price is None:
        raise ValueError("조회 결과 없음(종목 코드/시장을 확인하세요)")
    out = dict(
        price=price, target_mean=_num(info.get("targetMeanPrice")),
        target_high=_num(info.get("targetHighPrice")), target_low=_num(info.get("targetLowPrice")),
        target_median=_num(info.get("targetMedianPrice")),
        rec_mean=_num(info.get("recommendationMean")), rec_key=info.get("recommendationKey"),
        n_analysts=int(info["numberOfAnalystOpinions"]) if info.get("numberOfAnalystOpinions") else None,
        strong_buy=None, buy=None, hold=None, sell=None, strong_sell=None,
    )
    try:  # 투자의견 분포(이번 달) — 없어도 컨센서스 자체는 유효
        df = t.recommendations
        if df is not None and len(df):
            row = df.iloc[0]
            for k, c in (("strong_buy", "strongBuy"), ("buy", "buy"), ("hold", "hold"),
                         ("sell", "sell"), ("strong_sell", "strongSell")):
                out[k] = int(row[c])
    except Exception:
        pass
    return out


def yf_industry(sym):
    """Yahoo 업종명(영문). 없으면 None. 섹터 매핑이 없는 종목의 소속 테마를 추정할 때만 쓴다."""
    import yfinance as yf

    return (yf.Ticker(sym).get_info() or {}).get("industry")


FIELDS = ("price", "target_mean", "target_high", "target_low", "target_median", "rec_mean", "rec_key",
          "n_analysts", "strong_buy", "buy", "hold", "sell", "strong_sell")


def collect_one(code, market, fetcher=None):
    fetched_at = now_iso()
    base = {k: None for k in FIELDS}
    try:
        data = (fetcher or yf_fetch)(symbol(code, market))
        return dict(base, **{k: data.get(k) for k in FIELDS}, code=code, status="ok", error=None,
                    fetched_at=fetched_at)
    except Exception as e:
        return dict(base, code=code, status="failed", error=f"{type(e).__name__}: {e}"[:300],
                    fetched_at=fetched_at)


def collect_all(items, fetcher=None, delay=None):
    delay = config.REPORTS_DELAY_SEC if delay is None else delay
    out = []
    for i, it in enumerate(items):
        if i and delay:
            time.sleep(delay)
        out.append(collect_one(it["code"], it["market"], fetcher))
    return out


def save(conn, snaps):
    conn.executemany(
        "INSERT INTO consensus_snapshots (code,status,error,fetched_at,price,target_mean,target_high,"
        "target_low,target_median,rec_mean,rec_key,n_analysts,strong_buy,buy,hold,sell,strong_sell) "
        "VALUES (:code,:status,:error,:fetched_at,:price,:target_mean,:target_high,:target_low,"
        ":target_median,:rec_mean,:rec_key,:n_analysts,:strong_buy,:buy,:hold,:sell,:strong_sell)", snaps)


# ---- 관심종목 관리 ----
def watchlist(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM watchlist ORDER BY added_at, code")]


def add_stock(conn, code, market, name, fetcher=None):
    """검증 후 추가. 먼저 실제로 조회해 보고(첫 스냅샷 저장) 실패하면 추가하지 않는다.

    market 이 없으면 KOSPI → KOSDAQ 순으로 조회해 성공한 시장을 쓴다.
    """
    code = (code or "").strip().upper()
    market = (market or "").strip().upper() or None
    name = " ".join((name or "").split())[:20]
    if not CODE_RE.match(code):
        raise WatchlistError("종목 코드는 영문 대문자/숫자 6자리여야 합니다 (예: 005930)")
    if market is not None and market not in MARKETS:
        raise WatchlistError("시장은 KOSPI 또는 KOSDAQ 이어야 합니다")
    if conn.execute("SELECT 1 FROM watchlist WHERE code=?", (code,)).fetchone():
        raise WatchlistError("이미 관심종목에 있습니다")
    if conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] >= config.WATCHLIST_MAX:
        raise WatchlistError(f"관심종목은 최대 {config.WATCHLIST_MAX}개입니다")
    snap = None
    for m in ([market] if market else list(MARKETS)):
        snap = collect_one(code, m, fetcher)
        if snap["status"] == "ok":
            market = m
            break
    if snap["status"] != "ok":
        raise WatchlistError(f"조회 실패: {snap['error']}")
    conn.execute("INSERT INTO watchlist (code,market,name,added_at) VALUES (?,?,?,?)",
                 (code, market, name or code, now_iso()))
    save(conn, [snap])
    return code


def resolve(conn, q, code=None, market=None, name=None):
    """통합 검색어(종목명 또는 코드) -> (code, market, name). 자동완성에서 고른 경우 code/market 이 함께 온다.

    유일하게 특정되지 않으면 후보를 안내하는 WatchlistError 를 낸다.
    """
    import stockmaster

    if code and market:
        return code, market, name
    q = " ".join((q or "").split())
    if not q:
        raise WatchlistError("종목명이나 종목코드를 입력하세요")
    cand = stockmaster.search(conn, q, limit=8)
    exact = [c for c in cand if c["code"] == q.upper() or stockmaster.norm(c["name"]) == stockmaster.norm(q)]
    pick = exact[0] if len(exact) == 1 else cand[0] if len(cand) == 1 else None
    if pick:
        return pick["code"], pick["market"], pick["name"]
    if not cand:
        if CODE_RE.match(q.upper()):  # 종목 목록에 없는 코드: 시장을 모르니 KOSPI/KOSDAQ 모두 시도
            return q.upper(), None, name or None
        raise WatchlistError(f"'{q}' 검색 결과가 없습니다. 종목 목록에 없을 수 있습니다 (전체 목록 업로드 또는 종목코드 직접 입력)")
    raise WatchlistError("여러 종목이 검색됩니다. 입력창 아래 후보에서 선택하세요: "
                         + ", ".join(f"{c['name']}({c['code']})" for c in cand))


def remove_stock(conn, code):
    conn.execute("DELETE FROM watchlist WHERE code=?", ((code or "").strip().upper(),))


def collect_watchlist(conn_factory, fetcher=None, delay=None):
    """관심종목 전체 스냅샷. 이미 실행 중이면 None. 반환: 저장한 종목 수."""
    if not _lock.acquire(blocking=False):
        return None
    try:
        with conn_factory() as conn:
            items = watchlist(conn)
        snaps = collect_all(items, fetcher, delay)
        with conn_factory() as conn:
            save(conn, snaps)
        return len(snaps)
    finally:
        _lock.release()


def stock_rows(conn):
    """종목별: 최신 시도, 최신 성공, 추이 비교용 과거 성공 스냅샷."""
    rows = []
    for w in watchlist(conn):
        code = w["code"]
        last = conn.execute("SELECT * FROM consensus_snapshots WHERE code=? ORDER BY fetched_at DESC, id DESC LIMIT 1",
                            (code,)).fetchone()
        ok = conn.execute("SELECT * FROM consensus_snapshots WHERE code=? AND status='ok' "
                          "ORDER BY fetched_at DESC, id DESC LIMIT 1", (code,)).fetchone()
        base = None
        if ok:
            cutoff = (datetime.fromisoformat(ok["fetched_at"])
                      - timedelta(days=config.PICK_TREND_MIN_DAYS)).isoformat(timespec="seconds")
            base = conn.execute(
                "SELECT * FROM consensus_snapshots WHERE code=? AND status='ok' AND target_mean IS NOT NULL "
                "AND fetched_at<=? ORDER BY fetched_at DESC, id DESC LIMIT 1", (code, cutoff)).fetchone()
        rows.append(dict(stock=w, last=dict(last) if last else None, ok=dict(ok) if ok else None,
                         base=dict(base) if base else None))
    return rows
