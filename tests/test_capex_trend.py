import json

import pytest
from fastapi.testclient import TestClient

import config
import db
import main
import pipeline
from analyzer import llm
from collectors import ai_tracker as at
from tests.conftest import FakeProvider, GOOD, fake_rss, macro_ok

QS = ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"]
# 실제 yfinance 분기 CAPEX(십억 달러). AMZN 은 2026-06-30 분기가 빠져 있다
REAL = {
    "MSFT": list(zip(QS, [19.39, 29.88, 30.88, 35.8, 39.37])),
    "GOOGL": list(zip(QS, [23.95, 27.85, 35.67, 44.92, 51.35])),
    "META": list(zip(QS, [18.83, 21.38, 19.0, 30.12, 33.94])),
    "AMZN": [("2025-09-30", 35.09), ("2025-12-31", 39.52), ("2026-03-31", 44.2), ("2026-09-30", 77.26)],
}


def seed(data=REAL):
    db.init_db()
    with db.session() as conn:
        at.save_capex(conn, [(t, [(d, v * 1e9) for d, v in rows]) for t, rows in data.items()])
        return at.capex_summary(conn)


def by(trend):
    return {t["label"]: t for t in trend}


# ---------- 순수 계산 ----------
def test_growth_trend_constant_growth_is_similar():
    t = at.growth_trend([100, 110, 121, 133.1, 146.41])                    # 매 분기 +10%
    assert [x["label"] for x in t] == ["분기", "2분기", "3분기"]
    assert by(t)["분기"]["now"] == pytest.approx(10.0) and by(t)["분기"]["prev"] == pytest.approx(10.0)
    assert all(x["verdict"] == "유사" and abs(x["delta"]) < 1e-9 for x in t)


def test_growth_trend_acceleration_and_deceleration():
    acc = by(at.growth_trend([100, 110, 130, 170, 240]))
    assert acc["분기"]["now"] == pytest.approx((240 / 170 - 1) * 100) and acc["분기"]["prev"] == pytest.approx((170 / 130 - 1) * 100)
    assert acc["분기"]["verdict"] == "가속" and acc["2분기"]["now"] == pytest.approx((240 / 130 - 1) * 100)
    assert acc["2분기"]["prev"] == pytest.approx((170 / 110 - 1) * 100)
    mild = at.growth_trend([100, 110, 130, 160, 200])[0]            # 증가율 +23% → +25%: 변화 +1.9%p 는 임계 안이라 '유사'
    assert mild["verdict"] == "유사" and 0 < mild["delta"] < config.CAPEX_QOQ_DECEL_PP
    dec = by(at.growth_trend([100, 150, 200, 230, 250]))
    assert dec["분기"]["now"] == pytest.approx((250 / 230 - 1) * 100) and dec["분기"]["verdict"] == "둔화"
    assert dec["분기"]["delta"] == pytest.approx((250 / 230 - 230 / 200) * 100)
    assert all(x["verdict"] == "둔화" for x in dec.values())


def test_growth_trend_needs_enough_quarters_per_horizon():
    assert at.growth_trend([100, 110]) == []                                # 직전 증가율을 못 구함
    assert [x["label"] for x in at.growth_trend([100, 110, 130])] == ["분기"]
    assert [x["label"] for x in at.growth_trend([100, 110, 130, 150])] == ["분기", "2분기"]
    assert [x["label"] for x in at.growth_trend([100, 110, 130, 150, 170])] == ["분기", "2분기", "3분기"]
    assert at.growth_trend([]) == [] and at.growth_trend([5]) == []


def test_growth_trend_skips_zero_bases():
    assert at.growth_trend([0, 100, 120]) == []                             # 0 에서의 증가율은 정의되지 않는다


def test_threshold_boundaries_with_margin():
    pp = config.CAPEX_QOQ_DECEL_PP

    def verdict_for(delta_pp):
        # 직전 +20% → 이번 (20 + delta)% 가 되도록 3개 값을 만든다 (부동소수점 경계 오차를 피해 경계의 양쪽으로 여유를 둔다)
        return at.growth_trend([100.0, 120.0, 120.0 * (1 + (20 + delta_pp) / 100)])[0]["verdict"]
    assert verdict_for(-pp + 0.2) == "유사" and verdict_for(-pp - 0.2) == "둔화"
    assert verdict_for(pp - 0.2) == "유사" and verdict_for(pp + 0.2) == "가속"


