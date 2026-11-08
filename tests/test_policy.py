import json

import pytest
from fastapi.testclient import TestClient

import config
import db
import main
import pipeline
import weights as W
from analyzer import llm
from scoring import policy
from tests.conftest import FakeProvider, make_rss, macro_ok

US_T = ["트럼프, 반도체 관세 25% 부과 확정…삼성·SK하이닉스 직격 - 한국경제", "미 정부 셧다운 우려 확산 - 연합뉴스"]
KR_T = ["정부, 밸류업 세제 지원 확정…배당소득 분리과세 25% 시행 - 매일경제"]
CAPS = {"us": 10.0, "kr": 6.0}


def an(**kw):
    base = dict(summary_lines=["a", "b", "c", "d"], adjustment=0, risk_comment="r")
    base.update(kw)
    return llm.Analysis(**base)


# ---------- 환산/검증 ----------
def test_points_are_level_over_three_times_cap():
    a = an(us_policy_effect=-3, us_policy_evidence=[US_T[0]], kr_policy_effect=2, kr_policy_evidence=[KR_T[0]])
    r = policy.assess(a, {"us": US_T, "kr": KR_T}, CAPS)
    assert r["us"]["points"] == -10.0 and r["kr"]["points"] == 4.0          # -3/3*10, 2/3*6
    assert policy.total(r) == -6.0
    assert r["us"]["level_text"] == "매우 부정" and r["kr"]["level_text"] == "긍정"


def test_partial_level_scaling():
    a = an(us_policy_effect=-1, us_policy_evidence=[US_T[1]])
    assert policy.assess(a, {"us": US_T, "kr": []}, CAPS)["us"]["points"] == pytest.approx(-3.3)


def test_unsupported_evidence_forces_zero():
    a = an(us_policy_effect=-3, us_policy_evidence=["AI가 지어낸 존재하지 않는 헤드라인입니다"])
    r = policy.assess(a, {"us": US_T, "kr": []}, CAPS)["us"]
    assert r["effect"] == 0 and r["points"] == 0 and "근거" in r["note"] and r["evidence"] == []


def test_no_evidence_or_no_headlines_forces_zero():
    assert policy.assess(an(us_policy_effect=2), {"us": US_T, "kr": []}, CAPS)["us"]["points"] == 0
    r = policy.assess(an(kr_policy_effect=3, kr_policy_evidence=[KR_T[0]]), {"us": [], "kr": []}, CAPS)["kr"]
    assert r["points"] == 0 and "기사가 없어" in r["note"]


def test_evidence_matching_rules():
    trimmed = "트럼프, 반도체 관세 25% 부과 확정…삼성·SK하이닉스 직격"           # 출처 꼬리표를 뺀 부분 문자열은 인정
    ev = policy.supported_evidence([trimmed, "관세", "  트럼프,  반도체 관세 25% 부과 확정…삼성·SK하이닉스 직격 ",
                                    "존재하지 않는 헤드라인 문장입니다"], US_T)
    assert ev == [trimmed]          # 짧은 문자열("관세")·없는 문장 제외, 공백만 다른 중복은 한 번만


def test_cap_zero_or_missing_means_no_policy_effect():
    a = an(us_policy_effect=-3, us_policy_evidence=[US_T[0]])
    assert policy.assess(a, {"us": US_T, "kr": []}, {})["us"]["points"] == 0
    assert policy.assess(a, {"us": US_T, "kr": []}, {"us": 0, "kr": 0})["us"]["points"] == 0


def test_schema_bounds_and_defaults():
    with pytest.raises(Exception):
        an(us_policy_effect=4)
    with pytest.raises(Exception):
        an(kr_policy_effect=-4)
    with pytest.raises(Exception):
        an(us_policy_evidence=["a", "b", "c", "d"])
    a = an()                                  # 필드를 빼먹어도 0(영향 없음)
    assert a.us_policy_effect == 0 and a.kr_policy_evidence == []
    with pytest.raises(Exception):
        an(adjustment=config.LLM_MAX_ADJUSTMENT + 1)   # 일반 보정은 ±5 로 축소


def test_prompt_mentions_policy_rules():
    for needle in ("policy_news", "us_policy_effect", "kr_policy_effect", "글자 그대로", "중복 반영"):
        assert needle in llm.SYSTEM_PROMPT


