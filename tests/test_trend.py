import json

import pytest
from fastapi.testclient import TestClient

import config
import db
import main
import pipeline
import weights as W
from collectors import macro
from tests.conftest import GOOD, FakeProvider, fake_rss, flat_closes, macro_ok

NEW_KEYS = {"KOSPI10D", "KOSPIDD", "VIXAVG", "AI1D", "AI10D", "AIDD", "MEM10D"}   # 점수에 반영되는 새 지표(가중치 > 0)
DERIVED_ALL = NEW_KEYS | {"POWER10D"}                                               # + 참고용(가중치 0) 파생 지표


# ---------- 계산식 ----------
def test_kospi_features_rising_crash_and_plateau():
    chg, dd = macro.kospi_features([100 + i for i in range(12)])            # 꾸준한 상승
    assert chg == pytest.approx((111 / 101 - 1) * 100) and dd == 0.0      # 최근 11개(101~111): 10거래일 전 대비
    closes = [110, 112, 115, 113, 108, 105, 100, 98, 99, 101, 100]            # 고점 115 → 폭락 → 반등
    chg, dd = macro.kospi_features(closes)
    assert chg == pytest.approx((100 / 110 - 1) * 100) and dd == pytest.approx((100 / 115 - 1) * 100)
    assert macro.kospi_features(flat_closes(30)) == (0.0, 0.0)


def test_kospi_window_is_ten_trading_days_not_more():
    # 11개 이전의 고점(200)은 10일 창 밖이라 낙폭에 반영되지 않는다
    closes = [200] + [100] * 11
    assert macro.kospi_features(closes)[1] == 0.0
    assert macro.kospi_features([200] + [100] * 10)[1] == pytest.approx(-50.0)


def test_features_need_enough_data():
    with pytest.raises(ValueError, match="11개 필요"):
        macro.kospi_features([100] * 10)
    with pytest.raises(ValueError, match="10개 필요"):
        macro.vix_average([20] * 9)
    assert macro.vix_average([10] * 5 + [30] * 5) == 20.0
    assert macro.vix_average([99] * 20 + [20] * 10) == 20.0                  # 최근 10개만


# ---------- 수집: 지표별 실패 격리 ----------
def hist(kospi=None, vix=None):
    def fetch(symbol):
        if symbol == config.KOSPI_SYMBOL:
            if kospi is None:
                raise RuntimeError("kospi down")
            return kospi
        if vix is None:
            raise RuntimeError("vix down")
        return vix
    return fetch


def test_derive_all_ok():
    rows = {r["key"]: r for r in macro.derive(hist([100 + i for i in range(12)], [25] * 12))}
    assert set(rows) == DERIVED_ALL and all(r["status"] == "ok" for r in rows.values())
    assert rows["VIXAVG"]["value"] == 25 and rows["KOSPIDD"]["value"] == 0.0
    assert all(r["change_pct"] is None for r in rows.values())      # 파생 지표는 전일 변동률이 없다


def test_derive_failures_are_isolated():
    rows = {r["key"]: r for r in macro.derive(hist(None, [25] * 12))}
    assert rows["KOSPI10D"]["status"] == rows["KOSPIDD"]["status"] == "failed" and "kospi down" in rows["KOSPIDD"]["error"]
    assert rows["VIXAVG"]["status"] == "ok"
    rows = {r["key"]: r for r in macro.derive(hist([100] * 12, None))}
    assert rows["VIXAVG"]["status"] == "failed" and rows["KOSPI10D"]["status"] == "ok"


def test_collect_all_includes_derived_and_base_failure_does_not_block_them():
    def base_fail(symbol):
        raise RuntimeError("down")
    rows = macro.collect_all(base_fail, hist([100] * 12, [20] * 12))
    by = {r["key"]: r for r in rows}
    assert len(rows) == len(config.MACRO_INDICATORS)
    assert by["SOX"]["status"] == "failed" and by["KOSPI10D"]["status"] == "ok"


