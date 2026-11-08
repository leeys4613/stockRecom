"""AI 투자 사이클(데이터센터 건설 속도) 트래커: 종목별 가격 스냅샷 + 빅테크 분기 CAPEX.

수요(빅테크 CAPEX) → 공급망(반도체·메모리) → 인프라(데이터센터)가 서로 어떻게 반응하는지 추적한다.
CAPEX 는 늘어도 주가가 크게 빠지면 시장이 투자 지속성/수익성을 의심한다는 신호다.
종목별로 ok/failed 를 기록한다 (실패를 숨기지 않는다).
"""
from datetime import datetime, timedelta, timezone

import config


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def metrics_from_closes(closes):
    """종가 목록 -> (가격, 1일 변화%, TREND_DAYS 거래일 변화%, 60일 고점 대비%). 데이터 부족 시 예외."""
    n = config.TREND_DAYS
    if len(closes) < n + 1:
        raise ValueError(f"종가 {len(closes)}개뿐 ({n + 1}개 필요)")
    last = closes[-1]
    return (last, (last / closes[-2] - 1) * 100, (last / closes[-(n + 1)] - 1) * 100,
            (last / max(closes[-config.AI_DD_WINDOW:]) - 1) * 100)


def collect(history_fetcher=None):
    from collectors import macro

    fetch = history_fetcher or macro.yf_closes
    out = []
    for ticker, (_, grp) in config.AI_TICKERS.items():
        fetched_at = now_iso()
        try:
            price, c1, c10, dd = metrics_from_closes(fetch(ticker))
            out.append(dict(ticker=ticker, grp=grp, status="ok", error=None, fetched_at=fetched_at,
                            price=price, chg1d=c1, chg10d=c10, dd60=dd))
        except Exception as e:
            out.append(dict(ticker=ticker, grp=grp, status="failed", error=f"{type(e).__name__}: {e}"[:300],
                            fetched_at=fetched_at, price=None, chg1d=None, chg10d=None, dd60=None))
    return out


def save(conn, snaps):
    conn.executemany(
        "INSERT INTO ai_tracker_snapshots (ticker,grp,status,error,fetched_at,price,chg1d,chg10d,dd60) "
        "VALUES (:ticker,:grp,:status,:error,:fetched_at,:price,:chg1d,:chg10d,:dd60)", snaps)


def latest(conn):
    """종목별 최신 시도 + 최신 성공 값(실패해도 마지막 성공 값은 stale 로 표시)."""
    rows = []
    now = datetime.now(timezone.utc)
    for ticker, (name, grp) in config.AI_TICKERS.items():
        last = conn.execute("SELECT * FROM ai_tracker_snapshots WHERE ticker=? ORDER BY fetched_at DESC, id DESC LIMIT 1",
                            (ticker,)).fetchone()
        ok = conn.execute("SELECT * FROM ai_tracker_snapshots WHERE ticker=? AND status='ok' "
                          "ORDER BY fetched_at DESC, id DESC LIMIT 1", (ticker,)).fetchone()
        age_h = None if ok is None else (now - datetime.fromisoformat(ok["fetched_at"])).total_seconds() / 3600
        rows.append(dict(ticker=ticker, name=name, grp=grp,
                         status=last["status"] if last else "none", error=last["error"] if last else None,
                         price=ok["price"] if ok else None, chg1d=ok["chg1d"] if ok else None,
                         chg10d=ok["chg10d"] if ok else None, dd60=ok["dd60"] if ok else None,
                         fetched_at=ok["fetched_at"] if ok else None, age_hours=age_h,
                         stale=age_h is None or age_h > 36))
    return rows


def group_summary(rows):
    """종목군별 평균 (최신 데이터만). 반환: {grp: dict(n, total, chg1d, chg10d, dd60)}"""
    out = {}
    for grp in config.AI_GROUP_NAMES:
        got = [r for r in rows if r["grp"] == grp and r["price"] is not None and not r["stale"]]
        total = sum(1 for r in rows if r["grp"] == grp)
        out[grp] = dict(n=len(got), total=total,
                        chg1d=sum(r["chg1d"] for r in got) / len(got) if got else None,
                        chg10d=sum(r["chg10d"] for r in got) / len(got) if got else None,
                        dd60=sum(r["dd60"] for r in got) / len(got) if got else None)
    return out


