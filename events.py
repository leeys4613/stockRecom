"""이벤트 캘린더: 관심종목·강세 종목의 예정 이벤트를 뉴스에서 추출하고(AI) 파괴력을 판단해 저장한다.

날짜 형식·기간, 근거 헤드라인, 셀온 위험 표시는 코드가 검증·계산한다. 이벤트가 지난 뒤에는 실제 주가 반응을 채워
"이벤트 전 올랐다가 직후 빠졌는가(셀온)"를 유형별로 확인할 수 있게 한다. 매매를 지시하지 않으며 사용자가 정한 시점(이벤트 N일 전)에 알릴 뿐이다.
"""
import json
import logging
import statistics
import threading
import time
from datetime import date, datetime, timedelta, timezone

import calendar_kr
import config

log = logging.getLogger("events")
_lock = threading.Lock()
MIN_SAMPLE = 30


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(s):
    return "".join((s or "").split()).casefold()


def price_stats(closes):
    """종가 목록(오름차순, 약 1년) -> 가격 지표. 이력이 부족하면 예외."""
    if len(closes) < 30:
        raise ValueError(f"종가 {len(closes)}개뿐 (30개 필요)")
    rets = [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes)) if closes[i - 1] > 0]
    absr = sorted(abs(r) for r in rets)
    p95 = absr[min(len(absr) - 1, int(len(absr) * 0.95))]
    last = closes[-1]
    return dict(price=round(last, 2), r5=round((last / closes[-6] - 1) * 100, 1), r10=round((last / closes[-11] - 1) * 100, 1),
                dd60=round((last / max(closes[-60:]) - 1) * 100, 1), avg_abs_move_1y=round(statistics.fmean(absr), 2),
                p95_abs_move_1y=round(p95, 2), big_move_days_1y=sum(1 for r in absr if r >= config.EVENT_BIG_MOVE_PCT))


def universe(conn):
    """관심종목 먼저, 그다음 최신 강세 종목(스크리너 추천). 중복 제거, 최대 EVENT_MAX_STOCKS."""
    out, seen = [], set()
    for r in conn.execute("SELECT code,market,name FROM watchlist ORDER BY added_at, code"):
        out.append(dict(r, src="watchlist"))
        seen.add(r["code"])
    last = conn.execute("SELECT id FROM screen_runs WHERE status='ok' ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        for r in conn.execute("SELECT code,market,name FROM screen_items WHERE run_id=? AND status='kept' ORDER BY market DESC, rank",
                              (last["id"],)):
            if r["code"] not in seen:
                out.append(dict(r, src="screener"))
                seen.add(r["code"])
    return out[:config.EVENT_MAX_STOCKS]


def _past_reactions(conn, code):
    rows = conn.execute("SELECT event_type, date, pre_ret, post_ret FROM events WHERE code=? AND post_ret IS NOT NULL "
                        "ORDER BY date DESC LIMIT 3", (code,)).fetchall()
    return [dict(type=r["event_type"], date=r["date"], before_pct=None if r["pre_ret"] is None else round(r["pre_ret"], 1),
                 after_3d_pct=round(r["post_ret"], 1)) for r in rows]


