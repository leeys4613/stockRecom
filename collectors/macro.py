"""매크로 지표 수집기. 지표별로 ok/failed + fetched_at 을 반환하고 DB에 저장한다."""
from datetime import datetime, timedelta, timezone

import config


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def yf_fetch(symbol):
    """yfinance에서 최근 종가 2개를 받아 (현재값, 직전값)을 반환. 실패 시 예외."""
    import yfinance as yf

    hist = yf.Ticker(symbol).history(period="7d", interval="1d")
    closes = hist["Close"].dropna()
    if len(closes) < 2:
        raise ValueError("종가 데이터 부족")
    return float(closes.iloc[-1]), float(closes.iloc[-2])


def collect_one(key, fetcher=yf_fetch):
    _, symbol = config.MACRO_INDICATORS[key]
    fetched_at = now_iso()
    try:
        value, prev = fetcher(symbol)
        change = (value - prev) / prev * 100 if prev else None
        return dict(key=key, status="ok", value=value, prev_value=prev,
                    change_pct=change, error=None, fetched_at=fetched_at)
    except Exception as e:  # 실패를 숨기지 않고 기록
        return dict(key=key, status="failed", value=None, prev_value=None,
                    change_pct=None, error=f"{type(e).__name__}: {e}"[:300],
                    fetched_at=fetched_at)


def yf_closes(symbol, period="4mo"):
    """최근 일별 종가 목록(오름차순). 추세 지표용."""
    import yfinance as yf

    closes = yf.Ticker(symbol).history(period=period, interval="1d")["Close"].dropna()
    return [float(x) for x in closes]


def kospi_features(closes, days=None):
    """KOSPI 종가 -> (days 거래일 변화율 %, days 일 고점 대비 %). 데이터가 부족하면 예외."""
    days = days or config.TREND_DAYS
    if len(closes) < days + 1:
        raise ValueError(f"KOSPI 종가 {len(closes)}개뿐 ({days + 1}개 필요)")
    last = closes[-1]
    window = closes[-(days + 1):]
    return (last / window[0] - 1) * 100, (last / max(window) - 1) * 100


def vix_average(closes, days=None):
    days = days or config.TREND_DAYS
    if len(closes) < days:
        raise ValueError(f"VIX 종가 {len(closes)}개뿐 ({days}개 필요)")
    return sum(closes[-days:]) / days


def _derived_row(key, value, fetched_at, error=None):
    return dict(key=key, status="ok" if error is None else "failed", value=value, prev_value=None,
                change_pct=None, error=error, fetched_at=fetched_at)


def derive_ai(ai_snaps):
    """AI 대형주 바스켓(1일·10일·60일 고점 대비)과 메모리 프록시(10일) 지표. 종목별 스냅샷에서 계산한다."""
    fetched_at = now_iso()
    ok = {r["ticker"]: r for r in ai_snaps if r["status"] == "ok"}
    out = []
    members = [t for t, (_, g) in config.AI_TICKERS.items() if g in config.AI_BASKET_GROUPS]
    got = [ok[t] for t in members if t in ok]
    if len(got) >= config.AI_MIN_TICKERS:
        for key, field in (("AI1D", "chg1d"), ("AI10D", "chg10d"), ("AIDD", "dd60")):
            out.append(_derived_row(key, sum(r[field] for r in got) / len(got), fetched_at))
    else:
        err = f"AI 바스켓 종목 {len(got)}/{len(members)}개만 수집 (최소 {config.AI_MIN_TICKERS}개 필요)"
        out += [_derived_row(k, None, fetched_at, err) for k in ("AI1D", "AI10D", "AIDD")]
    mem = [ok[t] for t in config.MEMORY_PROXY if t in ok]
    if len(mem) >= config.MEM_MIN_TICKERS:
        out.append(_derived_row("MEM10D", sum(r["chg10d"] for r in mem) / len(mem), fetched_at))
    else:
        out.append(_derived_row("MEM10D", None, fetched_at,
                                f"메모리 프록시 {len(mem)}/{len(config.MEMORY_PROXY)}개만 수집 (최소 {config.MEM_MIN_TICKERS}개 필요)"))
    return out


def derive(history_fetcher=None, ai_snaps=None):
    """파생 추세 지표: KOSPI 10거래일 변화/고점 대비, VIX 10일 평균. 실패는 지표별로 기록한다."""
    fetch = history_fetcher or yf_closes
    fetched_at = now_iso()
    out = []
    try:
        chg, dd = kospi_features(fetch(config.KOSPI_SYMBOL))
        out += [_derived_row("KOSPI10D", chg, fetched_at), _derived_row("KOSPIDD", dd, fetched_at)]
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:300]
        out += [_derived_row("KOSPI10D", None, fetched_at, err), _derived_row("KOSPIDD", None, fetched_at, err)]
    try:
        out.append(_derived_row("VIXAVG", vix_average(fetch(config.MACRO_INDICATORS["VIX"][1])), fetched_at))
    except Exception as e:
        out.append(_derived_row("VIXAVG", None, fetched_at, f"{type(e).__name__}: {e}"[:300]))
    if ai_snaps is None:
        from collectors import ai_tracker

        ai_snaps = ai_tracker.collect(history_fetcher)
    return out + derive_ai(ai_snaps)


def collect_all(fetcher=yf_fetch, history_fetcher=None, ai_snaps=None):
    rows = [collect_one(k, fetcher) for k, (_, sym) in config.MACRO_INDICATORS.items() if sym is not None]
    return rows + derive(history_fetcher, ai_snaps)


def save(conn, rows):
    conn.executemany(
        "INSERT INTO macro_snapshots (key,status,value,prev_value,change_pct,error,fetched_at)"
        " VALUES (:key,:status,:value,:prev_value,:change_pct,:error,:fetched_at)",
        rows,
    )


def latest(conn):
    """지표별 최신 시도 + 최신 성공 값. 실패해도 마지막 성공 값은 stale 로 보여준다."""
    out = []
    for key, (name, symbol) in config.MACRO_INDICATORS.items():
        last = conn.execute(
            "SELECT * FROM macro_snapshots WHERE key=? ORDER BY fetched_at DESC, id DESC LIMIT 1",
            (key,)).fetchone()
        last_ok = conn.execute(
            "SELECT * FROM macro_snapshots WHERE key=? AND status='ok' "
            "ORDER BY fetched_at DESC, id DESC LIMIT 1", (key,)).fetchone()
        out.append(dict(
            key=key, name=name, symbol=symbol, kind="feature" if symbol is None else "change",
            unit=config.DERIVED_UNITS.get(key, ""),
            status=last["status"] if last else "none",
            error=last["error"] if last else None,
            attempted_at=last["fetched_at"] if last else None,
            value=last_ok["value"] if last_ok else None,
            change_pct=last_ok["change_pct"] if last_ok else None,
            ok_at=last_ok["fetched_at"] if last_ok else None,
            freshness=freshness(last, last_ok),
        ))
    return out


def freshness(last, last_ok, now=None):
    """fresh / stale / failed / none"""
    if last is None:
        return "none"
    if last["status"] == "failed":
        return "failed"
    now = now or datetime.now(timezone.utc)
    age = now - datetime.fromisoformat(last_ok["fetched_at"])
    return "stale" if age > timedelta(hours=config.STALE_AFTER_HOURS) else "fresh"
