import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
import db
import main
import pipeline
from analyzer import llm
from collectors import ai_tracker, macro
from scoring import policy
from tests.conftest import FakeProvider, fake_rss, flat_closes, macro_ok, make_rss

BASKET = [t for t, (_, g) in config.AI_TICKERS.items() if g in config.AI_BASKET_GROUPS]


# ---------- 종목별 계산 ----------
def test_metrics_from_closes_math():
    closes = [100.0] * 50 + [120.0] + [100.0] * 11            # 62개: 직전 최고 120 이후 하락
    price, c1, c10, dd = ai_tracker.metrics_from_closes(closes)
    assert price == 100.0 and c1 == 0.0 and c10 == pytest.approx((100 / 100 - 1) * 100)
    assert dd == pytest.approx((100 / 120 - 1) * 100)         # 60일 고점(120) 대비 -16.7%
    with pytest.raises(ValueError, match="필요"):
        ai_tracker.metrics_from_closes([1.0] * 5)


def test_dd_window_is_sixty_days():
    closes = [200.0] + [100.0] * 70                           # 200 은 60일 창 밖
    assert ai_tracker.metrics_from_closes(closes)[3] == 0.0


def hist(per_ticker, default=None):
    def fetch(symbol, period="4mo"):
        v = per_ticker.get(symbol, default)
        if v is None:
            raise RuntimeError(f"{symbol} down")
        return v
    return fetch


def test_collect_isolates_failures_and_records_reason():
    snaps = ai_tracker.collect(hist({"MU": None}, default=flat_closes(70)))
    by = {s["ticker"]: s for s in snaps}
    assert len(snaps) == len(config.AI_TICKERS)
    assert by["MU"]["status"] == "failed" and "MU down" in by["MU"]["error"]
    assert by["TSM"]["status"] == "ok" and by["TSM"]["chg10d"] == 0.0


# ---------- 파생 지표(AI 바스켓 / 메모리) ----------
def snaps_with(chg1d=1.0, chg10d=-5.0, dd60=-30.0, fail=()):
    out = []
    for t, (_, g) in config.AI_TICKERS.items():
        if t in fail:
            out.append(dict(ticker=t, grp=g, status="failed", error="x", fetched_at="x", price=None, chg1d=None, chg10d=None, dd60=None))
        else:
            out.append(dict(ticker=t, grp=g, status="ok", error=None, fetched_at="x", price=1.0, chg1d=chg1d, chg10d=chg10d, dd60=dd60))
    return out


def test_derive_ai_means_and_memory_proxy():
    rows = {r["key"]: r for r in macro.derive_ai(snaps_with(1.0, -5.0, -30.0))}
    assert rows["AI1D"]["value"] == 1.0 and rows["AI10D"]["value"] == -5.0 and rows["AIDD"]["value"] == -30.0
    assert rows["MEM10D"]["value"] == -5.0 and all(r["status"] == "ok" for r in rows.values())


def test_basket_only_uses_basket_groups_memory_only_uses_proxy_list():
    snaps = snaps_with(0.0, 0.0, 0.0)
    for s in snaps:                                           # 메모리 전용 종목(Nanya 등)을 크게 움직여도 바스켓은 불변
        if s["grp"] == "memory":
            s["chg10d"] = -20.0
    rows = {r["key"]: r for r in macro.derive_ai(snaps)}
    assert rows["AI10D"]["value"] == 0.0                                          # 바스켓은 영향 없음
    assert rows["MEM10D"]["value"] < -5                                           # 메모리 프록시는 반영


def test_basket_needs_minimum_tickers_else_failed_with_reason():
    many = [t for t in BASKET][: len(BASKET) - config.AI_MIN_TICKERS + 1]       # 성공 종목이 최소 미만이 되도록 실패 처리
    rows = {r["key"]: r for r in macro.derive_ai(snaps_with(fail=many))}
    assert rows["AI1D"]["status"] == "failed" and "최소" in rows["AI1D"]["error"]
    few = [t for t in config.MEMORY_PROXY][: len(config.MEMORY_PROXY) - config.MEM_MIN_TICKERS + 1]
    assert {r["key"]: r for r in macro.derive_ai(snaps_with(fail=few))}["MEM10D"]["status"] == "failed"


def test_one_failed_ticker_does_not_break_basket():
    rows = {r["key"]: r for r in macro.derive_ai(snaps_with(fail=["IREN"]))}
    assert rows["AI10D"]["status"] == "ok"


# ---------- CAPEX ----------
QS = ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"]


def series(*vals):
    """연속 5분기 CAPEX(십억 달러). 마지막 분기가 최신."""
    return list(zip(QS, vals))