def test_config_consistency_for_derived_indicators():
    assert all(config.MACRO_INDICATORS[k][1] is None for k in NEW_KEYS)                 # 심볼 없음 = 파생
    assert set(config.SCORE_WEIGHTS) == set(config.MACRO_INDICATORS) == set(config.SCORE_SATURATION_PCT)
    assert sum(abs(w) for w in config.SCORE_WEIGHTS.values()) == pytest.approx(1.0)
    assert config.SCORE_DIRECTION["VIXAVG"] == -1 and config.SCORE_DIRECTION["KOSPI10D"] == 1 and config.SCORE_DIRECTION["KOSPIDD"] == 1
    assert config.SCORE_CENTER["VIXAVG"] == 20.0 and config.TREND_DAYS == 10


# ---------- 점수 입력/영향 ----------
def rows_for(**vals):
    out = []
    for k, (name, sym) in config.MACRO_INDICATORS.items():
        out.append(dict(key=k, name=name, kind="feature" if sym is None else "change", unit="", value=vals.get(k, 1.0),
                        change_pct=None if sym is None else vals.get(k, 1.0), freshness="fresh"))
    return out


def test_score_inputs_feature_vs_change_and_staleness():
    rows = rows_for(VIXAVG=25.0, KOSPI10D=-6.0, SOX=2.0)
    rows[0]["freshness"] = "stale"
    got = pipeline.score_inputs(rows)
    assert got["VIXAVG"] == 5.0                      # 값 - 중립값(20)
    assert got["KOSPI10D"] == -6.0                    # 값 그대로
    assert got["SOX"] == 2.0                          # 전일 변동률
    assert got[rows[0]["key"]] is None                # 최신이 아니면 결측


def run(history, provider=None):
    db.init_db()
    return pipeline.run_once(provider or FakeProvider(GOOD), macro_ok, fake_rss, history_fetcher=history)


def base_score():
    with db.session() as conn:
        return pipeline.latest_analysis(conn)["base_score"]


def test_crash_regime_scores_lower_than_calm_regime_with_same_daily_moves():
    run(hist([100 + i for i in range(12)], [14] * 12))             # KOSPI 상승·VIX 평온
    calm = base_score()
    run(hist([115, 113, 108, 105, 100, 98, 99, 101, 100, 95, 92, 90], [32] * 12))   # 폭락 국면·VIX 높음
    crash = base_score()
    assert crash < calm - 8        # 하루 지표가 같아도 국면(추세·수준)이 점수에 반영된다


def test_high_vix_average_alone_lowers_score():
    run(hist(flat_closes(12, 5000), [14] * 12))
    low = base_score()
    run(hist(flat_closes(12, 5000), [34] * 12))
    assert base_score() < low


def test_trend_indicator_failure_is_reported_missing_not_hidden():
    run(hist(None, [20] * 12))
    with db.session() as conn:
        a = pipeline.latest_analysis(conn)
    assert {"KOSPI10D", "KOSPIDD"} <= set(a["missing"]) and a["coverage"] < 1.0


# ---------- 마이그레이션: 기존(사용자 조정) 비중 유지 ----------
OLD10 = {"SOX": 0.35, "USDKRW": -0.20, "US10Y": -0.10, "USDJPY": 0.05, "USDCNH": -0.05, "BTC": 0.05,
         "WTI": -0.05, "VIX": -0.05, "SPX": 0.05, "DXY": -0.05}


def seed_legacy_10_indicator_db(policy='{"us": 4.0, "kr": 2.0}'):
    db.init_db()
    with db.session() as conn:
        conn.execute("UPDATE weight_sets SET label='v2 조정본', weights=?, saturation=?, policy=? WHERE id=1",
                     (json.dumps(OLD10), json.dumps({k: 2.0 for k in OLD10}), policy))   # id 1 을 10개 지표 구버전으로 되돌림
        conn.execute("INSERT INTO analyses (created_at,llm_status,final_score,weight_set_id) VALUES "
                     "('2026-11-08T00:00:00+00:00','ok',52.0,1)")


