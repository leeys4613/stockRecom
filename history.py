"""장기 일봉 캐시(KOSPI, VIX). 과거 유사 국면 분석(/analog)에 쓴다. 처음엔 전체 이력, 이후엔 최근 구간만 덧붙인다."""
import logging
import threading
from datetime import date, datetime, timedelta

import config

log = logging.getLogger("history")
_lock = threading.Lock()


def yf_history(symbol, period):
    import yfinance as yf

    s = yf.Ticker(symbol).history(period=period, interval="1d")["Close"].dropna()
    return [(d.strftime("%Y-%m-%d"), float(c)) for d, c in zip(s.index, s)]


def refresh(conn_factory, fetcher=None):
    """반환: {이름: 저장한 행 수 또는 오류 문자열}. 이미 실행 중이면 None."""
    if not _lock.acquire(blocking=False):
        return None
    try:
        fetcher = fetcher or yf_history
        out = {}
        for name, sym in config.ANALOG_SYMBOLS.items():
            try:
                with conn_factory() as conn:
                    last = conn.execute("SELECT MAX(date) FROM price_history WHERE symbol=?", (sym,)).fetchone()[0]
                stale = last is None or datetime.strptime(last, "%Y-%m-%d").date() < date.today() - timedelta(days=20)
                rows = fetcher(sym, "max" if stale else "3mo")
                with conn_factory() as conn:
                    conn.executemany("INSERT OR REPLACE INTO price_history (symbol,date,close) VALUES (?,?,?)",
                                     [(sym, d, c) for d, c in rows])
                out[name] = len(rows)
            except Exception as e:
                out[name] = f"{type(e).__name__}: {e}"[:200]
        return out
    finally:
        _lock.release()


def load(conn, name):
    sym = config.ANALOG_SYMBOLS[name]
    return [(r["date"], r["close"]) for r in conn.execute(
        "SELECT date, close FROM price_history WHERE symbol=? ORDER BY date", (sym,))]
