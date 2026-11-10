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
    if len(bars) < 11:
        raise ValueError(f"지수 일봉 {len(bars)}개뿐")
    c = [b[1] for b in bars]
    daily = {bars[i][0]: (c[i] / c[i - 1] - 1) * 100 for i in range(len(bars) - 5, len(bars))}
    return dict(asof=bars[-1][0], r3=(c[-1] / c[-4] - 1) * 100, r5=(c[-1] / c[-6] - 1) * 100,
                r10=(c[-1] / c[-11] - 1) * 100, daily=daily)


def evaluate(bars, idx):
    """한 종목의 지수 대비 성과. 기준일 일봉이 없거나(거래정지·지연) 이력이 부족하면 None."""
    if len(bars) < 21 or bars[-1][0] != idx["asof"]:
        return None
    c = [b[1] for b in bars]
    v = [b[2] for b in bars]
    if min(c[-11:]) <= 0:
        return None
    r3 = (c[-1] / c[-4] - 1) * 100
    r5 = (c[-1] / c[-6] - 1) * 100
    r10 = (c[-1] / c[-11] - 1) * 100
    beat = 0
    for i in range(len(bars) - 5, len(bars)):
        d = bars[i][0]
        if d in idx["daily"] and (c[i] / c[i - 1] - 1) * 100 > idx["daily"][d]:
            beat += 1
    max_day = max((c[i] / c[i - 1] - 1) * 100 for i in range(len(c) - 10, len(c)))      # 최근 10거래일 중 가장 큰 하루 상승률
    value20 = sum(ci * vi for ci, vi in zip(c[-20:], v[-20:])) / 20
    avg20 = sum(v[-20:]) / 20
    return dict(price=c[-1], r3=r3, r5=r5, r10=r10, ex3=r3 - idx["r3"], ex5=r5 - idx["r5"], ex10=r10 - idx["r10"],
                beat_days=beat, max_day=max_day, value20=value20,
                vol_ratio=(sum(v[-5:]) / 5) / avg20 if avg20 else None)


def passes_base(m):
    """급등일 조건을 뺀 나머지 기본 조건."""
    return (m["price"] >= config.SCREEN_MIN_PRICE and m["value20"] >= config.SCREEN_MIN_VALUE20
            and m["ex3"] > 0 and m["ex5"] > 0 and m["ex10"] > 0 and m["beat_days"] >= config.SCREEN_BEAT_DAYS)


def has_jump(m):
    """최근 10거래일 중 하루에 SCREEN_MAX_DAY_JUMP% 이상 오른 날이 있다 — 그 하루가 초과수익을 만들었을 수 있다."""
    return m["max_day"] >= config.SCREEN_MAX_DAY_JUMP - 1e-6      # 부동소수점 오차로 정확히 15%인 날이 14.9999…가 되는 것을 막는다


def passes(m):
    return passes_base(m) and not has_jump(m)


