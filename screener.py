"""지수 대비 강세 종목 스크리너: 최근 3~5거래일에 소속 지수(코스피→KOSPI, 코스닥→KOSDAQ)보다 더 오른 종목을 찾는다.

과거 실적을 걸러 보여 줄 뿐 앞으로의 초과수익을 예측하지 않는다. 데이터는 yfinance(일봉)와 시가총액(fast_info)이며
종목 목록은 사용자가 올린 stock_master 를 쓴다. 조회에 실패한 종목은 숨기지 않고 개수를 기록한다.
"""
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import config

log = logging.getLogger("screener")
_lock = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def raise_fd_limit(target=4096):
    """launchd 서버의 열린 파일 한도(기본 256)는 전체 종목 조회 중 소켓이 쌓이면 바닥난다. 가능하면 소프트 한도를 올린다."""
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except Exception:
        pass


# ---- 데이터 조회 (네트워크; 테스트에서 교체) ----
def yf_chunk(symbols):
    """{symbol: [(date, close, volume)] 오름차순}. 데이터가 없는 종목은 빠진다."""
    import yfinance as yf

    df = yf.download(symbols, period="2mo", interval="1d", group_by="ticker", threads=4, progress=False,
                     auto_adjust=False)
    out = {}
    top = set(df.columns.get_level_values(0))
    for s in symbols:
        if s not in top:
            continue
        sub = df[s][["Close", "Volume"]].dropna()
        if len(sub):
            out[s] = [(d.strftime("%Y-%m-%d"), float(c), float(v)) for d, c, v in zip(sub.index, sub["Close"], sub["Volume"])]
    return out


def yf_index(symbol):
    import yfinance as yf

    s = yf.Ticker(symbol).history(period="2mo", interval="1d")["Close"].dropna()
    return [(d.strftime("%Y-%m-%d"), float(c)) for d, c in zip(s.index, s)]


def yf_mcap(symbol):
    import yfinance as yf

    v = yf.Ticker(symbol).fast_info["market_cap"]
    return float(v) if v else None


# ---- 계산 ----
def index_returns(bars):
    """[(date, close)] -> {'asof', 'r3', 'r5', 'daily': {date: 일수익률 %}} (마지막 6개 일봉 사용)."""
    if len(bars) < 6:
        raise ValueError(f"지수 일봉 {len(bars)}개뿐")
    c = [b[1] for b in bars]
    daily = {bars[i][0]: (c[i] / c[i - 1] - 1) * 100 for i in range(len(bars) - 5, len(bars))}
    return dict(asof=bars[-1][0], r3=(c[-1] / c[-4] - 1) * 100, r5=(c[-1] / c[-6] - 1) * 100, daily=daily)


def evaluate(bars, idx):
    """한 종목의 지수 대비 성과. 기준일 일봉이 없거나(거래정지·지연) 이력이 부족하면 None."""
    if len(bars) < 21 or bars[-1][0] != idx["asof"]:
        return None
    c = [b[1] for b in bars]
    v = [b[2] for b in bars]
    if min(c[-6:]) <= 0:
        return None
    r3 = (c[-1] / c[-4] - 1) * 100
    r5 = (c[-1] / c[-6] - 1) * 100
    beat = 0
    for i in range(len(bars) - 5, len(bars)):
        d = bars[i][0]
        if d in idx["daily"] and (c[i] / c[i - 1] - 1) * 100 > idx["daily"][d]:
            beat += 1
    value20 = sum(ci * vi for ci, vi in zip(c[-20:], v[-20:])) / 20
    avg20 = sum(v[-20:]) / 20
    return dict(price=c[-1], r3=r3, r5=r5, ex3=r3 - idx["r3"], ex5=r5 - idx["r5"], beat_days=beat, value20=value20,
                vol_ratio=(sum(v[-5:]) / 5) / avg20 if avg20 else None)


def passes(m):
    return (m["price"] >= config.SCREEN_MIN_PRICE and m["value20"] >= config.SCREEN_MIN_VALUE20
            and m["ex3"] > 0 and m["ex5"] > 0 and m["beat_days"] >= config.SCREEN_BEAT_DAYS)


