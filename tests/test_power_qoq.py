import pytest
from fastapi.testclient import TestClient

import config
import db
import main
import pipeline
import weights as W
from analyzer import llm
from collectors import ai_tracker, macro
from scoring import engine
from tests.conftest import GOOD, FakeProvider, fake_rss, flat_closes, macro_ok, make_rss

QS = ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"]


def cap(**by_ticker):
    return [(t, [(d, v * 1e9) for d, v in zip(QS, vals)]) for t, vals in by_ticker.items()]


def summary(**by_ticker):
    db.init_db()
    with db.session() as conn:
        ai_tracker.save_capex(conn, cap(**by_ticker))
        return ai_tracker.capex_summary(conn)


def view_with(capex_vals, dd=-30.0):
    """CAPEX 시리즈 + 모든 AI 바스켓 종목의 60일 낙폭 dd 로 tracker_view 를 만든다."""
    db.init_db()
    with db.session() as conn:
        ai_tracker.save_capex(conn, cap(MSFT=capex_vals))
        snaps = ai_tracker.collect(lambda s, p="4mo": [100.0] * 30 + [100.0 * (1 + dd / 100)] * 40 + [100.0 * (1 + dd / 100)])
        ai_tracker.save(conn, snaps)
        return pipeline.tracker_view(conn)


# ---------- 분기 대비(QoQ) ----------
def test_qoq_per_ticker_total_and_history():
    s = summary(MSFT=(20, 30, 31, 36, 40), GOOGL=(24, 28, 36, 45, 51))
    per = {p["ticker"]: p for p in s["per"]}
    assert per["MSFT"]["qoq"] == pytest.approx((40 / 36 - 1) * 100) and per["GOOGL"]["qoq"] == pytest.approx((51 / 45 - 1) * 100)
    t = s["total"]
    assert t["n"] == 2 and t["qoq"] == pytest.approx((91 / 81 - 1) * 100)                 # (40+51)/(36+45)
    assert [h["period"] for h in t["qoq_history"]] == ["2026-03-31", "2026-06-30", "2026-09-30"]
    assert [round(h["qoq"], 1) for h in t["qoq_history"]] == [15.5, 20.9, 12.3]            # 합계 67/58, 81/67, 91/81
    assert t["yoy"] == pytest.approx(((40 + 51) / (20 + 24) - 1) * 100)                    # 전년 대비는 보조로 유지


def test_qoq_total_excludes_flagged_ticker():
    s = summary(MSFT=(20, 30, 31, 36, 40), AMZN=(35, 39, 44, 0.0001, 77))                  # AMZN: 직전 분기 급변 → 이상치
    assert any("급변" in f for f in {p["ticker"]: p["flags"] for p in s["per"]}["AMZN"])
    assert s["total"]["n"] == 1 and s["total"]["qoq"] == pytest.approx((40 / 36 - 1) * 100)


def test_no_qoq_without_previous_quarter():
    db.init_db()
    with db.session() as conn:
        ai_tracker.save_capex(conn, [("MSFT", [("2026-09-30", 4e10)])])
        s = ai_tracker.capex_summary(conn)
    assert s["total"] is None and {p["ticker"]: p for p in s["per"]}["MSFT"]["qoq"] is None


@pytest.mark.parametrize("vals,dd,expect", [
    ((20, 25, 30, 35, 40), -30, "직전 분기보다 +14% 늘고 있지만"),         # 증가 지속 + 주가 낙폭 → 지속성 의심
    ((20, 25, 30, 35, 40), -3, "신뢰가 유지"),                              # 증가 지속 + 주가 유지
    ((20, 20.5, 20.8, 21, 21.5), -3, "둔화"),                              # QoQ 0~5%
    ((20, 25, 30, 35, 33), -3, "줄었습니다"),                              # 감소
])
def test_reading_uses_qoq(vals, dd, expect):
    r = view_with(vals, dd)["reading"]
    assert expect in r


def test_reading_flags_deceleration_vs_previous_quarter():
    r = view_with((20, 28, 36, 42, 45), -3)["reading"]               # QoQ +17% → +7%
    assert "직전(+17%)보다 낮아졌습니다" in r