@pytest.mark.parametrize("verdicts,expect", [
    (["둔화", "둔화", "둔화"], "모든 기간에서 증가율 둔화"),
    (["가속", "가속"], "모든 기간에서 증가율 가속"),
    (["둔화", "둔화", "가속"], "대체로 증가율 둔화"),
    (["가속", "가속", "둔화"], "대체로 증가율 가속"),
    (["둔화", "가속"], "혼조"),
    (["유사", "유사", "유사"], "혼조"),
    (["둔화", "유사", "유사"], "대체로 증가율 둔화"),
])
def test_trend_overall(verdicts, expect):
    assert expect in at.trend_overall([dict(verdict=v) for v in verdicts])
    assert at.trend_overall([]) is None


def test_consecutive_tail_stops_at_missing_quarter():
    items = [("2025-03-31", 1), ("2025-06-30", 2), ("2026-03-31", 3), ("2026-06-30", 4), ("2026-09-30", 5)]
    assert [d for d, _ in at._consecutive_tail(items)] == ["2026-03-31", "2026-06-30", "2026-09-30"]
    assert at._consecutive_tail([]) == []
    assert [d for d, _ in at._consecutive_tail(items[-1:])] == ["2026-09-30"]


# ---------- 요약(합계/종목별) ----------
def test_total_trend_matches_hand_calculation_on_real_data():
    s = seed()
    t = s["total"]
    sums = [62.17, 79.11, 85.55, 110.84, 124.66]                           # MSFT+GOOGL+META (AMZN 은 이상치로 제외)
    assert t["n"] == 3 and t["qoq"] == pytest.approx((sums[4] / sums[3] - 1) * 100)
    tr = by(t["trend"])
    assert tr["분기"]["now"] == pytest.approx(12.46, abs=0.01) and tr["분기"]["prev"] == pytest.approx(29.56, abs=0.01)
    assert tr["분기"]["verdict"] == "둔화"
    assert tr["2분기"]["now"] == pytest.approx(45.71, abs=0.01) and tr["2분기"]["prev"] == pytest.approx(40.11, abs=0.01)
    assert tr["2분기"]["verdict"] == "가속"                                 # 분기별 요철: 2분기 기준으로는 오히려 가속
    assert tr["3분기"]["now"] == pytest.approx(57.58, abs=0.01) and tr["3분기"]["prev"] == pytest.approx(78.29, abs=0.01)
    assert tr["3분기"]["verdict"] == "둔화"
    assert "대체로 증가율 둔화" in t["overall"]


def test_flagged_ticker_has_no_trend_and_is_excluded():
    s = seed()
    per = {p["ticker"]: p for p in s["per"]}
    assert "직전 분기 데이터 누락" in per["AMZN"]["flags"] and per["AMZN"]["trend"] == [] and per["AMZN"]["trend_overall"] is None
    assert per["GOOGL"]["trend"] and per["GOOGL"]["flags"] == []


def test_per_ticker_trend_values():
    per = {p["ticker"]: p for p in seed()["per"]}
    g = by(per["GOOGL"]["trend"])
    assert g["분기"]["now"] == pytest.approx((51.35 / 44.92 - 1) * 100) and g["분기"]["prev"] == pytest.approx((44.92 / 35.67 - 1) * 100)
    assert all(x["verdict"] == "둔화" for x in g.values()) and "모든 기간에서 증가율 둔화" in per["GOOGL"]["trend_overall"]
    assert "혼조" in per["META"]["trend_overall"] or "대체로" in per["META"]["trend_overall"]


def test_trend_uses_only_consecutive_recent_quarters():
    data = {"MSFT": [("2024-12-31", 10.0), ("2025-03-31", 11.0), ("2026-03-31", 30.0), ("2026-06-30", 33.0), ("2026-09-30", 36.0)]}
    s = seed(data)
    assert [x["label"] for x in s["per"][0]["trend"]] == ["분기"]          # 2025-03 이전은 끊겨서 사용하지 않는다(3개 연속)