# ---------- 가중치 세트의 정책 상한 ----------
def test_validate_policy():
    assert W.validate_policy({"us": "10", "kr": 6}) == {"us": 10.0, "kr": 6.0, **{k: config.POLICY_CAPS_DEFAULT[k] for k in ("ai", "mem")}}   # ai/mem 은 입력이 없으면 기본값
    assert W.validate_policy({"us": 1, "kr": 2, "ai": 3, "mem": 4}) == {"us": 1.0, "kr": 2.0, "ai": 3.0, "mem": 4.0}
    for bad in ({"us": 21, "kr": 1}, {"us": -1, "kr": 1}, {"us": "x", "kr": 1}, {"us": "nan", "kr": 1}, {"us": 1}, None):
        with pytest.raises(W.WeightError):
            W.validate_policy(bad)


def test_new_version_inherits_active_policy_when_not_given():
    db.init_db()
    keys = list(config.MACRO_INDICATORS)
    with db.session() as conn:
        W.create_version(conn, {k: 10 for k in keys}, {k: 2 for k in keys}, "a", {"us": 4, "kr": 2})
        nid = W.create_version(conn, {k: 10 for k in keys}, {k: 2 for k in keys}, "b")
        assert W.get(conn, nid)["policy"] == {"us": 4.0, "kr": 2.0, "ai": config.POLICY_CAPS_DEFAULT["ai"], "mem": config.POLICY_CAPS_DEFAULT["mem"]}


def test_fresh_db_v1_includes_new_indicators_and_policy():
    db.init_db()
    with db.session() as conn:
        v1 = W.get_active(conn)
    assert v1["policy"] == config.POLICY_CAPS_DEFAULT and {"ai", "mem"} <= set(v1["policy"])
    assert {"VIX", "SPX", "DXY"} <= set(v1["weights"])
    assert sum(abs(w) for w in v1["weights"].values()) == pytest.approx(1.0)
    assert config.SCORE_DIRECTION["VIX"] == -1 and config.SCORE_DIRECTION["SPX"] == 1 and config.SCORE_DIRECTION["DXY"] == -1


# ---------- 파이프라인 ----------
def policy_payload(**over):
    p = dict(summary_lines=["a", "b", "c", "d"], adjustment=2, risk_comment="r",
             us_policy_effect=-2, us_policy_evidence=[US_T[0]],
             kr_policy_effect=1, kr_policy_evidence=[KR_T[0]])
    p.update(over)
    return p


def rss_for(topic_titles):
    def fetch(url):
        for key, titles in topic_titles.items():
            if key in url:
                return make_rss([(t, f"https://e.com/{i}", 3) for i, t in enumerate(titles)])
        return make_rss()
    return fetch


def run(provider, news_fetcher=None):
    db.init_db()
    return pipeline.run_once(provider, macro_ok, news_fetcher or rss_for({"%ED%8A%B8%EB%9F%BC%ED%94%84": US_T}))


def test_final_score_is_base_plus_policy_plus_adjustment_and_saved():
    def news_fetch(url):                     # 정책 주제만 정책 기사 반환
        if "%EB%B0%B8%EB%A5%98%EC%97%85" in url:     # '밸류업'
            return make_rss([(t, "https://e.com/k", 3) for t in KR_T])
        if "%ED%8A%B8%EB%9F%BC%ED%94%84" in url:     # '트럼프'
            return make_rss([(t, "https://e.com/u", 3) for t in US_T])
        return make_rss()
    run(FakeProvider(policy_payload()), news_fetch)
    with db.session() as conn:
        a = pipeline.latest_analysis(conn)
    exp = round(a["base_score"] - 6.7 + 2.0 + 2, 1)     # us -2/3*10=-6.7, kr +1/3*6=+2.0, adj +2
    assert a["final_score"] == pytest.approx(min(100, max(0, exp)), abs=0.11)
    assert a["policy_us"] == -6.7 and a["policy_kr"] == 2.0
    assert a["policy"]["us"]["evidence"] == [US_T[0]] and a["policy_total"] == pytest.approx(-4.7)


def test_policy_news_separated_from_general_news_in_llm_input():
    seen = {}

    class Spy(FakeProvider):
        def complete(self, prompt):
            seen["prompt"] = prompt
            return super().complete(prompt)
    run(Spy(policy_payload(us_policy_effect=0, us_policy_evidence=[], kr_policy_effect=0, kr_policy_evidence=[])))
    body = seen["prompt"].split("<input>")[1]
    ctx = json.loads(body.split("</input>")[0])
    assert set(ctx["policy_news"]) == {"us", "kr", "ai", "mem"}
    assert "미국 정책·정치" not in ctx["news"] and "한국 증시 부양·자본시장 정책" not in ctx["news"]
    assert {"VIX", "SPX", "DXY"} <= {m["key"] for m in ctx["macro"]}


def test_fabricated_evidence_in_pipeline_gets_zero_policy():
    run(FakeProvider(policy_payload(us_policy_evidence=["AI가 지어낸 헤드라인 문장입니다 정말로"], us_policy_effect=-3)))
    with db.session() as conn:
        a = pipeline.latest_analysis(conn)
    assert a["policy_us"] == 0.0 and a["policy"]["us"]["effect"] == 0 and "근거" in a["policy"]["us"]["note"]