def rank_key(m):
    """순위: 3일·5일·10일 지수 대비 초과수익의 평균(클수록 위)."""
    return -(m["ex3"] + m["ex5"] + m["ex10"]) / 3


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
        try:    # 지난 스캔 후보의 5·10일 뒤 성과를 채운다(실패해도 이번 스캔에는 영향 없음)
            fill_outcomes(conn_factory, chunk_fetcher, index_fetcher)
        except Exception as e:
            log.warning("fill_outcomes failed: %s", type(e).__name__)
        delay = config.SCREEN_MCAP_DELAY_SEC if delay is None else delay
        with conn_factory() as conn:
            universe = [dict(r) for r in conn.execute("SELECT code,name,market,industry FROM stock_master")]
        try:
            idx = {m: index_returns(index_fetcher(sym)) for m, sym in config.SCREEN_INDEX.items()}
        except Exception as e:
            return _save_failed(conn_factory, len(universe), f"지수 조회 실패: {type(e).__name__}: {e}"[:300])
        asof = max(i["asof"] for i in idx.values())
        by_sym = {f"{u['code']}.{u['market']}": u for u in universe}
        syms = list(by_sym)
        loaded, failed, cands, jumped = 0, 0, [], []
        allm = {}       # 평가가 끝난 모든 종목의 지표 (대장주 비교에 쓴다)
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
                allm[u["code"]] = m
                if passes_base(m):
                    (jumped if has_jump(m) else cands).append(dict(u, **m, flags=flags_for(m)))
        if universe and loaded < len(universe) * config.SCREEN_MIN_LOAD_RATIO:
            # 데이터 대부분을 못 받은 결과는 "강세 종목 없음"처럼 보이면 안 되므로 실패로 기록한다
            return _save_failed(conn_factory, len(universe),
                                f"종목 일봉 조회 성공률이 낮습니다 ({loaded}/{len(universe)}개). 네트워크/Yahoo 제한을 확인하세요")
        cands.sort(key=rank_key)
        final, missing = [], 0
        in_pool = {"KS": 0, "KQ": 0}      # 코스피·코스닥을 따로 뽑는다(각각 자기 지수와 비교)
        for c in cands:
            if in_pool[c["market"]] >= config.SCREEN_AI_POOL:
                continue
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
                in_pool[c["market"]] += 1
        # 하루 급등으로 제외한 종목 중 상위 일부는 성과 비교용으로만 기록한다(화면에는 안 나옴)
        jump_track = []
        jumped.sort(key=rank_key)
        for c in jumped:
            if len(jump_track) >= config.SCREEN_JUMP_TRACK:
                break
            with conn_factory() as conn:
                cached = conn.execute("SELECT 1 FROM mcap_cache WHERE code=?", (c["code"],)).fetchone()
                try:
                    mc = _mcap(conn, c["code"], f"{c['code']}.{c['market']}", mcap_fetcher)
                except Exception:
                    mc = None
            if not cached and delay:
                time.sleep(delay)
            if mc and mc >= config.SCREEN_MIN_MCAP:
                jump_track.append(dict(c, mcap=mc, rank=0, status="jump"))
        # 2단계: AI 가 뉴스 근거로 강세 배경을 해석한다. 배경을 확인할 수 없는("뚜렷한 배경 없음") 종목은 추천에서 뺀다
        ai_status, ai_error, notes, dropped = None, None, {}, 0
        kept = final
        if ai:
            ai_status, ai_error, notes = _interpret(final, idx, provider, headline_fetcher)
            if ai_status == "ok":
                kept = [c for c in final if notes[c["code"]]["reason_type"] != "뚜렷한 배경 없음"]
                dropped = len(final) - len(kept)
            elif ai_status == "failed":
                notes = {}      # 해석을 못 했으면 뺄 근거가 없으므로 수치 순위 그대로 보여주되 화면에 경고한다
        per, kept_top = {"KS": 0, "KQ": 0}, []
        for c in kept:                      # 시장별 상위 TOP_N (순위는 시장 안에서 매긴다)
            if per[c["market"]] < config.SCREEN_TOP_N:
                per[c["market"]] += 1
                kept_top.append(dict(c, rank=per[c["market"]]))
        kept = kept_top
        kept_codes = {c["code"] for c in kept}
        others = []         # 추천에서 빠진 풀 구성원도 성과 비교용으로 기록한다(화면에는 안 나옴)
        for c in final:
            if c["code"] not in kept_codes:
                nobg = ai_status == "ok" and notes[c["code"]]["reason_type"] == "뚜렷한 배경 없음"
                others.append(dict(c, rank=0, status="nobg" if nobg else "cut"))
        leaders = _leaders(conn_factory, kept, universe, allm, mcap_fetcher, delay)
        with conn_factory() as conn:
            rid = conn.execute(
                "INSERT INTO screen_runs (created_at,status,asof,index_ret,universe_n,loaded_n,failed_n,passed_n,mcap_missing_n,final_n,"
                "ai_status,ai_error,pool_n,nobg_n,jump_n) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now_iso(), "ok", asof,
                 json.dumps({m: dict(r3=round(v["r3"], 2), r5=round(v["r5"], 2), r10=round(v["r10"], 2)) for m, v in idx.items()}),
                 len(universe), loaded, failed, len(cands), missing, len(kept), ai_status, ai_error, len(final), dropped, len(jumped))).lastrowid
            conn.executemany(
                "INSERT INTO screen_items (run_id,rank,code,name,market,price,r3,r5,r10,ex3,ex5,ex10,beat_days,value20,mcap,vol_ratio,flags,ai,leader,status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(rid, n, c["code"], c["name"], c["market"], c["price"], round(c["r3"], 2), round(c["r5"], 2), round(c["r10"], 2),
                  round(c["ex3"], 2), round(c["ex5"], 2), round(c["ex10"], 2), c["beat_days"], c["value20"], c["mcap"],
                  None if c["vol_ratio"] is None else round(c["vol_ratio"], 2), json.dumps(c["flags"]),
                  json.dumps(notes[c["code"]], ensure_ascii=False) if c["code"] in notes else None,
                  json.dumps(leaders[c["code"]], ensure_ascii=False) if leaders.get(c["code"]) else None,
                  c.get("status", "kept"))
                 for n, c in ((c["rank"], c) for c in kept + others + jump_track)])
        return rid
    finally:
        _lock.release()


