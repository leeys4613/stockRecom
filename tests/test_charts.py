from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import charts as C
import config
import db
import main


def daily(n=300, start=100.0, step=0.5):
    """오늘까지 이어지는 일봉 n개(주말 포함 달력일 단순화)."""
    today = date.today()
    return [((today - timedelta(days=n - 1 - i)).isoformat(), start + step * i) for i in range(n)]


class Fetch:
    def __init__(self, rows=None, exc=None):
        self.rows, self.exc, self.calls = rows, exc, 0

    def __call__(self, sym, interval=None):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.rows


def test_spec_maps_direct_derived_and_basket_keys():
    assert C.chartable("SOX") and C.chartable("USDKRW") and C.chartable("VIX")
    assert C.spec("SOX")["symbol"] == "^SOX" and C.spec("SOX")["transform"] is None
    assert C.spec("KOSPI10D")["symbol"] == config.KOSPI_SYMBOL and C.spec("VIXAVG")["symbol"] == "^VIX"
    for k in ("AI1D", "AI10D", "AIDD", "MEM10D", "POWER10D", "NOPE"):
        assert not C.chartable(k)


def test_daily_series_slices_by_range_and_computes_stats():
    db.init_db()
    f = Fetch(daily())
    d = C.series(db.session, "SOX", 30, "1d", f)
    assert d["interval"] == "1d" and d["last"] == d["points"][-1][1] and d["low"] == d["points"][0][1] and d["high"] == d["last"]
    assert 28 <= len(d["points"]) <= 32 and d["chg"] == pytest.approx((d["last"] / d["first"] - 1) * 100) and d["stale"] is False
    assert len(C.series(db.session, "SOX", 365, "1d", f)["points"]) > len(d["points"])
    assert f.calls == 1                                                              # 같은 지표는 12시간 캐시


def test_daily_failure_falls_back_to_cached_rows_with_stale_flag(monkeypatch):
    db.init_db()
    C.series(db.session, "SOX", 90, "1d", Fetch(daily()))
    with db.session() as conn:
        conn.execute("UPDATE chart_fetch SET fetched_at='2000-01-01T00:00:00+00:00'")      # 캐시를 오래된 것으로
    d = C.series(db.session, "SOX", 90, "1d", Fetch(exc=RuntimeError("yahoo down")))
    assert d["stale"] is True and "yahoo down" in d["error"] and d["points"]
    db.init_db()
    with pytest.raises(C.ChartError):
        C.series(db.session, "USDKRW", 90, "1d", Fetch(exc=RuntimeError("yahoo down")))   # 캐시도 없으면 오류


def test_derived_series_use_same_formulas_as_the_score():
    db.init_db()
    closes = [100.0 + i for i in range(60)]
    rows = [(f"2026-01-{(i % 28) + 1:02d}-{i}", c) for i, c in enumerate(closes)]
    ch = C._kospi_change(rows)
    assert ch[0][1] == pytest.approx((closes[10] / closes[0] - 1) * 100)
    dd = C._kospi_dd(rows)
    assert all(v[1] == pytest.approx(0.0) for v in dd)                                # 계속 오르면 10일 고점 대비 0
    va = C._vix_avg(rows)
    assert va[0][1] == pytest.approx(sum(closes[:10]) / 10) and len(va) == 51
    d = C.series(db.session, "KOSPI10D", 90, "1d", Fetch(daily(80)))
    assert d["unit"] == "%" and d["chg"] is None and d["note"] and d["diff"] == pytest.approx(d["last"] - d["first"])


def test_intraday_and_weekly_use_memory_cache_and_respect_ranges():
    C._mem.clear()
    mins = [(f"11-{day:02d} {h:02d}:{m:02d}", 100.0 + i * 0.01) for i, (day, h, m) in enumerate(
        (day, h, m) for day in range(3, 8) for h in range(9, 16) for m in range(0, 60, 5))]
    f = Fetch(mins)
    d1 = C.series(db.session, "SOX", 1, "5m", f)
    d5 = C.series(db.session, "SOX", 5, "5m", f)
    assert {p[0][:5] for p in d1["points"]} == {"11-07"} and {p[0][:5] for p in d5["points"]} == {f"11-0{n}" for n in range(3, 8)}
    assert f.calls == 1 and d1["interval"] == "5m" and d1["points"][0][0].startswith("11-07")
    wk = [((date.today() - timedelta(weeks=300 - i)).isoformat(), 100.0 + i) for i in range(300)]
    w = C.series(db.session, "SOX", 1095, "1wk", Fetch(wk))
    assert 150 <= len(w["points"]) <= 160 and w["interval"] == "1wk"                  # 약 3년치 주봉


def test_invalid_requests_are_rejected():
    db.init_db()
    for key, days, interval in (("AI10D", 90, "1d"), ("SOX", 7, "1d"), ("SOX", 90, "1h"), ("SOX", 90, "5m"),
                                ("KOSPI10D", 1, "5m"), ("KOSPI10D", 1095, "1wk")):
        with pytest.raises(C.ChartError):
            C.series(db.session, key, days, interval, Fetch(daily()))


def test_routes_status_codes_and_page_markup(monkeypatch):
    db.init_db()
    monkeypatch.setattr(C, "yf_series", Fetch(daily()))
    monkeypatch.setattr(C, "yf_bars", Fetch([(f"11-07 {h:02d}:{m:02d}", 100.0 + h) for h in range(9, 16) for m in range(0, 60, 5)]))
    with TestClient(main.app) as c:
        r = c.get("/market/chart/SOX?days=90")
        assert r.status_code == 200 and r.json()["interval"] == "1d" and r.headers["cache-control"] == "no-store"
        assert c.get("/market/chart/SOX?interval=5m&days=1").json()["interval"] == "5m"
        assert c.get("/market/chart/AI10D").status_code == 404
        assert c.get("/market/chart/SOX?days=7").status_code == 400 and c.get("/market/chart/SOX?interval=1h").status_code == 400
        assert c.get("/market/chart/KOSPI10D?interval=5m&days=1").status_code == 422
        monkeypatch.setattr(C, "yf_series", Fetch(exc=RuntimeError("down")))
        with db.session() as conn:
            conn.execute("DELETE FROM price_history WHERE symbol='^TNX'")
        assert c.get("/market/chart/US10Y?days=30").status_code == 502
        html = c.get("/market").text
    assert 'data-chart="1"' in html and "분봉(5분)" in html and "주봉" in html and "/market/chart/" in html
    import re
    assert re.search(r'data-key="SOX"[^>]*data-chart="1"', html) and not re.search(r'data-key="AI10D"[^>]*data-chart', html)
    assert re.search(r'data-key="KOSPI10D"[^>]*data-daily="1"', html) and not re.search(r'data-key="SOX"[^>]*data-daily', html)