def capex_rows(**quarters):
    return [(t, [(d, v * 1e9) for d, v in qs]) for t, qs in quarters.items()]


def save_capex(rows):
    db.init_db()
    with db.session() as conn:
        ai_tracker.save_capex(conn, rows)
        return ai_tracker.capex_summary(conn)


def test_capex_yoy_total_and_flags():
    s = save_capex(capex_rows(
        MSFT=[("2025-09-30", 20), ("2025-12-31", 30), ("2026-03-31", 31), ("2026-06-30", 36), ("2026-09-30", 40)],
        GOOGL=[("2025-09-30", 24), ("2025-12-31", 28), ("2026-03-31", 36), ("2026-06-30", 45), ("2026-09-30", 51)],
        AMZN=[("2025-09-30", 35), ("2025-12-31", 39), ("2026-03-31", 44), ("2026-09-30", 77)],   # 6월 분기 누락 + 급증
    ))
    per = {p["ticker"]: p for p in s["per"]}
    assert per["MSFT"]["yoy"] == pytest.approx(100.0) and per["MSFT"]["flags"] == []
    assert per["GOOGL"]["yoy"] == pytest.approx((51 / 24 - 1) * 100)
    assert "직전 분기 데이터 누락" in per["AMZN"]["flags"]                       # 이상치 표시
    assert s["total"]["n"] == 2 and s["total"]["yoy"] == pytest.approx(((40 + 51) / (20 + 24) - 1) * 100)   # AMZN 제외
    assert per["META"]["latest"] is None                                           # 데이터 없음은 그대로 표시


def test_capex_sudden_jump_flagged_without_gap():
    s = save_capex(capex_rows(MSFT=[("2025-12-31", 10), ("2026-03-31", 11), ("2026-06-30", 12), ("2026-09-30", 30)]))
    flags = {p["ticker"]: p["flags"] for p in s["per"]}["MSFT"]
    assert any("급변" in f for f in flags) and s["total"] is None


def test_capex_due_and_refresh_window():
    db.init_db()
    with db.session() as conn:
        assert ai_tracker.capex_due(conn)                                          # 한 번도 안 받음
        ai_tracker.save_capex(conn, capex_rows(MSFT=[("2026-09-30", 40)]))
        assert not ai_tracker.capex_due(conn)
        old = (datetime.now(timezone.utc) - timedelta(hours=config.CAPEX_REFRESH_HOURS + 1)).isoformat(timespec="seconds")
        conn.execute("UPDATE capex_quarters SET fetched_at=?", (old,))
        assert ai_tracker.capex_due(conn)


def test_collect_capex_isolates_failures():
    def fetch(t):
        if t == "AMZN":
            raise ValueError("no capex row")
        return [("2026-09-30", 4e10)]
    ok, failed = ai_tracker.collect_capex(fetch)
    assert {t for t, _ in ok} == set(config.CAPEX_TICKERS) - {"AMZN"} and failed[0][0] == "AMZN"


# ---------- 파이프라인 통합 ----------
def run(provider=None, history=None, capex=None, news_fetcher=None):
    db.init_db()
    return pipeline.run_once(provider or FakeProvider(_good()), macro_ok, news_fetcher or fake_rss,
                             history_fetcher=history, capex_fetcher=capex)


def _good():
    from tests.conftest import GOOD
    return GOOD


def test_pipeline_saves_tracker_snapshots_and_ai_derived_rows():
    run(history=hist({}, default=flat_closes(70, 100.0)))
    with db.session() as conn:
        n = conn.execute("SELECT COUNT(*) FROM ai_tracker_snapshots WHERE status='ok'").fetchone()[0]
        keys = {r["key"] for r in macro.latest(conn) if r["kind"] == "feature"}
    assert n == len(config.AI_TICKERS) and {"AI1D", "AI10D", "AIDD", "MEM10D"} <= keys


def test_pipeline_fetches_capex_once_per_window_and_survives_failure():
    calls = []

    def capex(t):
        calls.append(t)
        return [("2025-09-30", 2e10), ("2026-09-30", 4e10)]
    run(capex=capex)
    run(capex=capex)                                         # 20시간 안이라 다시 받지 않는다
    assert len(calls) == len(config.CAPEX_TICKERS)
    db.init_db()


def test_capex_failure_does_not_block_analysis():
    def boom(t):
        raise RuntimeError("yf down")
    assert run(capex=boom) is not None
    with db.session() as conn:
        a = pipeline.latest_analysis(conn)
    assert a["llm_status"] == "ok"