def fill_outcomes(conn_factory, chunk_fetcher=None, index_fetcher=None):
    """기준일로부터 5·10거래일이 지난 후보의 실제 성과(종목 수익률, 소속 지수 대비 초과수익)를 채운다. 반환: 갱신한 행 수."""
    chunk_fetcher = chunk_fetcher or yf_chunk
    index_fetcher = index_fetcher or yf_index
    with conn_factory() as conn:
        pending = [dict(r) for r in conn.execute(
            "SELECT i.run_id, i.code, i.market, r.asof FROM screen_items i JOIN screen_runs r ON r.id=i.run_id "
            "WHERE r.status='ok' AND r.asof IS NOT NULL AND (i.fwd5 IS NULL OR i.fwd10 IS NULL) "
            "AND r.asof >= date('now','-45 days') AND r.asof <= date('now','-6 days')")]
    if not pending:
        return 0
    syms = sorted({f"{p['code']}.{p['market']}" for p in pending})
    bars = {}
    for i in range(0, len(syms), config.SCREEN_CHUNK):
        try:
            bars.update(chunk_fetcher(syms[i:i + config.SCREEN_CHUNK]))
        except Exception as e:
            log.warning("outcome chunk failed: %s", type(e).__name__)
    idx = {}
    for m in {p["market"] for p in pending}:
        try:
            idx[m] = dict(index_fetcher(config.SCREEN_INDEX[m]))
        except Exception as e:
            log.warning("outcome index failed: %s", type(e).__name__)
    n = 0
    now = now_iso()
    with conn_factory() as conn:
        for p in pending:
            b = bars.get(f"{p['code']}.{p['market']}")
            im = idx.get(p["market"])
            if not b or not im:
                continue
            dates = [x[0] for x in b]
            if p["asof"] not in dates or p["asof"] not in im:
                continue
            pos = dates.index(p["asof"])
            entry = b[pos][1]
            vals = {}
            for h in (5, 10):
                if pos + h < len(b) and b[pos + h][0] in im and entry > 0:
                    ret = (b[pos + h][1] / entry - 1) * 100
                    iret = (im[b[pos + h][0]] / im[p["asof"]] - 1) * 100
                    vals[h] = (round(ret, 2), round(ret - iret, 2))
            if vals:
                conn.execute(
                    "UPDATE screen_items SET fwd5=COALESCE(?,fwd5), fwd5_ex=COALESCE(?,fwd5_ex), fwd10=COALESCE(?,fwd10), "
                    "fwd10_ex=COALESCE(?,fwd10_ex), fwd_at=? WHERE run_id=? AND code=?",
                    (*(vals.get(5) or (None, None)), *(vals.get(10) or (None, None)), now, p["run_id"], p["code"]))
                n += 1
    return n