# ---------------- 빅테크 분기 CAPEX ----------------
def yf_capex(ticker):
    """yfinance 분기 현금흐름표의 설비투자 -> [(분기말 YYYY-MM-DD, 양수 USD)]. 없으면 예외."""
    import yfinance as yf

    cf = yf.Ticker(ticker).quarterly_cashflow
    rows = [r for r in cf.index if "capital expenditure" in str(r).lower()]
    if not rows:
        raise ValueError("설비투자(Capital Expenditure) 항목 없음")
    s = cf.loc[rows[0]].dropna().sort_index()
    return [(str(d.date()), abs(float(v))) for d, v in s.items()]


def capex_due(conn):
    r = conn.execute("SELECT MAX(fetched_at) FROM capex_quarters").fetchone()[0]
    return r is None or datetime.now(timezone.utc) - datetime.fromisoformat(r) > timedelta(hours=config.CAPEX_REFRESH_HOURS)


def collect_capex(fetcher=None):
    """[(ticker, [(period_end, capex)])], 실패 목록 [(ticker, 사유)]."""
    ok, failed = [], []
    for t in config.CAPEX_TICKERS:
        try:
            ok.append((t, (fetcher or yf_capex)(t)))
        except Exception as e:
            failed.append((t, f"{type(e).__name__}: {e}"[:200]))
    return ok, failed


def save_capex(conn, collected):
    now = now_iso()
    conn.executemany("INSERT OR REPLACE INTO capex_quarters (ticker,period_end,capex,fetched_at) VALUES (?,?,?,?)",
                     [(t, d, v, now) for t, rows in collected for d, v in rows])


def _days(a, b):
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days


def _consecutive_tail(items):
    """[(분기말, 값)] 오름차순에서 최신 분기까지 끊기지 않고 이어진(분기 누락 없는) 구간만 반환."""
    if not items:
        return []
    tail = [items[-1]]
    for x in reversed(items[:-1]):
        if _days(x[0], tail[0][0]) > 120:
            break
        tail.insert(0, x)
    return tail


HORIZONS = ((1, "분기"), (2, "2분기"), (3, "3분기"))


def growth_trend(values):
    """증가율의 추세: 1·2·3분기 전 대비 '이번 분기 기준 증가율'과 '직전 분기 기준 증가율'을 비교한다.

    values: 연속 분기의 값(오름차순). 예) 분기 기준: 이번 = 이번/직전분기-1, 직전 = 직전분기/그전분기-1.
    변화가 -CAPEX_QOQ_DECEL_PP(%p) 보다 작으면 '둔화', +CAPEX_QOQ_DECEL_PP 보다 크면 '가속', 아니면 '유사'.
    데이터가 모자라 계산할 수 없는 기간은 생략한다.
    """
    out = []
    last = len(values) - 1
    for h, label in HORIZONS:
        if last - 1 - h < 0 or not values[last - h] or not values[last - 1 - h]:
            continue
        now = (values[last] / values[last - h] - 1) * 100
        prev = (values[last - 1] / values[last - 1 - h] - 1) * 100
        d = now - prev
        verdict = "둔화" if d < -config.CAPEX_QOQ_DECEL_PP else "가속" if d > config.CAPEX_QOQ_DECEL_PP else "유사"
        out.append(dict(horizon=h, label=label, now=now, prev=prev, delta=d, verdict=verdict))
    return out


def trend_overall(trend):
    """여러 기간의 판정을 종합한 한 줄. 계산할 기간이 없으면 None."""
    if not trend:
        return None
    n = len(trend)
    dec = sum(1 for t in trend if t["verdict"] == "둔화")
    acc = sum(1 for t in trend if t["verdict"] == "가속")
    if dec == n:
        return "모든 기간에서 증가율 둔화"
    if acc == n:
        return "모든 기간에서 증가율 가속"
    if dec > acc:
        return "대체로 증가율 둔화"
    if acc > dec:
        return "대체로 증가율 가속"
    return "혼조(뚜렷한 방향 없음)"