def flags_for(m):
    f = []
    if m["r5"] >= config.SCREEN_OVERHEAT_R5:
        f.append("overheat")
    if m["vol_ratio"] is not None and m["vol_ratio"] >= 2.0:
        f.append("volume_surge")
    return f


FLAG_TEXT = {"overheat": "단기 과열(5일 급등)", "volume_surge": "거래량 급증"}


def _mcap(conn, code, sym, fetch):
    r = conn.execute("SELECT mcap, fetched_at FROM mcap_cache WHERE code=?", (code,)).fetchone()
    if r and datetime.fromisoformat(r["fetched_at"]) > datetime.now(timezone.utc) - timedelta(days=config.SCREEN_MCAP_CACHE_DAYS):
        return r["mcap"]
    v = fetch(sym)
    if v:
        conn.execute("INSERT OR REPLACE INTO mcap_cache (code,mcap,fetched_at) VALUES (?,?,?)", (code, v, now_iso()))
    return v


def run(conn_factory, chunk_fetcher=None, index_fetcher=None, mcap_fetcher=None, delay=None, provider=None,
        headline_fetcher=None, ai=True):
    """전체 스캔. 이미 실행 중이면 None. 반환: 저장된 screen_runs.id."""
    if not _lock.acquire(blocking=False):
        return None
    try:
        raise_fd_limit()
        chunk_fetcher = chunk_fetcher or yf_chunk
        index_fetcher = index_fetcher or yf_index
        mcap_fetcher = mcap_fetcher or yf_mcap
        delay = config.SCREEN_MCAP_DELAY_SEC if delay is None else delay
        with conn_factory() as conn:
            universe = [dict(r) for r in conn.execute("SELECT code,name,market FROM stock_master")]
        try:
            idx = {m: index_returns(index_fetcher(sym)) for m, sym in config.SCREEN_INDEX.items()}
        except Exception as e:
            return _save_failed(conn_factory, len(universe), f"지수 조회 실패: {type(e).__name__}: {e}"[:300])
        asof = max(i["asof"] for i in idx.values())
        by_sym = {f"{u['code']}.{u['market']}": u for u in universe}
        syms = list(by_sym)
        loaded, failed, cands = 0, 0, []
        for i in range(0, len(syms), config.SCREEN_CHUNK):
            part = syms[i:i + config.SCREEN_CHUNK]
            try:
                got = chunk_fetcher(part)
            except Exception as e:
                log.warning("chunk failed: %s", type(e).__name__)
                got = {}
            failed += len(part) - len(got)
            for s, bars in got.items():
                u = by_sym[s]
                m = evaluate(bars, idx[u["market"]])
                if m is None:
                    failed += 1     # 기준일 일봉 없음(거래정지·지연)/이력 부족도 집계한다
                    continue
                loaded += 1
                if passes(m):
                    cands.append(dict(u, **m, flags=flags_for(m)))
        if universe and loaded < len(universe) * config.SCREEN_MIN_LOAD_RATIO:
            # 데이터 대부분을 못 받은 결과는 "강세 종목 없음"처럼 보이면 안 되므로 실패로 기록한다
            return _save_failed(conn_factory, len(universe),
                                f"종목 일봉 조회 성공률이 낮습니다 ({loaded}/{len(universe)}개). 네트워크/Yahoo 제한을 확인하세요")
        cands.sort(key=lambda x: -(x["ex3"] + x["ex5"]) / 2)
        final, missing = [], 0
        for c in cands:
            if len(final) >= config.SCREEN_TOP_N:
                break
            with conn_factory() as conn:
                cached = conn.execute("SELECT 1 FROM mcap_cache WHERE code=?", (c["code"],)).fetchone()
                try:
                    mc = _mcap(conn, c["code"], f"{c['code']}.{c['market']}", mcap_fetcher)
                except Exception:
                    mc = None
            if not cached and delay:
                time.sleep(delay)
            if mc is None:
                missing += 1       # 시가총액을 모르면 크기 필터를 통과시킬 수 없어 제외하고 개수만 알린다
                continue
            if mc >= config.SCREEN_MIN_MCAP:
                final.append(dict(c, mcap=mc))
        with conn_factory() as conn:
            rid = conn.execute(
                "INSERT INTO screen_runs (created_at,status,asof,index_ret,universe_n,loaded_n,failed_n,passed_n,mcap_missing_n,final_n) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now_iso(), "ok", asof,
                 json.dumps({m: dict(r3=round(v["r3"], 2), r5=round(v["r5"], 2)) for m, v in idx.items()}),
                 len(universe), loaded, failed, len(cands), missing, len(final))).lastrowid
            conn.executemany(
                "INSERT INTO screen_items (run_id,rank,code,name,market,price,r3,r5,ex3,ex5,beat_days,value20,mcap,vol_ratio,flags) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(rid, n, c["code"], c["name"], c["market"], c["price"], round(c["r3"], 2), round(c["r5"], 2), round(c["ex3"], 2),
                  round(c["ex5"], 2), c["beat_days"], c["value20"], c["mcap"],
                  None if c["vol_ratio"] is None else round(c["vol_ratio"], 2), json.dumps(c["flags"]))
                 for n, c in enumerate(final, 1)])
        if ai:
            _augment(conn_factory, rid, final, idx, provider, headline_fetcher)
        return rid
    finally:
        _lock.release()