MIN_SAMPLE = 30


def _agg(rows):
    def side(key):
        v = [r[key] for r in rows if r[key] is not None]
        return dict(n=len(v), mean=sum(v) / len(v) if v else None, hit=sum(1 for x in v if x > 0) / len(v) * 100 if v else None)
    return dict(d5=side("fwd5_ex"), d10=side("fwd10_ex"))


def review(conn):
    """후보 유형별 실제 성과(지수 대비 초과수익). 표본이 MIN_SAMPLE 미만인 그룹은 small=True."""
    rows = []
    for r in conn.execute("SELECT i.*, r.asof FROM screen_items i JOIN screen_runs r ON r.id=i.run_id WHERE r.status='ok' "
            "AND r.id IN (SELECT MAX(id) FROM screen_runs WHERE status='ok' GROUP BY asof)"):
        d = dict(r)
        d["ai"] = json.loads(d["ai"]) if d.get("ai") else None
        d["leader"] = json.loads(d["leader"]) if d.get("leader") else None
        d["flags"] = json.loads(d["flags"]) if d.get("flags") else []
        rows.append(d)
    done = [r for r in rows if r["fwd5_ex"] is not None or r["fwd10_ex"] is not None]

    def leader_key(r):
        ld = r["leader"]
        if not ld:
            return "업종 정보 없음"
        return "자신이 대장주" if ld["is_self"] else "대장주도 강세" if ld["ex5"] > 0 else "대장주는 지수 이하"

    def tone_key(r):
        t = (r["ai"] or {}).get("news_tone")
        return None if t is None else "뉴스 긍정(+1 이상)" if t >= 1 else "뉴스 부정(-1 이하)" if t <= -1 else "뉴스 중립(0)"

    specs = [
        ("추천 여부", lambda r: dict(kept="추천(화면에 표시)", nobg="AI가 배경 불명으로 제외", cut="순위권 밖(시장별 상위 밖)",
                                                       jump="하루 급등(15%+)일 포함으로 제외").get(r["status"])),
        ("시장", lambda r: "코스피" if r["market"] == "KS" else "코스닥"),
        ("강세 배경(AI)", lambda r: (r["ai"] or {}).get("reason_type")),
        ("재료 성격(AI)", lambda r: (r["ai"] or {}).get("catalyst_kind")),
        ("뉴스 톤(AI)", tone_key),
        ("단기 과열 플래그", lambda r: "과열 표시 있음" if "overheat" in r["flags"] else "과열 표시 없음"),
        ("업종 대장주", leader_key),
        ("추천 순위(시장 내)", lambda r: None if r["status"] != "kept" else "1~3위" if r["rank"] <= 3 else "4~10위"),
    ]
    groups = []
    for title, fn in specs:
        buckets = {}
        for r in done:
            k = fn(r)
            if k is not None:
                buckets.setdefault(k, []).append(r)
        items = []
        for k, rs in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
            a = _agg(rs)
            items.append(dict(label=k, total=len(rs), small=max(a["d5"]["n"], a["d10"]["n"]) < MIN_SAMPLE, **a))
        groups.append(dict(title=title, items=items))
    pending = sum(1 for r in rows if r["fwd5_ex"] is None)
    return dict(overall=_agg(done), n_items=len(rows), n_done=len(done), n_pending=pending,
                n_runs=len({r["run_id"] for r in rows}), groups=groups, min_sample=MIN_SAMPLE)


