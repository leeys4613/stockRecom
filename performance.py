"""예측 기록의 사후 검증: KOSPI 1/5거래일 수익률을 채우고 적중률·Brier 를 계산한다.

기준 종가: 분석 시점(KST)에 이미 알려진 마지막 KOSPI 종가.
  - 장 마감 후(16:00~) 분석 → 당일 종가 / 그 전 분석 → 직전 거래일 종가
  (장중 분석은 당일 오전 움직임이 일부 반영된 상태라는 한계가 있다.)
점수는 확률이 아니다. Brier 는 "점수/100 을 확률처럼 썼을 때 보정이 되는가"를 보는 진단용이다.
"""
from bisect import bisect_right
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import calendar_kr
import config

KST = ZoneInfo(config.TIMEZONE)
AFTER_CLOSE = time(16, 0)
MIN_DAYS_FOR_MEANING = 30  # 이보다 적은 서로 다른 기준일이면 "표본 부족"
KOSPI_SYMBOL = "^KS11"


def kospi_closes():
    """[(date, close)] 오름차순. 실패 시 예외."""
    import yfinance as yf

    hist = yf.Ticker(KOSPI_SYMBOL).history(period="1y", interval="1d")
    closes = hist["Close"].dropna()
    if len(closes) < 7:
        raise ValueError("KOSPI 종가 데이터 부족")
    return [(ts.date(), float(v)) for ts, v in closes.items()]


def reference_index(created_at_iso, dates):
    """분석 시점에 알려진 마지막 종가의 인덱스. 아직 알 수 없으면 None."""
    t = datetime.fromisoformat(created_at_iso).astimezone(KST)
    d = t.date()
    after_close = t.time() >= AFTER_CLOSE
    if after_close and calendar_kr.is_trading_day(d) and (not dates or dates[-1] < d):
        return None  # 당일 종가가 아직 데이터에 없음 → 잘못된 기준을 잡지 말고 대기
    cutoff = d if after_close else d - timedelta(days=1)
    idx = bisect_right(dates, cutoff) - 1
    return idx if idx >= 0 else None


def compute_outcome(created_at_iso, closes):
    """dict(ref_*, d1_*, d5_*) (아직 없는 값은 None). 기준을 못 잡으면 None."""
    dates = [d for d, _ in closes]
    i = reference_index(created_at_iso, dates)
    if i is None:
        return None
    ref_date, ref_close = closes[i]
    out = dict(ref_date=ref_date.isoformat(), ref_close=ref_close)
    for tag, n in (("d1", 1), ("d5", 5)):
        if i + n < len(closes):
            d, c = closes[i + n]
            out.update({f"{tag}_date": d.isoformat(), f"{tag}_close": c,
                        f"{tag}_ret": (c / ref_close - 1) * 100})
        else:
            out.update({f"{tag}_date": None, f"{tag}_close": None, f"{tag}_ret": None})
    return out


def fill_outcomes(conn, fetch=None):
    """결과가 비어 있는 예측의 사후 수익률을 채운다. 실패해도 예외 대신 상태를 반환."""
    pending = conn.execute(
        "SELECT a.id, a.created_at FROM analyses a LEFT JOIN outcomes o ON o.analysis_id=a.id "
        "WHERE a.final_score IS NOT NULL AND (o.analysis_id IS NULL OR o.d5_ret IS NULL) "
        "ORDER BY a.id").fetchall()
    if not pending:
        return dict(status="ok", updated=0, error=None)
    try:
        closes = (fetch or kospi_closes)()
    except Exception as e:
        return dict(status="failed", updated=0, error=f"{type(e).__name__}: {e}"[:300])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    updated = 0
    for row in pending:
        o = compute_outcome(row["created_at"], closes)
        if o is None:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO outcomes (analysis_id,ref_date,ref_close,d1_date,d1_close,"
            "d1_ret,d5_date,d5_close,d5_ret,updated_at) VALUES "
            "(:analysis_id,:ref_date,:ref_close,:d1_date,:d1_close,:d1_ret,:d5_date,:d5_close,"
            ":d5_ret,:updated_at)", dict(o, analysis_id=row["id"], updated_at=now))
        updated += 1
    return dict(status="ok", updated=updated, error=None)


def _band(score):
    return ("green" if score >= config.SIGNAL_GREEN else
            "yellow" if score >= config.SIGNAL_YELLOW else "red")


def summarize(conn, horizon="d1", weight_set_id=None):
    """horizon: 'd1' | 'd5'. 결과가 채워진 예측만 집계한다. weight_set_id 로 가중치 버전별 집계."""
    col = f"{horizon}_ret"
    sql = (f"SELECT a.final_score AS score, o.ref_date AS ref_date, o.{col} AS ret "
           f"FROM analyses a JOIN outcomes o ON o.analysis_id=a.id "
           f"WHERE a.final_score IS NOT NULL AND o.{col} IS NOT NULL")
    args = ()
    if weight_set_id is not None:
        sql += " AND a.weight_set_id=?"
        args = (weight_set_id,)
    rows = conn.execute(sql, args).fetchall()
    n = len(rows)
    res = dict(horizon=horizon, n=n, n_days=len({r["ref_date"] for r in rows}),
               hit_rate=None, base_up_rate=None, brier=None, brier_baseline=None, bands={})
    if n == 0:
        res["enough"] = False
        return res
    ups = [1 if r["ret"] > 0 else 0 for r in rows]
    up_rate = sum(ups) / n
    res["base_up_rate"] = up_rate
    res["hit_rate"] = sum((r["score"] >= 50) == bool(u) for r, u in zip(rows, ups)) / n
    res["brier"] = sum((r["score"] / 100 - u) ** 2 for r, u in zip(rows, ups)) / n
    res["brier_baseline"] = up_rate * (1 - up_rate)  # 항상 평균 상승률만 말하는 기준 모델
    for band in ("green", "yellow", "red"):
        sel = [(r, u) for r, u in zip(rows, ups) if _band(r["score"]) == band]
        res["bands"][band] = dict(
            n=len(sel),
            avg_ret=(sum(r["ret"] for r, _ in sel) / len(sel)) if sel else None,
            up_rate=(sum(u for _, u in sel) / len(sel)) if sel else None)
    res["enough"] = res["n_days"] >= MIN_DAYS_FOR_MEANING
    return res


def recent(conn, limit=20):
    return [dict(r) for r in conn.execute(
        "SELECT a.id, a.created_at, a.final_score, a.base_score, a.adjustment, a.llm_status, "
        "o.ref_date, o.d1_ret, o.d5_ret FROM analyses a LEFT JOIN outcomes o "
        "ON o.analysis_id=a.id WHERE a.final_score IS NOT NULL ORDER BY a.id DESC LIMIT ?",
        (limit,))]