def test_migration_adds_version_keeping_relative_weights_and_policy():
    seed_legacy_10_indicator_db()
    db.init_db()
    db.init_db()                                                  # 멱등
    with db.session() as conn:
        ws = W.list_all(conn)
        assert [w["id"] for w in ws] == [2, 1] and ws[1]["label"] == "v2 조정본"
        new, old = ws[0], ws[1]
        assert "지표 확장" in new["label"] and set(new["weights"]) == {k for k, v in config.SCORE_WEIGHTS.items() if v != 0}
        share = sum(abs(config.SCORE_WEIGHTS[k]) for k in NEW_KEYS)                       # 0.25
        for k in OLD10:                                                                   # 기존 상대 비중 유지(균일 축소)
            assert new["weights"][k] == pytest.approx(OLD10[k] * (1 - share), abs=1e-5)
        for k in NEW_KEYS:
            assert new["weights"][k] == config.SCORE_WEIGHTS[k]
        assert sum(abs(w) for w in new["weights"].values()) == pytest.approx(1.0, abs=1e-4)
        assert new["policy"] == {"us": 4.0, "kr": 2.0, "ai": config.POLICY_CAPS_DEFAULT["ai"],
                                 "mem": config.POLICY_CAPS_DEFAULT["mem"]}              # 정책 상한 이어받고 새 영역은 기본값
        assert new["saturation"]["SOX"] == 2.0 and new["saturation"]["VIXAVG"] == config.SCORE_SATURATION_PCT["VIXAVG"]
        assert old["weights"] == OLD10                                                    # 이전 버전 불변
        assert conn.execute("SELECT weight_set_id FROM analyses").fetchone()[0] == 1      # 과거 분석은 이전 버전


def test_fresh_db_has_no_extra_version():
    db.init_db()
    db.init_db()
    with db.session() as conn:
        assert [w["id"] for w in W.list_all(conn)] == [1]


def test_migrated_version_is_usable_by_pipeline_and_settings():
    seed_legacy_10_indicator_db()
    db.init_db()
    run(hist(flat_closes(12, 5000), [20] * 12))
    with db.session() as conn:
        a = pipeline.latest_analysis(conn)
    assert a["weight_set_id"] == 2 and a["coverage"] == pytest.approx(1.0)
    with TestClient(main.app) as c:
        html = c.get("/settings").text
        assert 'name="imp_KOSPI10D"' in html and 'name="imp_VIXAVG"' in html


# ---------- 화면 / AI 입력 ----------
def test_dashboard_shows_trend_rows_with_units():
    run(hist([115, 113, 108, 105, 100, 98, 99, 101, 100, 95, 92, 90], [28] * 12))
    with TestClient(main.app) as c:
        html = c.get("/market").text
    for name in ("KOSPI 10거래일 변화", "KOSPI 10일 고점 대비", "VIX 10일 평균 (20=중립)"):
        assert name in html
    assert "-20.35%" in html and "추세" in html            # 90/113-1, 파생 지표는 전일비 칸에 '추세'


def test_ai_input_marks_feature_indicators_and_explains_them():
    seen = {}

    class Spy(FakeProvider):
        def complete(self, prompt):
            seen["prompt"] = prompt
            return super().complete(prompt)
    run(hist(flat_closes(12, 5000), [20] * 12), Spy(GOOD))
    ctx = json.loads(seen["prompt"].split("<input>")[1].split("</input>")[0])
    kinds = {m["key"]: m["kind"] for m in ctx["macro"]}
    assert all(kinds[k] == "feature" for k in NEW_KEYS) and kinds["SOX"] == "change"
    notes = ctx["algorithm"]["indicator_notes"]
    assert "KOSPI10D" in notes["kind=feature"] and "과대평가" in notes["kind=feature"]
    from analyzer import llm
    assert "kind=feature" in llm.SYSTEM_PROMPT