def _augment(conn_factory, rid, final, idx, provider, headline_fetcher):
    """2단계: 후보의 최근 헤드라인을 모아 AI 가 강세 배경·톤·리스크를 해석한다. 실패해도 1단계 결과는 그대로 둔다."""
    from analyzer import screen_llm
    from collectors import news

    def mark(status, error=None):
        with conn_factory() as conn:
            conn.execute("UPDATE screen_runs SET ai_status=?, ai_error=? WHERE id=?", (status, error, rid))

    if not final:
        return mark("skipped")
    try:
        titles = {}
        for i, c in enumerate(final):
            if i and config.STOCK_VIEW_DELAY_SEC:
                time.sleep(config.STOCK_VIEW_DELAY_SEC)
            try:
                titles[c["code"]] = news.stock_headlines(c["name"], headline_fetcher)
            except Exception:
                titles[c["code"]] = []
        ctx = dict(
            index_returns={m: dict(r3=round(v["r3"], 1), r5=round(v["r5"], 1)) for m, v in idx.items()},
            stocks=[dict(code=c["code"], name=c["name"], market=c["market"], r3=round(c["r3"], 1), r5=round(c["r5"], 1),
                         excess3=round(c["ex3"], 1), excess5=round(c["ex5"], 1), beat_days_of_5=c["beat_days"],
                         flags=c["flags"], headlines=titles[c["code"]]) for c in final])
        result = screen_llm.analyze(ctx, provider or screen_llm.get_provider(), {c["code"] for c in final})
        if result["status"] != "ok":
            return mark("failed", result["error"])
        with conn_factory() as conn:
            for c in final:
                a = screen_llm.assess(result["items"].get(c["code"]), titles[c["code"]])
                conn.execute("UPDATE screen_items SET ai=? WHERE run_id=? AND code=?",
                             (json.dumps(a, ensure_ascii=False), rid, c["code"]))
        mark("ok")
    except Exception as e:
        mark("failed", f"{type(e).__name__}: {e}"[:300])


def _save_failed(conn_factory, universe_n, error):
    with conn_factory() as conn:
        return conn.execute("INSERT INTO screen_runs (created_at,status,error,universe_n) VALUES (?,?,?,?)",
                            (now_iso(), "failed", error, universe_n)).lastrowid


def latest(conn):
    """(최신 실행, 최신 성공 실행의 종목들). 실패했어도 마지막 성공 결과를 함께 보여준다."""
    last = conn.execute("SELECT * FROM screen_runs ORDER BY id DESC LIMIT 1").fetchone()
    ok = conn.execute("SELECT * FROM screen_runs WHERE status='ok' ORDER BY id DESC LIMIT 1").fetchone()
    if ok is None:
        return (dict(last) if last else None), None, []
    ok = dict(ok)
    ok["index_ret"] = json.loads(ok["index_ret"]) if ok["index_ret"] else {}
    items = []
    for r in conn.execute("SELECT * FROM screen_items WHERE run_id=? ORDER BY rank", (ok["id"],)):
        d = dict(r)
        d["flags"] = json.loads(d["flags"]) if d["flags"] else []
        d["ai"] = json.loads(d["ai"]) if d.get("ai") else None
        items.append(d)
    return (dict(last) if last else None), ok, items