def test_total_needs_common_consecutive_quarters():
    s = seed({"MSFT": list(zip(QS, [20, 25, 30, 35, 40])), "GOOGL": list(zip(QS[2:], [30, 35, 40]))})   # GOOGL 은 3분기만
    t = s["total"]
    assert t["n"] == 2 and [x["label"] for x in t["trend"]] == ["분기"]    # 공통 구간이 3분기라 분기 기준만


# ---------- 해석 / AI 입력 / 화면 ----------
def tracker_view_with(data=REAL, dd=-30.0):
    db.init_db()
    with db.session() as conn:
        at.save_capex(conn, [(t, [(d, v * 1e9) for d, v in rows]) for t, rows in data.items()])
        at.save(conn, at.collect(lambda s, p="4mo": [100.0] * 30 + [100.0 * (1 + dd / 100)] * 41))
        return pipeline.tracker_view(conn)


def test_reading_mentions_overall_trend_and_deceleration():
    r = tracker_view_with()["reading"]
    assert "직전(+30%)보다 낮아졌습니다" in r and "증가율 추세는 '대체로 증가율 둔화'" in r


def test_ai_context_has_growth_trend_for_total_and_each_ticker():
    seen = {}

    class Spy(FakeProvider):
        def complete(self, prompt):
            seen["p"] = prompt
            return super().complete(prompt)
    db.init_db()
    pipeline.run_once(Spy(GOOD), macro_ok, fake_rss,
                      capex_fetcher=lambda t: [(d, v * 1e9) for d, v in REAL[t]])
    ctx = json.loads(seen["p"].split("<input>")[1].split("</input>")[0])["ai_tracker"]
    tot = ctx["bigtech_capex_total"]
    assert tot["growth_trend_overall"].startswith("대체로 증가율 둔화")
    assert [g["horizon"] for g in tot["growth_trend"]] == ["분기", "2분기", "3분기"]
    assert {g["horizon"]: g["verdict"] for g in tot["growth_trend"]} == {"분기": "둔화", "2분기": "가속", "3분기": "둔화"}
    per = {c["t"]: c for c in ctx["bigtech_capex"]}
    assert per["GOOGL"]["growth_trend_overall"] == "모든 기간에서 증가율 둔화" and per["AMZN"]["growth_trend"] == []
    assert "growth_trend" in ctx["capex_note"]


def test_prompt_asks_ai_to_judge_capex_growth_trend():
    for needle in ("growth_trend", "growth_trend_overall", "둔화 중인지 가속 중인지"):
        assert needle in llm.SYSTEM_PROMPT


def test_dashboard_shows_trend_line_arrows_and_table():
    db.init_db()
    pipeline.run_once(FakeProvider(GOOD), macro_ok, fake_rss,
                      capex_fetcher=lambda t: [(d, v * 1e9) for d, v in REAL[t]])
    with TestClient(main.app) as c:
        html = c.get("/tracker").text
    assert 'id="capex-trend"' in html and "대체로 증가율 둔화" in html
    assert "분기 기준 +12%" in html and "직전 +30%" in html and "↘" in html and "↗" in html
    assert 'id="capex-trend-table"' in html and "<th>2분기</th>" in html and "<th>3분기</th>" in html
    assert "증가율 추세 = 1·2·3분기 전 대비" in html
    row_amzn = html[html.index("AMZN", html.index('id="capex-trend-table"')):][:400]
    assert 'class="mut">-</span>' in row_amzn                              # 이상치 종목은 추세 칸이 '-'


def test_dashboard_without_enough_quarters_hides_trend_line():
    db.init_db()
    pipeline.run_once(FakeProvider(GOOD), macro_ok, fake_rss,
                      capex_fetcher=lambda t: [("2026-06-30", 3.5e10), ("2026-09-30", 4e10)])   # 2분기뿐: 증가율의 추세를 못 구함
    with TestClient(main.app) as c:
        html = c.get("/tracker").text
    assert 'id="capex-trend"' not in html and "직전 분기 대비" in html
