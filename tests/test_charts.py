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


# ---------- 종목 차트 (관심종목·강세 종목) ----------
def seed_master(*codes):
    db.init_db()
    names = {"005930": ("삼성전자", "KS"), "247540": ("에코프로비엠", "KQ")}
    with db.session() as conn:
        for c in codes:
            n, m = names[c]
            conn.execute("INSERT OR REPLACE INTO stock_master (code,name,market,name_norm,choseong,source,updated_at) VALUES (?,?,?,?,?,?,?)",
                         (c, n, m, n, n, "t", "x"))


def test_stock_series_only_for_known_stocks_and_uses_own_symbol():
    seed_master("005930")
    seen = []
    f = lambda sym, interval=None: seen.append(sym) or daily()  # noqa: E731
    d = C.stock_series(db.session, "005930", 90, "1d", fetcher=f)
    assert d["name"] == "삼성전자" and d["code"] == "005930" and d["market"] == "KS"
    assert seen == ["005930.KS"] and "index" not in d and d["levels"] is None
    with pytest.raises(C.ChartError):
        C.stock_series(db.session, "999999", 90, "1d", fetcher=f)                    # 종목 목록에 없는 코드는 조회하지 않는다
    assert seen == ["005930.KS"]


def test_compare_overlays_market_index_rebased_to_the_stock_start():
    seed_master("005930", "247540")
    rows = {"005930.KS": daily(300, 100.0, 1.0), "^KS11": daily(300, 5000.0, 5.0), "247540.KQ": daily(300, 100.0, 1.0), "^KQ11": daily(300, 700.0, 1.0)}
    f = lambda sym, interval=None: rows[sym]  # noqa: E731
    d = C.stock_series(db.session, "005930", 90, "1d", compare=True, fetcher=f)
    ix = d["index"]
    assert ix["name"] == "KOSPI" and ix["points"][0][1] == pytest.approx(d["points"][0][1])        # 시작점이 같게 맞춰진다
    assert len(ix["points"]) == len(d["points"]) and ix["chg"] > 0 and ix["stock_chg"] > 0
    assert ix["stock_chg"] == pytest.approx((d["points"][-1][1] / d["points"][0][1] - 1) * 100)
    assert C.stock_series(db.session, "247540", 90, "1d", compare=True, fetcher=f)["index"]["name"] == "KOSDAQ"   # 코스닥은 KOSDAQ 과 비교
    bad = lambda sym, interval=None: rows[sym] if sym == "005930.KS" else (_ for _ in ()).throw(RuntimeError("idx down"))  # noqa: E731
    with db.session() as conn:
        conn.execute("DELETE FROM price_history WHERE symbol='^KS11'")
        conn.execute("DELETE FROM chart_fetch WHERE symbol='^KS11'")
    d2 = C.stock_series(db.session, "005930", 90, "1d", compare=True, fetcher=bad)
    assert "index" not in d2 and d2["points"]                                          # 지수 선을 못 받아도 종목 차트는 나온다


def test_levels_come_from_latest_stock_view_for_daily_only():
    import json

    import stockviews
    seed_master("005930")
    lv = dict(supports=[dict(price=95000), dict(price=90000)], resistances=[dict(price=110000)])
    with db.session() as conn:
        stockviews.save(conn, [stockviews.make_row("005930", None, dict(
            crowd_effect=0, crowd_score=50, crowd_evidence=[], crowd_note=None, sector_effect=0, sector_score=50, sector_note=None,
            score=50, comment="c"), [], {"levels": lv})])
    f = lambda sym, interval=None: daily()  # noqa: E731
    d = C.stock_series(db.session, "005930", 90, "1d", levels=True, fetcher=f)
    assert d["levels"] == dict(supports=[95000, 90000], resistances=[110000])
    assert C.stock_series(db.session, "005930", 90, "1d", levels=False, fetcher=f)["levels"] is None
    wk = lambda sym, interval=None: [((date.today() - timedelta(weeks=300 - i)).isoformat(), 100.0 + i) for i in range(300)]  # noqa: E731
    assert C.stock_series(db.session, "005930", 1095, "1wk", levels=True, fetcher=wk)["levels"] is None   # 일봉이 아니면 선을 그리지 않는다


def test_stock_chart_route_and_card_markup(monkeypatch):
    seed_master("005930")
    monkeypatch.setattr(C, "yf_series", Fetch(daily()))
    with TestClient(main.app) as c:
        r = c.get("/stock/chart/005930?days=90")
        assert r.status_code == 200 and r.json()["name"] == "삼성전자" and r.headers["cache-control"] == "no-store"
        assert c.get("/stock/chart/999999").status_code == 404
        assert c.get("/stock/chart/005930?days=7").status_code == 400 and c.get("/stock/chart/005930?interval=1h").status_code == 400
        assert c.get("/stock/chart/005930?compare=1").json()["index"]["name"] == "KOSPI"
        js = c.get("/static/chart.js")
    assert js.status_code == 200 and "KMSChart" in js.text and "/stock/chart/" in js.text and "지수와 비교" in js.text


def test_cards_on_watchlist_and_screener_have_chart_details():
    from tests import test_screener as ts
    from tests import test_stock_views as sv
    sv.seed_watchlist("005930")
    ts.seed_master()
    ts.S.run(db.session, ts.fake_chunk, lambda s: ts.idx_bars(), lambda s: 2e12, delay=0, ai=False)
    with TestClient(main.app) as c:
        wl = c.get("/watchlist").text
        sc = c.get("/screener").text
    assert 'class="stock-chart" data-code="005930" data-compare="0" data-levels="1"' in wl and '<script src="/static/chart.js">' in wl
    assert 'class="stock-chart" data-code="000001" data-compare="1" data-levels="0"' in sc and '<script src="/static/chart.js">' in sc
    assert "매도" not in wl


def test_last_completed_trading_day_rules():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import calendar_kr as K
    z = ZoneInfo("Asia/Seoul")
    at = lambda s: K.last_completed_trading_day(datetime.fromisoformat(s).replace(tzinfo=z))  # noqa: E731
    assert at("2026-11-10 16:00") == date(2026, 11, 10)         # 장 마감 후: 오늘(화) 일봉이 확정돼야 한다
    assert at("2026-11-10 14:00") == date(2026, 11, 9)          # 장중: 직전 거래일
    assert at("2026-11-11 00:05") == date(2026, 11, 10)         # 자정 직후: 전날
    assert at("2026-11-14 12:00") == date(2026, 11, 13)         # 주말: 금요일


def test_stock_chart_flags_when_latest_daily_bar_is_missing():
    seed_master("005930")
    old = [((date.today() - timedelta(days=125 - i)).isoformat(), 100.0 + i) for i in range(65)]      # 마지막 일봉이 약 2달 전
    d = C.stock_series(db.session, "005930", 365, "1d", fetcher=lambda sym, interval=None: old)
    assert d["behind"] is True and d["expected"] and d["as_of"] < d["expected"]
    with db.session() as conn:                                              # 12시간 캐시를 비워 새 데이터를 받게 한다
        conn.execute("DELETE FROM price_history")
        conn.execute("DELETE FROM chart_fetch")
    fresh = C.stock_series(db.session, "005930", 365, "1d", fetcher=lambda sym, interval=None: daily())
    assert fresh["behind"] is False or fresh["behind"] is None
    mins = [(f"11-07 {h:02d}:{m:02d}", 100.0 + h) for h in range(9, 16) for m in range(0, 60, 5)]
    assert "behind" not in C.stock_series(db.session, "005930", 1, "5m", fetcher=lambda sym, interval=None: mins)   # 분봉은 판단하지 않는다
