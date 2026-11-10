import math
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import analog as A
import config
import db
import history
import main


def days(n, start=date(2005, 1, 3)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def synth(n=700, seed=1):
    """충격(급락+VIX 급등)이 주기적으로 오고 그 뒤 반등하는 합성 KOSPI/VIX."""
    ds = days(n)
    ks, vx, c, v = [], [], 1000.0, 15.0
    for i, d in enumerate(ds):
        phase = i % 120
        if 50 <= phase < 60:
            c *= 0.985; v = min(45.0, v + 2.5)        # 급락 + 공포
        elif 60 <= phase < 80:
            c *= 1.006; v = max(14.0, v - 1.2)        # 반등
        else:
            c *= 1.0005; v = 15.0 + math.sin(i / 7)
        ks.append((d, c)); vx.append((d, v))
    return ks, vx


def test_features_use_only_past_vix_and_compute_trend_fields():
    ks, vx = synth()
    rows = A.build_rows(ks, vx, start="2005-01-01")
    r = rows[40]
    i = r["i"]
    assert r["k10"] == pytest.approx((ks[i][1] / ks[i - 10][1] - 1) * 100)
    assert r["kdd"] <= 0 + 1e-9
    j = [d for d, _ in vx].index(r["date"])
    assert r["vix"] == vx[j - 1][1]                                  # 같은 날이 아니라 직전 거래일의 VIX (미래 정보 차단)
    assert r["f5"] == pytest.approx((ks[i + 5][1] / ks[i][1] - 1) * 100)
    assert rows[-1]["f5"] is None and rows[-1]["f20"] is None        # 이후 데이터가 없으면 비워 둔다


def test_start_date_and_warmup_are_respected():
    ks, vx = synth()
    rows = A.build_rows(ks, vx, start="2006-06-01")
    assert rows and rows[0]["date"] >= "2006-06-01"
    assert A.build_rows(ks[:5], vx[:5]) == []


def test_backtest_groups_levels_and_episodes():
    ks, vx = synth()
    rows = A.build_rows(ks, vx, start="2005-01-01")
    bt, base = A.backtest(rows)
    assert [r["level"] for r in bt] == [lv for lv, _ in A.LEVELS]
    fear = next(r for r in bt if r["level"] in ("fear",) )
    stress = [r for r in bt if r["level"] in ("fear", "extreme_fear")]
    assert sum(r["days"] for r in stress) > 0                         # 충격 구간이 신호로 잡힌다
    for r in bt:
        assert r["episodes"] <= r["days"]
        if r["days"]:
            assert r["ep"][5]["n"] <= r["episodes"]
    assert base[5]["n"] > 0 and base[5]["up"] > 0


def test_stress_signal_is_followed_by_rebound_in_synthetic_data():
    ks, vx = synth()
    rows = A.build_rows(ks, vx, start="2005-01-01")
    sel = [r for r in rows if r["level"] in ("fear", "extreme_fear") and r["f20"] is not None]
    assert sel and A.dist([r["f20"] for r in sel])["median"] > 0      # 합성 데이터에서 반등이 나오게 만들었으므로 그대로 잡혀야 한다


def test_episodes_merge_consecutive_days():
    rows = [dict(i=i) for i in (1, 2, 3, 4, 30, 31, 80)]
    assert [r["i"] for r in A._episodes(rows, sep=10)] == [1, 30, 80]


def test_nearest_picks_similar_separated_days_and_distribution():
    ks, vx = synth()
    rows = A.build_rows(ks, vx, start="2005-01-01")
    cur = dict(vix=40.0, vixavg=30.0, k10=-12.0, kdd=-12.0)
    near, summ = A.nearest(rows, cur, k=5, sep=10)
    assert len(near) == 5
    idx = sorted(c["i"] for c in near)
    assert all(b - a > 10 for a, b in zip(idx, idx[1:]))              # 서로 충분히 떨어진 날
    assert all(c["vix"] > 20 for c in near)                           # 충격 구간과 가까운 날을 골랐다
    assert summ[5]["n"] >= 1 and set(summ[5]) >= {"mean", "median", "up", "p10", "p90"}
    assert near == sorted(near, key=lambda c: c["date"], reverse=True)


def test_dist_percentiles_and_empty():
    d = A.dist([1, 2, 3, 4, 5, None])
    assert d["n"] == 5 and d["median"] == 3 and d["up"] == 100 and d["p10"] == pytest.approx(1.4) and d["p90"] == pytest.approx(4.6)
    assert A.dist([None]) == {"n": 0}


def fake_fetcher(calls=None):
    ks, vx = synth()

    def f(sym, period):
        if calls is not None:
            calls.append((sym, period))
        return ks if sym == config.ANALOG_SYMBOLS["KOSPI"] else vx
    return f


def test_history_refresh_full_then_incremental_and_failure_isolated():
    db.init_db()
    calls = []
    out = history.refresh(db.session, fake_fetcher(calls))
    assert out["KOSPI"] == 700 and out["VIX"] == 700 and {p for _, p in calls} == {"max"}
    with db.session() as conn:
        assert len(history.load(conn, "KOSPI")) == 700
        last = history.load(conn, "KOSPI")[-1][0]
    assert last >= "2007-01-01"
    calls.clear()
    # 마지막 데이터가 오래됐으면(합성 데이터는 2000년대) 다시 전체를 받는다
    history.refresh(db.session, fake_fetcher(calls))
    assert {p for _, p in calls} == {"max"}

    def boom(sym, period):
        raise RuntimeError("down")
    out = history.refresh(db.session, boom)
    assert "down" in out["KOSPI"] and "down" in out["VIX"]
    with db.session() as conn:
        assert len(history.load(conn, "KOSPI")) == 700                   # 실패해도 기존 데이터 유지


def test_page_without_data_and_with_data():
    db.init_db()
    with TestClient(main.app) as c:
        html = c.get("/analog").text
        assert "장기 일봉 데이터가 아직 없습니다" in html and "데이터 갱신" in html
    history.refresh(db.session, fake_fetcher())
    with TestClient(main.app) as c:
        html = c.get("/analog").text
    assert 'id="backtest"' in html and 'id="analog-table"' in html and "과거 분포이며 예측이 아닙니다" in html
    assert 'data-level="extreme_fear"' in html and "표본 부족(30 미만)" in html and "과적합" in html
    assert "매도" not in html


def test_refresh_route_schedules_background_job(monkeypatch):
    calls = []
    monkeypatch.setattr(history, "refresh", lambda f, *a: calls.append(1))
    with TestClient(main.app) as c:
        assert c.post("/analog/refresh", follow_redirects=False).status_code == 303
    assert calls == [1]
