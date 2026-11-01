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


def collect_all(fetcher=yf_fetch):
    return [collect_one(k, fetcher) for k in config.MACRO_INDICATORS]


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
            key=key, name=name, symbol=symbol,
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
