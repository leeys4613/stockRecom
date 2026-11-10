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
    assert 'id="analog-summary"' in html and 'id="analog-table"' in html and "과거 분포이며 예측이 아닙니다" in html
    assert 'id="backtest"' not in html and "공포·환희 신호는 과거에" not in html and 'data-level' not in html     # 신호 등급별 백테스트는 뺐다
    assert "매도" not in html


def test_refresh_route_schedules_background_job(monkeypatch):
    calls = []
    monkeypatch.setattr(history, "refresh", lambda f, *a: calls.append(1))
    with TestClient(main.app) as c:
        assert c.post("/analog/refresh", follow_redirects=False).status_code == 303
    assert calls == [1]


def test_verdict_compares_up_rate_to_baseline_and_withholds_on_small_samples():
    base = dict(n=6000, up=57.0)
    mk = lambda up, n=40: dict(n=n, up=up)  # noqa: E731
    assert A.verdict(mk(66), base)["label"] == "평소보다 좋았음" and A.verdict(mk(66), base)["cls"] == "fresh"
    assert A.verdict(mk(48), base)["label"] == "평소보다 나빴음" and A.verdict(mk(48), base)["cls"] == "failed"
    assert A.verdict(mk(60), base)["label"] == "평소와 비슷"
    assert A.verdict(mk(57 + A.VERDICT_GAP), base)["label"] == "평소보다 좋았음"           # 경계값은 포함
    assert A.verdict(mk(90, n=A.VERDICT_MIN_N - 1), base)["label"].startswith("표본 적음")   # 표본이 적으면 좋/나쁨을 말하지 않는다
    assert A.verdict(dict(n=0), base)["label"] == "표본 없음"


def test_page_uses_plain_language_with_glance_summary():
    db.init_db()
    history.refresh(db.session, fake_fetcher())
    with TestClient(main.app) as c:
        html = c.get("/analog").text
    assert 'id="how-to-read"' in html and "오른 비율" in html and "나빴던 경우" in html and "보통 결과" in html
    assert "평소" in html and ("평소보다" in html or "평소와 비슷" in html or "표본 적음" in html)
    assert "일주일 뒤" in html and "한 달 뒤" in html and 'class="rate"' in html
    assert "중앙값" not in html and "에피소드" not in html and "하위10%" not in html          # 통계 용어를 쓰지 않는다
    assert "매도" not in html