def test_ai_context_carries_qoq_and_history():
    import json
    seen = {}

    class Spy(FakeProvider):
        def complete(self, prompt):
            seen["p"] = prompt
            return super().complete(prompt)
    db.init_db()
    pipeline.run_once(Spy(GOOD), macro_ok, fake_rss, capex_fetcher=lambda t: [(d, v * 1e9) for d, v in zip(QS, (20, 25, 30, 35, 42))])
    ctx = json.loads(seen["p"].split("<input>")[1].split("</input>")[0])["ai_tracker"]
    t = ctx["bigtech_capex_total"]
    assert t["qoq_pct"] == 20.0 and len(t["qoq_history"]) == 3 and "분기 대비(QoQ)" in ctx["capex_note"]
    assert all("qoq_pct" in c for c in ctx["bigtech_capex"])


def test_dashboard_shows_qoq_as_primary_and_yoy_as_secondary():
    db.init_db()
    pipeline.run_once(FakeProvider(GOOD), macro_ok, fake_rss, capex_fetcher=lambda t: [(d, v * 1e9) for d, v in zip(QS, (20, 25, 30, 35, 42))])
    with TestClient(main.app) as c:
        html = c.get("/tracker").text
    assert "직전 분기 대비 +20%" in html and "최근 분기 흐름" in html and "전년 대비 +110%" in html
    assert "<th>분기 대비</th>" in html and "전년 +110%" in html


# ---------- 전력·전기 인프라 ----------
def snaps_for(power_us_dd=-10.0, power_kr_dd=-50.0, others_dd=-10.0, n_us=6):
    out = []
    us_seen = 0
    for t, (_, g) in config.AI_TICKERS.items():
        dd = others_dd
        if g == "power":
            if t.endswith(".KS"):
                dd = power_kr_dd
            else:
                us_seen += 1
                if us_seen > n_us:
                    continue
                dd = power_us_dd
        out.append(dict(ticker=t, grp=g, status="ok", error=None, fetched_at=ai_tracker.now_iso(), price=10.0, chg1d=0.0, chg10d=-5.0, dd60=dd))
    return out


def build_reading(**kw):
    db.init_db()
    with db.session() as conn:
        ai_tracker.save(conn, snaps_for(**kw))
        return pipeline.tracker_view(conn)["build_reading"]


def test_power_group_has_us_and_korean_members():
    power = [t for t, (_, g) in config.AI_TICKERS.items() if g == "power"]
    assert {"VRT", "ETN", "GEV", "CEG", "VST", "PWR"} <= set(power)
    assert {"267260.KS", "010120.KS", "298040.KS", "034020.KS", "103590.KS"} <= set(power)
    assert "power" in config.AI_GROUP_NAMES and "power" not in config.AI_BASKET_GROUPS     # 기존 바스켓 지표 구성은 불변


@pytest.mark.parametrize("us_dd,expect", [(-5, "비교적 견조"), (-15, "중간 단계"), (-30, "크게 약세")])
def test_build_reading_thresholds_use_us_power_stocks(us_dd, expect):
    r = build_reading(power_us_dd=us_dd, power_kr_dd=-55.0)
    assert expect in r and f"평균 {us_dd:+d}%" in r
    assert "한국 전력기기주 평균 -55%" in r and "코스피 급락 영향" in r                       # 한국은 분리해서 언급


def test_build_reading_not_driven_by_korean_stocks():
    assert "비교적 견조" in build_reading(power_us_dd=-5, power_kr_dd=-70.0)                # 한국 급락에도 미국 기준 판정


def test_build_reading_needs_enough_us_tickers():
    assert build_reading(n_us=2) is None


def test_derive_power10d_mean_and_minimum():
    rows = {r["key"]: r for r in macro.derive_ai(snaps_for())}
    assert rows["POWER10D"]["status"] == "ok" and rows["POWER10D"]["value"] == -5.0
    few = [s for s in snaps_for()]
    for s in few:
        if s["grp"] == "power":
            s.update(status="failed", price=None, chg10d=None)
    r = {r["key"]: r for r in macro.derive_ai(few)}["POWER10D"]
    assert r["status"] == "failed" and "최소" in r["error"]


# ---------- 가중치 0 = 점수 미반영 ----------
def test_zero_weight_indicator_does_not_change_score_or_missing():
    w = {"SOX": 1.0, "POWER10D": 0.0}
    base = engine.compute_score({"SOX": 3.0, "POWER10D": None}, w)
    big = engine.compute_score({"SOX": 3.0, "POWER10D": -50.0}, w)
    assert base["score"] == big["score"] == 100.0 and base["missing"] == [] and base["coverage"] == 1.0