def test_ai_context_contains_tracker_groups_capex_and_reading():
    seen = {}

    class Spy(FakeProvider):
        def complete(self, prompt):
            seen["prompt"] = prompt
            return super().complete(prompt)
    run(Spy(_good()), history=hist({}, default=[100.0] * 20 + [60.0] * 50 + [50.0] * 5),   # 큰 낙폭
        capex=lambda t: [(d, v * 1e9) for d, v in series(20, 25, 30, 35, 42)])
    ctx = json.loads(seen["prompt"].split("<input>")[1].split("</input>")[0])
    tr = ctx["ai_tracker"]
    assert set(tr["groups"]) == {config.AI_GROUP_NAMES[g] for g in config.AI_GROUP_NAMES}
    assert len(tr["tickers"]) == len(config.AI_TICKERS) and tr["tickers"][0]["dd60"] < -30
    assert tr["bigtech_capex_total"]["yoy_pct"] == pytest.approx(110.0) and tr["bigtech_capex_total"]["n"] == 4
    assert "지속성" in tr["reading"]                                  # CAPEX 증가 + 주가 낙폭 → 괴리 해석


def test_reading_variants():
    db.init_db()
    with db.session() as conn:
        ai_tracker.save_capex(conn, capex_rows(MSFT=series(20, 20.5, 20.8, 21, 21)))             # 전년 대비 +5% (둔화)
        ai_tracker.save(conn, ai_tracker.collect(hist({}, default=flat_closes(70))))
        assert "둔화" in pipeline.tracker_view(conn)["reading"]
        ai_tracker.save_capex(conn, capex_rows(MSFT=series(20, 25, 30, 35, 40)))                 # +100%, 주가 평탄
        assert "신뢰가 유지" in pipeline.tracker_view(conn)["reading"]


# ---------- AI 시장 반응 / 메모리 가격 판정 ----------
AI_T = ["Microsoft, Meta slash data center spending plans as AI bubble fears grow - Reuters"]
MEM_T = ["HBM Prices Seen Up 121% as SK Hynix Buyers Shift to 8-Hi - FourWeekMBA"]


def test_new_regions_scoring_points_and_caps():
    a = llm.Analysis(summary_lines=list("abcd"), adjustment=0, risk_comment="r",
                     ai_market_effect=-3, ai_market_evidence=[AI_T[0]],
                     mem_price_effect=2, mem_price_evidence=[MEM_T[0]])
    r = policy.assess(a, {"us": [], "kr": [], "ai": AI_T, "mem": MEM_T}, config.POLICY_CAPS_DEFAULT)
    assert r["ai"]["points"] == -config.POLICY_CAPS_DEFAULT["ai"]                        # -3/3 × 상한
    assert r["mem"]["points"] == pytest.approx(round(2 / 3 * config.POLICY_CAPS_DEFAULT["mem"], 1))
    assert set(r) == {"us", "kr", "ai", "mem"}


def test_new_regions_unsupported_evidence_forces_zero():
    a = llm.Analysis(summary_lines=list("abcd"), adjustment=0, risk_comment="r",
                     mem_price_effect=3, mem_price_evidence=["Totally invented headline about DRAM prices soaring"])
    assert policy.assess(a, {"us": [], "kr": [], "ai": [], "mem": MEM_T}, config.POLICY_CAPS_DEFAULT)["mem"]["points"] == 0


def test_pipeline_applies_ai_and_memory_points_to_final_score():
    payload = dict(summary_lines=list("abcd"), adjustment=0, risk_comment="r",
                   ai_market_effect=-2, ai_market_evidence=[AI_T[0]],
                   mem_price_effect=1, mem_price_evidence=[MEM_T[0]])

    def news_fetch(url):
        if "AI%20bubble" in url or "AI+bubble" in url or "bubble" in url:
            return make_rss([(AI_T[0], "https://e.com/a", 3)])
        if "HBM" in url and "hl=en-US" in url:
            return make_rss([(MEM_T[0], "https://e.com/m", 3)])
        return make_rss()
    run(FakeProvider(payload), news_fetcher=news_fetch)
    with db.session() as conn:
        a = pipeline.latest_analysis(conn)
    assert a["policy"]["ai"]["points"] == round(-2 / 3 * config.POLICY_CAPS_DEFAULT["ai"], 1)
    assert a["policy"]["mem"]["points"] == round(1 / 3 * config.POLICY_CAPS_DEFAULT["mem"], 1)
    assert a["final_score"] == pytest.approx(round(a["base_score"] + a["policy_total"], 1), abs=0.11)


def test_english_topics_use_us_locale_and_korean_ones_do_not():
    from collectors import news
    seen = {}
    for topic in ("ai_capex", "mem_en", "mem_kr", "us_macro"):
        news.collect_one(topic, lambda u, t=topic: seen.setdefault(t, u) and make_rss())
    assert "hl=en-US" in seen["ai_capex"] and "hl=en-US" in seen["mem_en"]
    assert "hl=ko" in seen["mem_kr"] and "hl=ko" in seen["us_macro"]