def _leaders(conn_factory, kept, universe, allm, mcap_fetcher, delay):
    """추천 종목마다 같은 업종(KRX 업종명)의 대장주 = 시가총액 1위를 찾는다. 반환: {code: leader dict 또는 None}.

    업종 안에서 20일 평균 거래대금 상위 LEADER_CHECK 개만 시가총액을 조회한다(대장주는 거래도 활발하다는 가정, 조회 비용 제한).
    시가총액을 하나도 확인하지 못하면 None.
    """
    by_ind = {}
    for u in universe:
        if u.get("industry") and u["code"] in allm:
            by_ind.setdefault(u["industry"], []).append(u)
    cache = {}      # 업종 -> leader dict
    out = {}
    for c in kept:
        ind = next((u["industry"] for u in universe if u["code"] == c["code"]), None)
        if not ind:
            out[c["code"]] = None
            continue
        if ind not in cache:
            members = sorted(by_ind.get(ind, []), key=lambda u: -allm[u["code"]]["value20"])[:config.SCREEN_LEADER_CHECK]
            best = None
            for u in members:
                with conn_factory() as conn:
                    cached = conn.execute("SELECT 1 FROM mcap_cache WHERE code=?", (u["code"],)).fetchone()
                    try:
                        mc = _mcap(conn, u["code"], f"{u['code']}.{u['market']}", mcap_fetcher)
                    except Exception:
                        mc = None
                if not cached and delay:
                    time.sleep(delay)
                if mc and (best is None or mc > best[1]):
                    best = (u, mc)
            if best is None:
                cache[ind] = None
            else:
                u, mc = best
                m = allm[u["code"]]
                cache[ind] = dict(code=u["code"], name=u["name"], market=u["market"], mcap=mc, r5=round(m["r5"], 1),
                                  ex5=round(m["ex5"], 1), r10=round(m["r10"], 1), ex10=round(m["ex10"], 1), industry=ind,
                                  members=len(by_ind.get(ind, [])))
        lead = cache[ind]
        out[c["code"]] = None if lead is None else dict(lead, is_self=lead["code"] == c["code"])
    return out


def _interpret(final, idx, provider, headline_fetcher):
    """2단계: 후보의 최근 헤드라인을 모아 AI 가 강세 배경·톤·리스크를 해석한다. 반환: (status, error, {code: 해석}).

    status: ok / failed / skipped(후보 없음). 실패해도 예외를 내지 않는다.
    """
    from analyzer import screen_llm
    from collectors import news

    if not final:
        return "skipped", None, {}
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
            index_returns={m: dict(r3=round(v["r3"], 1), r5=round(v["r5"], 1), r10=round(v["r10"], 1)) for m, v in idx.items()},
            stocks=[dict(code=c["code"], name=c["name"], market=c["market"], r3=round(c["r3"], 1), r5=round(c["r5"], 1),
                         r10=round(c["r10"], 1), excess3=round(c["ex3"], 1), excess5=round(c["ex5"], 1),
                         excess10=round(c["ex10"], 1), beat_days_of_5=c["beat_days"],
                         flags=c["flags"], headlines=titles[c["code"]]) for c in final])
        result = screen_llm.analyze(ctx, provider or screen_llm.get_provider(), {c["code"] for c in final})
        if result["status"] != "ok":
            return "failed", result["error"], {}
        return "ok", None, {c["code"]: screen_llm.assess(result["items"].get(c["code"]), titles[c["code"]]) for c in final}
    except Exception as e:
        return "failed", f"{type(e).__name__}: {e}"[:300], {}


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
    for r in conn.execute("SELECT * FROM screen_items WHERE run_id=? AND status='kept' ORDER BY market DESC, rank", (ok["id"],)):
        d = dict(r)
        d["flags"] = json.loads(d["flags"]) if d["flags"] else []
        d["ai"] = json.loads(d["ai"]) if d.get("ai") else None
        d["leader"] = json.loads(d["leader"]) if d.get("leader") else None
        items.append(d)
    return (dict(last) if last else None), ok, items