def test_config_power_is_reference_only():
    assert config.SCORE_WEIGHTS["POWER10D"] == 0 and sum(abs(v) for v in config.SCORE_WEIGHTS.values()) == pytest.approx(1.0)
    assert config.POWER_MIN_TICKERS >= 1


def test_zero_weight_new_key_creates_no_new_weight_version():
    import json
    db.init_db()
    with db.session() as conn:
        w = json.loads(conn.execute("SELECT weights FROM weight_sets WHERE id=1").fetchone()[0])
        w.pop("POWER10D")                                              # POWER10D 도입 전의 가중치 세트(다른 지표는 전부 있음)
        conn.execute("UPDATE weight_sets SET weights=? WHERE id=1", (json.dumps(w),))
    db.init_db()
    with db.session() as conn:
        assert [x["id"] for x in W.list_all(conn)] == [1]              # 점수가 같으므로 성과 표본을 쪼개는 새 버전을 만들지 않는다


def test_pipeline_runs_with_set_lacking_power_key_and_still_stores_indicator():
    import json
    db.init_db()
    with db.session() as conn:
        w = json.loads(conn.execute("SELECT weights FROM weight_sets WHERE id=1").fetchone()[0])
        w.pop("POWER10D")
        conn.execute("UPDATE weight_sets SET weights=? WHERE id=1", (json.dumps(w),))
    pipeline.run_once(FakeProvider(GOOD), macro_ok, fake_rss)
    with db.session() as conn:
        a = pipeline.latest_analysis(conn)
        keys = {r["key"] for r in macro.latest(conn)}
    assert a["llm_status"] == "ok" and a["coverage"] == pytest.approx(1.0) and "POWER10D" in keys


def test_settings_shows_power_with_zero_importance_and_can_activate_it():
    with TestClient(main.app) as c:
        html = c.get("/settings").text
        assert 'name="imp_POWER10D" value="0.0"' in html and "전력 인프라 10거래일 변화" in html
        data = {f"imp_{k}": (10 if k != "POWER10D" else 20) for k in config.MACRO_INDICATORS}      # 리밸런싱: 전력 인프라 활성화
        data.update({f"sat_{k}": config.SCORE_SATURATION_PCT[k] for k in config.MACRO_INDICATORS})
        data.update(note="전력 반영", pol_us=10, pol_kr=6, pol_ai=6, pol_mem=6)
        assert c.post("/settings", data=data, follow_redirects=True).status_code == 200
        with db.session() as conn:
            assert W.get_active(conn)["weights"]["POWER10D"] > 0


# ---------- 뉴스/프롬프트/화면 ----------
def test_ai_region_includes_power_news_with_right_locale():
    from collectors import news
    assert {"ai_power_en", "ai_power_kr"} <= set(config.POLICY_TOPICS["ai"])
    seen = {}
    for t in ("ai_power_en", "ai_power_kr"):
        news.collect_one(t, lambda u, t=t: seen.setdefault(t, u) and make_rss())
    assert "hl=en-US" in seen["ai_power_en"] and "hl=ko" in seen["ai_power_kr"]


def test_prompt_and_context_mention_construction_signal():
    assert "construction_reading" in llm.SYSTEM_PROMPT and "power 그룹" in llm.SYSTEM_PROMPT
    import json
    seen = {}

    class Spy(FakeProvider):
        def complete(self, prompt):
            seen["p"] = prompt
            return super().complete(prompt)
    db.init_db()
    pipeline.run_once(Spy(GOOD), macro_ok, fake_rss)
    ctx = json.loads(seen["p"].split("<input>")[1].split("</input>")[0])
    assert "weight 0" in ctx["algorithm"]["indicator_notes"] and "construction_reading" in ctx["ai_tracker"]
    assert config.AI_GROUP_NAMES["power"] in ctx["ai_tracker"]["groups"]


def test_dashboard_shows_construction_signal_and_power_group():
    db.init_db()
    with db.session() as conn:
        ai_tracker.save(conn, snaps_for(power_us_dd=-30.0))
    with TestClient(main.app) as c:
        html = c.get("/tracker").text
        market = c.get("/market").text
    assert 'id="build-reading"' in html and "건설 속도 신호" in html and "전력·전기 인프라" in html
    assert "전력 인프라 10거래일 변화 (참고)" in market