# ---------- 4줄 요약 스키마 / 프롬프트 ----------
def test_summary_must_have_exactly_four_lines():
    for n in (3, 5):
        with pytest.raises(Exception):
            llm.Analysis(summary_lines=list("abcde")[:n], adjustment=0, risk_comment="r")
    assert len(llm.Analysis(summary_lines=list("abcd"), adjustment=0, risk_comment="r").summary_lines) == 4


def test_prompt_has_ai_memory_line_and_new_fields():
    p = llm.SYSTEM_PROMPT
    for needle in ("정확히 4개", "[AI·메모리]", "데이터센터 건설 속도", "ai_tracker", "ai_market_effect", "mem_price_effect",
                   "mem_price_evidence", "ai_market_evidence", "괴리"):
        assert needle in p, needle


# ---------- 가중치: AI·메모리 비중 ----------
def test_ai_memory_weight_share_is_thirty_percent():
    w = config.SCORE_WEIGHTS
    assert sum(abs(w[k]) for k in ("AI1D", "AI10D", "AIDD", "MEM10D")) == pytest.approx(0.30)
    assert sum(abs(v) for v in w.values()) == pytest.approx(1.0)
    assert all(config.SCORE_DIRECTION[k] == 1 for k in ("AI1D", "AI10D", "AIDD", "MEM10D"))
    assert config.SCORE_SATURATION_PCT["AIDD"] == 30.0           # 설정 화면 검증 범위 안이어야 저장 가능
    from weights import SAT_MAX
    assert all(v <= SAT_MAX for v in config.SCORE_SATURATION_PCT.values())


# ---------- 화면 ----------
def test_dashboard_tracker_panel_and_summary_title():
    run(history=hist({}, default=[100.0] * 20 + [60.0] * 50 + [50.0] * 5),
        capex=lambda t: [(d, v * 1e9) for d, v in series(20, 25, 30, 35, 42)])
    with TestClient(main.app) as c:
        html = c.get("/tracker").text
        home = c.get("/").text
        market = c.get("/market").text
    assert 'id="ai-tracker"' in html and "AI 투자 사이클 트래커" in html and "데이터센터 건설 속도" in html
    assert "빅테크 설비투자(CAPEX)" in html and "전년 대비 +110%" in html and "$168B" in html   # 42B × 4사
    for g in config.AI_GROUP_NAMES.values():
        assert g in html
    assert 'id="ai-table"' in html and 'id="capex-table"' in html and "<details" in html
    assert "시장이 투자의 지속성" in html                         # 규칙 기반 해석 문구
    assert "AI 요약" in home and "AI 3줄 요약" not in home and 'id="ai-tracker"' not in home   # 대시보드에는 트래커가 없다
    for name in ("AI 대형주 1일 변화", "AI 대형주 10거래일 변화", "AI 대형주 60일 고점 대비", "메모리주 10거래일 변화"):
        assert name in market


def test_dashboard_tracker_without_data_is_graceful():
    db.init_db()
    with TestClient(main.app) as c:
        html = c.get("/tracker").text
    assert 'id="ai-tracker"' in html and "CAPEX 데이터가 아직 없습니다" in html and "데이터 없음" in html


def test_policy_panel_lists_four_regions():
    payload = dict(summary_lines=list("abcd"), adjustment=0, risk_comment="r")
    run(FakeProvider(payload))
    with TestClient(main.app) as c:
        html = c.get("/").text
    for label in ("미국 정책·정치", "한국 증시 부양·자본시장 정책", "AI 투자 사이클 시장 반응", "메모리 가격 방향"):
        assert label in html


def test_settings_has_four_policy_cap_inputs_and_saves_them():
    with TestClient(main.app) as c:
        html = c.get("/settings").text
        for n in ("pol_us", "pol_kr", "pol_ai", "pol_mem"):
            assert f'name="{n}"' in html
        data = {f"imp_{k}": 10 for k in config.MACRO_INDICATORS}
        data.update({f"sat_{k}": config.SCORE_SATURATION_PCT[k] for k in config.MACRO_INDICATORS})
        data.update(note="", pol_us=5, pol_kr=3, pol_ai=7, pol_mem=8)
        r = c.post("/settings", data=data, follow_redirects=True)
        assert r.status_code == 200 and "AI ±7" in r.text and "메모리 ±8" in r.text
        r = c.post("/settings", data={**data, "pol_ai": 99})
        assert r.status_code == 400 and "AI 시장 반응 반영 상한" in r.text