def _upsert(conn, stock, ev, stats, now):
    """같은 이벤트가 매일 다르게 표현돼도 중복 저장하지 않는다. 날짜가 뒤늦게 확정되면 기존 '미확정' 행을 갱신한다."""
    code, typ = stock["code"], ev["event_type"]
    flags = json.dumps(ev["flags"])
    if ev["date"]:
        ukey = f"{code}|{typ}|{ev['date']}"
        if not conn.execute("SELECT 1 FROM events WHERE ukey=?", (ukey,)).fetchone():
            cutoff = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
            und = conn.execute("SELECT id FROM events WHERE code=? AND event_type=? AND date IS NULL AND last_seen>=? "
                               "ORDER BY id DESC LIMIT 1", (code, typ, cutoff)).fetchone()
            if und:
                conn.execute("UPDATE events SET ukey=? WHERE id=?", (ukey, und["id"]))
    else:
        ukey = f"{code}|{typ}|{_norm(ev['title'])[:30]}"
    row = conn.execute("SELECT id FROM events WHERE ukey=?", (ukey,)).fetchone()
    vals = (stock["name"], stock["market"], ev["title"], ev["date"], ev["date_note"], ev["impact"], ev["direction"], ev["priced_in"],
            ev["reason"], json.dumps([ev["evidence"]], ensure_ascii=False), flags, now)
    if row:
        conn.execute("UPDATE events SET name=?,market=?,title=?,date=?,date_note=?,impact=?,direction=?,priced_in=?,reason=?,evidence=?,"
                     "flags=?,last_seen=? WHERE id=?", (*vals, row["id"]))
        return row["id"]
    return conn.execute(
        "INSERT INTO events (code,event_type,ukey,name,market,title,date,date_note,impact,direction,priced_in,reason,evidence,flags,"
        "last_seen,first_seen,price_seen,r10_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, typ, ukey, *vals, now, stats["price"], stats["r10"])).lastrowid


def run(conn_factory, provider=None, headline_fetcher=None, closes_fetcher=None, today=None):
    """종목별 헤드라인·가격 지표를 모아 AI 가 이벤트를 추출·판단하게 하고 저장한다. 이미 실행 중이면 None. 반환: event_runs.id."""
    from analyzer import event_llm
    from collectors import macro, news

    if not _lock.acquire(blocking=False):
        return None
    try:
        today = today or calendar_kr.today_kst()
        with conn_factory() as conn:
            stocks = universe(conn)
            past = {s["code"]: _past_reactions(conn, s["code"]) for s in stocks}
        inputs, errors = [], 0
        for i, s in enumerate(stocks):
            if i and config.STOCK_VIEW_DELAY_SEC:
                time.sleep(config.STOCK_VIEW_DELAY_SEC)
            try:
                heads = news.stock_event_headlines(s["name"], headline_fetcher)
            except Exception:
                errors += 1
                continue
            if not heads:
                continue
            try:
                stats = price_stats((closes_fetcher or (lambda sym: macro.yf_closes(sym, "1y")))(f"{s['code']}.{s['market']}"))
            except Exception:
                stats = None
            inputs.append(dict(stock=s, heads=heads, stats=stats))
        if not inputs:
            return _save_run(conn_factory, "ok", None, len(stocks), 0)
        ctx = dict(today=today.isoformat(), stocks=[
            dict(code=x["stock"]["code"], name=x["stock"]["name"], market=x["stock"]["market"], price=x["stats"],
                 past_reactions=past[x["stock"]["code"]], headlines=x["heads"]) for x in inputs])
        result = event_llm.analyze(ctx, provider or event_llm.get_provider(), {x["stock"]["code"] for x in inputs})
        if result["status"] != "ok":
            return _save_run(conn_factory, "failed", result["error"], len(stocks), 0)
        now, n = now_iso(), 0
        with conn_factory() as conn:
            for x in inputs:
                s, stats = x["stock"], x["stats"]
                titles = [h["title"] for h in x["heads"]]
                for ev in event_llm.assess(result["items"].get(s["code"]), titles, today, None if not stats else stats["r10"]):
                    _upsert(conn, s, ev, stats or dict(price=None, r10=None), now)
                    n += 1
        return _save_run(conn_factory, "ok", None, len(stocks), n)
    except Exception as e:
        return _save_run(conn_factory, "failed", f"{type(e).__name__}: {e}"[:300], 0, 0)
    finally:
        _lock.release()


def _save_run(conn_factory, status, error, n_stocks, n_events):
    with conn_factory() as conn:
        return conn.execute("INSERT INTO event_runs (created_at,status,error,n_stocks,n_events) VALUES (?,?,?,?,?)",
                            (now_iso(), status, error, n_stocks, n_events)).lastrowid


def fill_outcomes(conn_factory, chunk_fetcher=None, today=None):
    """이벤트 날짜가 지난 뒤 실제 주가 반응을 채운다: pre_ret = 처음 본 날 대비 이벤트 전날까지, post_ret = 이벤트 전날 종가 대비 3거래일 뒤."""
    import screener

    chunk_fetcher = chunk_fetcher or screener.yf_chunk
    today = today or calendar_kr.today_kst()
    lo, hi = (today - timedelta(days=40)).isoformat(), (today - timedelta(days=5)).isoformat()
    with conn_factory() as conn:
        pend = [dict(r) for r in conn.execute("SELECT id,code,market,date,price_seen FROM events WHERE date IS NOT NULL AND date>=? AND date<=? "
                                              "AND post_ret IS NULL", (lo, hi))]
    if not pend:
        return 0
    syms = sorted({f"{p['code']}.{p['market']}" for p in pend})
    bars = {}
    for i in range(0, len(syms), config.SCREEN_CHUNK):
        try:
            bars.update(chunk_fetcher(syms[i:i + config.SCREEN_CHUNK]))
        except Exception as e:
            log.warning("event outcome fetch failed: %s", type(e).__name__)
    n = 0
    with conn_factory() as conn:
        for p in pend:
            b = bars.get(f"{p['code']}.{p['market']}")
            if not b:
                continue
            j = next((k for k, x in enumerate(b) if x[0] >= p["date"]), None)
            if j is None or j < 1 or j + 2 >= len(b):          # 이벤트 전날 종가와 3거래일 뒤 종가가 모두 필요
                continue
            pre = b[j - 1][1]
            pre_ret = (pre / p["price_seen"] - 1) * 100 if p["price_seen"] else None
            post_ret = (b[j + 2][1] / pre - 1) * 100
            conn.execute("UPDATE events SET pre_ret=?, post_ret=?, post_at=? WHERE id=?",
                         (None if pre_ret is None else round(pre_ret, 2), round(post_ret, 2), now_iso(), p["id"]))
            n += 1
    return n


def check_alerts(conn, today=None, sender=None):
    """관심종목의 파괴력 큼 이상 이벤트가 EVENT_ALERT_DAYS(예: 3일·1일 전)에 들어서면 한 번씩 알린다. 반환: 알린 이벤트 id 목록."""
    import alerts

    today = today or calendar_kr.today_kst()
    thresholds = sorted(config.EVENT_ALERT_DAYS)
    watch = {r["code"] for r in conn.execute("SELECT code FROM watchlist")}
    sent = []
    for e in conn.execute("SELECT * FROM events WHERE date IS NOT NULL AND date>=? AND impact>=?", (today.isoformat(), config.EVENT_ALERT_MIN_IMPACT)):
        if e["code"] not in watch:
            continue
        left = (date.fromisoformat(e["date"]) - today).days
        band = [n for n in thresholds if left <= n]
        if not band:
            continue
        band = min(band)
        if conn.execute("SELECT 1 FROM event_alerts_sent WHERE event_id=? AND days_before=?", (e["id"], band)).fetchone():
            continue
        flags = json.loads(e["flags"]) if e["flags"] else []
        body = (f"{e['date']} {e['title']} — 파괴력 {alerts_text(e['impact'])}"
                + (" · 호재가 이미 반영됐을 수 있어 셀온 가능성에 유의" if "sell_news_risk" in flags else "")
                + ". 이벤트 전 점검 시점입니다(매매 권유가 아닙니다).")
        payload = dict(title=f"{e['name']} · {'오늘' if left == 0 else f'D-{left}'} {e['event_type']}", body=body, url="/events",
                       tag=f"event-{e['id']}")
        if alerts.notify_all(conn, payload, sender):
            for n in thresholds:
                if n >= band:
                    conn.execute("INSERT OR IGNORE INTO event_alerts_sent (event_id,days_before,sent_at) VALUES (?,?,?)", (e["id"], n, now_iso()))
            sent.append(e["id"])
    return sent


def alerts_text(level):
    from analyzer import event_llm
    return event_llm.IMPACT_TEXT.get(level, "-")


def view(conn, today=None):
    """화면용: 예정(날짜 확정, 임박순), 일정 미확정, 지난 이벤트와 실제 반응, 유형별 셀온 통계."""
    from analyzer import event_llm

    today = today or calendar_kr.today_kst()
    fresh = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
    watch = {r["code"] for r in conn.execute("SELECT code FROM watchlist")}

    def parse(r):
        d = dict(r)
        d["evidence"] = json.loads(d["evidence"]) if d.get("evidence") else []
        d["flags"] = json.loads(d["flags"]) if d.get("flags") else []
        d["impact_text"] = event_llm.IMPACT_TEXT[d["impact"]]
        d["priced_text"] = event_llm.PRICED_TEXT[d["priced_in"]]
        d["in_watch"] = d["code"] in watch
        d["days_left"] = (date.fromisoformat(d["date"]) - today).days if d["date"] else None
        return d

    rows = [parse(r) for r in conn.execute("SELECT * FROM events WHERE last_seen>=? OR (date IS NOT NULL AND date>=?) ORDER BY date, impact DESC",
                                           (fresh, (today - timedelta(days=30)).isoformat()))]
    upcoming = sorted((e for e in rows if e["date"] and e["date"] >= today.isoformat() and e["last_seen"] >= fresh),
                      key=lambda e: (e["date"], -e["impact"]))
    undated = sorted((e for e in rows if not e["date"] and e["last_seen"] >= fresh), key=lambda e: -e["impact"])
    past = sorted((e for e in rows if e["date"] and e["date"] < today.isoformat()), key=lambda e: e["date"], reverse=True)
    done = [e for e in conn.execute("SELECT event_type, impact, pre_ret, post_ret FROM events WHERE post_ret IS NOT NULL")]

    def agg(rs):
        pre = [r["pre_ret"] for r in rs if r["pre_ret"] is not None]
        post = [r["post_ret"] for r in rs]
        return dict(n=len(rs), pre=statistics.fmean(pre) if pre else None, post=statistics.fmean(post),
                    drop=sum(1 for x in post if x < 0) / len(post) * 100, small=len(rs) < MIN_SAMPLE)

    stats = []
    for title, key in (("이벤트 유형별", lambda r: r["event_type"]),
                       ("파괴력별", lambda r: event_llm.IMPACT_TEXT[r["impact"]])):
        b = {}
        for r in done:
            b.setdefault(key(r), []).append(r)
        stats.append(dict(title=title, items=[dict(label=k, **agg(v)) for k, v in sorted(b.items(), key=lambda kv: -len(kv[1]))]))
    last = conn.execute("SELECT * FROM event_runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(upcoming=upcoming, undated=undated, past=past[:20], stats=stats, n_done=len(done), last=dict(last) if last else None,
                min_sample=MIN_SAMPLE)