def capex_summary(conn):
    """종목별 최신 분기 CAPEX, 전년 동기 대비, 이상치 표시 + 합계(이상 없는 종목만)."""
    per = []
    for t in config.CAPEX_TICKERS:
        q = [(r["period_end"], r["capex"]) for r in conn.execute(
            "SELECT period_end,capex FROM capex_quarters WHERE ticker=? ORDER BY period_end", (t,))]
        if not q:
            per.append(dict(ticker=t, name=config.AI_TICKERS.get(t, (t,))[0], latest=None, flags=[], trend=[], trend_overall=None))
            continue
        d, v = q[-1]
        prev = q[-2] if len(q) >= 2 else None
        yoy_base = next((x for x in q if 330 <= _days(x[0], d) <= 400), None)
        flags = []
        if prev:
            if _days(prev[0], d) > 120:
                flags.append("직전 분기 데이터 누락")
            ratio = v / prev[1] if prev[1] else None
            if ratio and (ratio >= config.CAPEX_ANOMALY_QOQ or ratio <= 1 / config.CAPEX_ANOMALY_QOQ):
                flags.append("직전 분기 대비 급변(데이터 확인 필요)")
        trend = [] if flags else growth_trend([x[1] for x in _consecutive_tail(q)])   # 이상치 종목은 추세를 내지 않는다
        per.append(dict(ticker=t, name=config.AI_TICKERS.get(t, (t,))[0], latest=d, capex=v,
                        yoy=(v / yoy_base[1] - 1) * 100 if yoy_base else None,
                        qoq=(v / prev[1] - 1) * 100 if prev else None, flags=flags,
                        trend=trend, trend_overall=trend_overall(trend)))
    # 합계는 이상치가 없는 종목만. 분기 대비(QoQ)가 기본 지표이고 전년 대비(YoY)는 보조다
    clean_q = [p for p in per if p.get("latest") and not p["flags"] and p["qoq"] is not None]
    clean_y = [p for p in per if p.get("latest") and not p["flags"] and p["yoy"] is not None]
    total = None
    if clean_q:
        cur = sum(p["capex"] for p in clean_q)
        prev = sum(p["capex"] / (1 + p["qoq"] / 100) for p in clean_q)
        total = dict(n=len(clean_q), capex=cur, qoq=(cur / prev - 1) * 100, period=max(p["latest"] for p in clean_q),
                     yoy=None, yoy_n=len(clean_y), qoq_history=[], trend=[], overall=None)
        if clean_y:
            c2 = sum(p["capex"] for p in clean_y)
            b2 = sum(p["capex"] / (1 + p["yoy"] / 100) for p in clean_y)
            total["yoy"] = (c2 / b2 - 1) * 100
        # 최근 분기별 합계의 QoQ 흐름(가속/둔화): 모든 대상 종목에 데이터가 있는 분기만 사용
        by_t = {p["ticker"]: dict(conn.execute("SELECT period_end,capex FROM capex_quarters WHERE ticker=? ORDER BY period_end",
                                               (p["ticker"],)).fetchall()) for p in clean_q}
        common = sorted(set.intersection(*[set(d) for d in by_t.values()]))
        # 모든 대상 종목에 데이터가 있고 분기가 끊기지 않은 구간만 사용
        tail = _consecutive_tail([(qd, sum(d[qd] for d in by_t.values())) for qd in common])
        sums = [v for _, v in tail]
        total["qoq_history"] = [dict(period=tail[i][0], qoq=(sums[i] / sums[i - 1] - 1) * 100)
                                for i in range(max(1, len(tail) - 3), len(tail)) if sums[i - 1]]
        total["trend"] = growth_trend(sums)
        total["overall"] = trend_overall(total["trend"])
    return dict(per=per, total=total)