def test_llm_failure_gives_zero_policy_and_rule_score():
    run(FakeProvider(exc=RuntimeError("limit")))
    with db.session() as conn:
        a = pipeline.latest_analysis(conn)
    assert a["policy_total"] == 0.0 and a["final_score"] == a["base_score"] and a["llm_status"] == "failed"


def test_legacy_weight_set_without_policy_caps_applies_no_policy():
    db.init_db()
    keys = list(config.MACRO_INDICATORS)
    with db.session() as conn:      # 정책 상한이 없는 구 버전을 적용 중인 상황
        conn.execute("UPDATE weight_sets SET policy=NULL")
    pipeline.run_once(FakeProvider(policy_payload()), macro_ok, rss_for({"%ED%8A%B8%EB%9F%BC%ED%94%84": US_T}))
    with db.session() as conn:
        a = pipeline.latest_analysis(conn)
    assert a["policy_us"] == 0.0 and a["policy_kr"] == 0.0


# ---------- 화면 ----------
def client():
    return TestClient(main.app)


def test_dashboard_shows_policy_panel_with_evidence():
    run(FakeProvider(policy_payload(kr_policy_effect=0, kr_policy_evidence=[])),
        rss_for({"%ED%8A%B8%EB%9F%BC%ED%94%84": US_T}))
    with client() as c:
        html = c.get("/").text
    assert 'id="policy"' in html and "정책·시장 반응" in html and "근거 보기" in html
    assert "트럼프, 반도체 관세 25% 부과 확정" in html and "상한 ±10" in html
    assert "입력 헤드라인에서 근거를 확인하지 못한 판정은 0으로 처리" in html


def test_dashboard_macro_table_includes_vix_spx_dxy():
    run(FakeProvider(policy_payload()))
    with client() as c:
        html = c.get("/").text
    for name in ("VIX 공포지수", "S&amp;P 500", "달러인덱스"):
        assert name in html


def test_settings_has_policy_caps_and_validates():
    with client() as c:
        html = c.get("/settings").text
        assert 'name="pol_us"' in html and 'name="pol_kr"' in html
        data = {f"imp_{k}": 10 for k in config.MACRO_INDICATORS}
        data.update({f"sat_{k}": config.SCORE_SATURATION_PCT[k] for k in config.MACRO_INDICATORS})
        data["note"] = ""
        r = c.post("/settings", data={**data, "pol_us": "99", "pol_kr": "6"})
        assert r.status_code == 400 and "0~20" in r.text
        r = c.post("/settings", data={**data, "pol_us": "4", "pol_kr": "3"}, follow_redirects=True)
        assert r.status_code == 200 and "정책 반영 상한: 미국 ±4 · 한국 ±3" in r.text
        r = c.post("/settings", data=data)                         # 정책 입력 누락
        assert r.status_code == 400


# ---------- AI 요약: 판단 중심 + 알고리즘 구조 전달 ----------
def test_prompt_asks_for_judgment_not_number_listing():
    p = llm.SYSTEM_PROMPT
    for needle in ("[국면]", "[핵심 동인]", "[리스크·운용]", "판단", "반복하지 말고", "algorithm",
                   "단정하지 말고", "입력에 없는 사실", "개별 종목의 매수/매도는 말하지 않는다", "rule_score_band 와"):
        assert needle in p, needle


def test_llm_input_includes_algorithm_structure():
    seen = {}

    class Spy(FakeProvider):
        def complete(self, prompt):
            seen["prompt"] = prompt
            return super().complete(prompt)
    run(Spy(policy_payload(us_policy_effect=0, us_policy_evidence=[], kr_policy_effect=0, kr_policy_evidence=[])))
    ctx = json.loads(seen["prompt"].split("<input>")[1].split("</input>")[0])
    alg = ctx["algorithm"]
    assert "최종 점수 = 규칙 점수" in alg["formula"]
    assert len(alg["bands"]) == 5 and alg["bands"][0]["band"].startswith("매수 가능성")
    assert alg["policy_caps_pts"] == config.POLICY_CAPS_DEFAULT
    assert alg["rule_score_band"]["band"].startswith("매수 가능성") and "%" in alg["rule_score_band"]["equity"]
    assert abs(sum(abs(w) for w in alg["indicator_weights"].values()) - 1.0) < 0.02
    with db.session() as conn:     # 저장되는 inputs 스냅샷에도 같은 구조가 남는다 (사후 검증용)
        assert "algorithm" in json.loads(conn.execute("SELECT inputs FROM analyses").fetchone()[0])
