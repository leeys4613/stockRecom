import json
import subprocess

import pytest
from fastapi.testclient import TestClient

import config
import db
import main
import pipeline
import stockviews
from analyzer import llm, stock_llm
from collectors import ai_tracker, macro, reports
from scoring import stock_view
from tests.conftest import GOOD, FakeProvider, consensus, flat_closes, make_rss

W = config.STOCK_VIEW_WEIGHTS
T1 = "삼성전자, HBM 수요 둔화 우려에 외국인 매도 이어져 - 한국경제"
T2 = "SK하이닉스 목표가 줄상향…AI 메모리 낙관론 확산 - 매일경제"


# ---------- 점수 환산 / 검증 (순수 함수) ----------
def test_score_of_maps_levels_to_0_100():
    assert [stock_view.score_of(x) for x in (-3, -1, 0, 1, 3)] == [0.0, 33.3, 50.0, 66.7, 100.0]


def item(**kw):
    base = dict(crowd_sentiment=0, crowd_evidence=[], sector_fit=0, sector_reason="", comment="")
    base.update(kw)
    return base


def test_assess_combines_two_scores_equally_and_keeps_evidence():
    a = stock_view.assess(item(crowd_sentiment=2, crowd_evidence=[T1], sector_fit=-1, sector_reason="약세 섹터", comment="요약"),
                          [T1, T2], ["semis"])
    assert a["crowd_effect"] == 2 and a["crowd_score"] == pytest.approx(83.3) and a["crowd_evidence"] == [T1]
    assert a["sector_effect"] == -1 and a["sector_score"] == pytest.approx(33.3) and a["sector_note"] == "약세 섹터"
    assert a["score"] == pytest.approx(round(W["crowd"] * 83.3 + W["sector"] * 33.3, 1)) and a["comment"] == "요약"


def test_unsupported_crowd_evidence_is_neutralized():
    a = stock_view.assess(item(crowd_sentiment=3, crowd_evidence=["AI가 지어낸 존재하지 않는 헤드라인 문장입니다"]), [T1], ["semis"])
    assert a["crowd_effect"] == 0 and a["crowd_score"] == 50.0 and "근거" in a["crowd_note"] and a["crowd_evidence"] == []


def test_no_headlines_means_neutral_crowd():
    a = stock_view.assess(item(crowd_sentiment=-3, crowd_evidence=[T1]), [], ["semis"])
    assert a["crowd_effect"] == 0 and "기사가 없어" in a["crowd_note"]
    assert "기사 없음" in stock_view.assess(item(), [], ["semis"])["crowd_note"]


def test_sector_is_neutral_when_stock_has_no_sector_group():
    a = stock_view.assess(item(sector_fit=3, sector_reason="AI가 억지로 준 점수"), [T1], [])
    assert a["sector_effect"] == 0 and a["sector_score"] == 50.0 and "섹터 판정 불가" in a["sector_note"]


def test_levels_are_clamped_and_missing_item_is_neutral():
    a = stock_view.assess(item(crowd_sentiment=9, crowd_evidence=[T1], sector_fit=-9), [T1], ["semis"])
    assert a["crowd_effect"] == 3 and a["sector_effect"] == -3
    n = stock_view.assess(None, [T1], ["semis"])
    assert n["score"] == 50.0 and n["comment"] is None


# ---------- 섹터 매핑 / 상대 강도 ----------
def test_sector_groups_mapping_explicit_industry_and_unknown():
    assert stock_view.sector_groups_for("005930") == ["semis", "memory"]
    assert stock_view.sector_groups_for("000660.KS") == ["semis", "memory"]
    assert stock_view.sector_groups_for("267260") == ["power"]
    assert stock_view.sector_groups_for("123456", "Semiconductors") == ["semis"]
    assert stock_view.sector_groups_for("123456", "Auto Manufacturers") == [] and stock_view.sector_groups_for("123456") == []


def gsum(**groups):
    base = {g: dict(n=0, total=0, chg1d=None, chg10d=None, dd60=None) for g in config.AI_GROUP_NAMES}
    for g, (c10, dd) in groups.items():
        base[g] = dict(n=3, total=3, chg1d=0.0, chg10d=c10, dd60=dd)
    return base


def test_sector_strength_is_relative_to_kospi_and_ranked():
    rows = stock_view.sector_strength(gsum(semis=(-10.0, -30.0), power=(-2.0, -20.0), memory=(-25.0, -45.0)), kospi10d=-8.0)
    assert [r["group"] for r in rows] == ["power", "semis", "memory"]                     # 상대 강도 순
    assert rows[0]["rs10"] == pytest.approx(6.0) and rows[1]["rs10"] == pytest.approx(-2.0) and rows[2]["rs10"] == pytest.approx(-17.0)
    assert [r["rank"] for r in rows] == [1, 2, 3]


def test_sector_strength_without_kospi_or_data():
    rows = stock_view.sector_strength(gsum(semis=(-10.0, -30.0)), kospi10d=None)
    assert rows[0]["rs10"] is None and len(rows) == 1                                      # 데이터 없는 그룹은 제외


# ---------- AI 호출 스키마 ----------
def payload(*items):
    return {"items": list(items)}


def raw(code, **kw):
    d = dict(code=code, crowd_sentiment=0, crowd_evidence=[], sector_fit=0, sector_reason="", comment="")
    d.update(kw)
    return d


def test_stock_schema_bounds():
    for bad in (dict(crowd_sentiment=4), dict(sector_fit=-4), dict(crowd_evidence=["a", "b", "c"])):
        with pytest.raises(Exception):
            stock_llm.StockItem(code="005930", **bad)
    assert stock_llm.StockItem(code="005930").crowd_sentiment == 0                       # 빠지면 중립


def test_analyze_stocks_ok_filters_unknown_codes_and_retries_failures():
    p = FakeProvider(payload(raw("005930", crowd_sentiment=1), raw("999999", crowd_sentiment=3)))
    r = stock_llm.analyze_stocks({}, p, {"005930", "000660"})
    assert r["status"] == "ok" and set(r["items"]) == {"005930"}                         # 요청하지 않은 코드는 버린다
    bad = FakeProvider(payload(raw("005930", crowd_sentiment=7)))
    r = stock_llm.analyze_stocks({}, bad, {"005930"})
    assert r["status"] == "failed" and bad.calls == 1 + config.LLM_RETRIES
    boom = FakeProvider(exc=RuntimeError("limit reached"))
    assert "limit reached" in stock_llm.analyze_stocks({}, boom, {"005930"})["error"]


def test_prompt_rules():
    p = stock_llm.SYSTEM_PROMPT
    for needle in ("대중심리", "주도섹터", "글자 그대로", "상대 강도", "섹터 판정 불가", "매수/매도를 지시하지 말고", "신뢰할 수 없는"):
        assert needle in p, needle


def test_claude_cli_provider_passes_custom_schema_and_system(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"is_error": False, "structured_output": {"items": []}}), stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    llm.ClaudeCliProvider().complete("p", system="SYS", schema=stock_llm.StockViews.model_json_schema())
    cmd = seen["cmd"]
    assert cmd[cmd.index("--system-prompt") + 1] == "SYS" and '"items"' in cmd[cmd.index("--json-schema") + 1]
    llm.ClaudeCliProvider().complete("p")                                                  # 기본 호출은 기존 분석 스키마/프롬프트
    cmd = seen["cmd"]
    assert cmd[cmd.index("--system-prompt") + 1] == llm.SYSTEM_PROMPT and "summary_lines" in cmd[cmd.index("--json-schema") + 1]


# ---------- 파이프라인: run_stock_views ----------
class RoutingProvider(FakeProvider):
    """종목 판정 호출(스키마에 items)과 일반 분석 호출을 구분해 응답한다."""

    def __init__(self, stock_payload=None, **kw):
        super().__init__(GOOD, **kw)
        self.stock_payload, self.stock_calls, self.prompts = stock_payload, 0, []

    def complete(self, prompt, system=None, schema=None):
        if schema and "items" in json.dumps(schema):
            self.stock_calls += 1
            self.prompts.append(prompt)
            if self.exc:
                raise self.exc
            return self.stock_payload
        return super().complete(prompt, system, schema)


def seed_watchlist(*codes):
    db.init_db()
    names = {"005930": "삼성전자", "000660": "SK하이닉스", "035420": "NAVER"}
    with db.session() as conn:
        for c in codes:
            reports.add_stock(conn, c, "KS", names[c], lambda s: consensus())


def seed_market(kospi10d=-8.0, groups=None):
    """KOSPI 10일 변화 지표와 테마 그룹 스냅샷을 만들어 둔다."""
    with db.session() as conn:
        macro.save(conn, [dict(key="KOSPI10D", status="ok", value=kospi10d, prev_value=None, change_pct=None, error=None,
                               fetched_at=ai_tracker.now_iso())])
        snaps = []
        for t, (_, g) in config.AI_TICKERS.items():
            c10, dd = (groups or {}).get(g, (-5.0, -20.0))
            snaps.append(dict(ticker=t, grp=g, status="ok", error=None, fetched_at=ai_tracker.now_iso(), price=10.0,
                              chg1d=0.0, chg10d=c10, dd60=dd))
        ai_tracker.save(conn, snaps)


def rss(*titles):
    return lambda url: make_rss([(t, f"https://e.com/{i}", 2) for i, t in enumerate(titles)])


HIST = lambda sym: [100.0] * 50 + [90.0] * 21  # noqa: E731


def run_views(provider, **kw):
    kw.setdefault("history_fetcher", HIST)
    kw.setdefault("headline_fetcher", rss(T1, T2))
    kw.setdefault("force", True)
    return pipeline.run_stock_views(provider, **kw)


def test_run_stock_views_saves_scored_rows_and_sends_context():
    seed_watchlist("005930", "000660")
    seed_market(-8.0, {"semis": (-10.0, -30.0), "memory": (-25.0, -45.0)})
    p = RoutingProvider(payload(
        raw("005930", crowd_sentiment=-2, crowd_evidence=[T1], sector_fit=-1, sector_reason="메모리는 시장 대비 약함", comment="삼성 요약"),
        raw("000660", crowd_sentiment=2, crowd_evidence=[T2], sector_fit=1, sector_reason="HBM 선도", comment="하이닉스 요약")))
    assert run_views(p) == 2 and p.stock_calls == 1
    with db.session() as conn:
        _, s = stockviews.latest(conn, "005930")
        _, h = stockviews.latest(conn, "000660")
    assert s["status"] == "ok" and s["crowd_effect"] == -2 and s["crowd_evidence"] == [T1]
    assert s["score"] == pytest.approx(round(W["crowd"] * stock_view.score_of(-2) + W["sector"] * stock_view.score_of(-1), 1))
    assert h["score"] > s["score"] and s["sector_groups"] == ["semis", "memory"] and s["metrics"]["headlines"] == 2
    assert s["metrics"]["dd60"] == pytest.approx(-10.0) and s["metrics"]["price"] == 90.0
    ctx = json.loads(p.prompts[0].split("<input>")[1].split("</input>")[0])
    assert {x["code"] for x in ctx["stocks"]} == {"005930", "000660"}
    s0 = next(x for x in ctx["stocks"] if x["code"] == "005930")
    assert s0["headlines"] == [T1, T2] and s0["sector_groups"] == ["반도체(공급망)", "메모리 프록시"] and s0["price"]["dd60"] == -10.0
    ss = {r["group"]: r for r in ctx["sector_strength"]}
    assert ss["반도체(공급망)"]["rs10"] == -2.0 and ss["메모리 프록시"]["rs10"] == -17.0          # KOSPI -8 대비
    assert ctx["kospi"]["chg10d"] == -8.0 and "market" in ctx


def test_market_context_includes_latest_analysis_when_available():
    seed_watchlist("005930")
    seed_market()
    db.init_db()
    pipeline.run_once(FakeProvider(GOOD), lambda s: (110.0, 100.0), lambda u: make_rss())
    p = RoutingProvider(payload(raw("005930")))
    run_views(p)
    ctx = json.loads(p.prompts[0].split("<input>")[1].split("</input>")[0])
    m = ctx["market"]
    assert m["final_score"] is not None and m["band"].startswith("매수 가능성") and len(m["summary"]) == 4
    assert set(m["judgments"]) == {"us", "kr", "ai", "mem"} and "groups" in m["ai_tracker"]


def test_min_interval_skip_force_and_only():
    seed_watchlist("005930", "000660")
    seed_market()
    p = RoutingProvider(payload(raw("005930"), raw("000660")))
    assert run_views(p) == 2
    assert pipeline.run_stock_views(p, history_fetcher=HIST, headline_fetcher=rss(T1), force=False) is None      # 6시간 이내: 건너뜀
    assert p.stock_calls == 1
    assert run_views(p, only={"005930"}) == 1 and p.stock_calls == 2                                             # 강제 + 일부 종목
    assert json.loads(p.prompts[1].split("<input>")[1].split("</input>")[0])["stocks"][0]["code"] == "005930"
    assert len(json.loads(p.prompts[1].split("<input>")[1].split("</input>")[0])["stocks"]) == 1


def test_new_stock_without_view_is_due_even_within_interval():
    seed_watchlist("005930")
    seed_market()
    p = RoutingProvider(payload(raw("005930"), raw("000660")))
    run_views(p)
    seed_watchlist("000660")                                      # 새 종목 추가
    assert pipeline.run_stock_views(p, history_fetcher=HIST, headline_fetcher=rss(T1), force=False) == 2


def test_empty_watchlist_returns_zero_without_llm_call():
    db.init_db()
    p = RoutingProvider(payload())
    assert run_views(p) == 0 and p.stock_calls == 0


def test_llm_failure_records_failed_rows_and_keeps_previous_view():
    seed_watchlist("005930")
    seed_market()
    run_views(RoutingProvider(payload(raw("005930", crowd_sentiment=1, crowd_evidence=[T1], comment="이전 판정"))))
    run_views(RoutingProvider(exc=RuntimeError("limit reached")))
    with db.session() as conn:
        last, ok = stockviews.latest(conn, "005930")
    assert last["status"] == "failed" and "limit reached" in last["error"]
    assert ok["status"] == "ok" and ok["comment"] == "이전 판정"                                 # 마지막 성공 판정은 유지


def test_stock_missing_from_llm_output_is_failed_not_silently_neutral():
    seed_watchlist("005930", "000660")
    seed_market()
    run_views(RoutingProvider(payload(raw("005930", comment="있음"))))
    with db.session() as conn:
        assert stockviews.latest(conn, "005930")[1]["comment"] == "있음"
        last, ok = stockviews.latest(conn, "000660")
    assert last["status"] == "failed" and "빠졌습니다" in last["error"] and ok is None


def test_headline_and_price_failures_are_isolated_and_recorded():
    seed_watchlist("005930")
    seed_market()

    def boom(url):
        raise TimeoutError("news down")

    def hist_boom(sym):
        raise RuntimeError("yf down")
    p = RoutingProvider(payload(raw("005930", crowd_sentiment=3, crowd_evidence=[T1], sector_fit=2, comment="x")))
    assert run_views(p, headline_fetcher=boom, history_fetcher=hist_boom) == 1
    with db.session() as conn:
        _, v = stockviews.latest(conn, "005930")
    assert v["crowd_effect"] == 0 and "기사가 없어" in v["crowd_note"]                          # 기사 없이는 심리를 반영하지 않는다
    assert v["sector_effect"] == 2 and len(v["metrics"]["errors"]) == 3 and v["metrics"].get("price") is None   # 가격·일봉·뉴스


def test_unmapped_stock_uses_yahoo_industry_then_falls_back_to_neutral():
    seed_watchlist("035420")
    seed_market()
    p = RoutingProvider(payload(raw("035420", sector_fit=3, sector_reason="x")))
    run_views(p, industry_fetcher=lambda sym: "Semiconductors")
    with db.session() as conn:
        assert stockviews.latest(conn, "035420")[1]["sector_groups"] == ["semis"]
    run_views(p, industry_fetcher=lambda sym: "Internet Content & Information")
    with db.session() as conn:
        v = stockviews.latest(conn, "035420")[1]
    assert v["sector_groups"] == [] and v["sector_effect"] == 0 and v["sector_score"] == 50.0 and "섹터 판정 불가" in v["sector_note"]


def test_concurrent_run_is_skipped():
    seed_watchlist("005930")
    assert pipeline._stock_lock.acquire(blocking=False)
    try:
        assert run_views(RoutingProvider(payload(raw("005930")))) is None
    finally:
        pipeline._stock_lock.release()


# ---------- run_once / 스케줄러 연동 ----------
def test_run_once_triggers_stock_views_best_effort(monkeypatch):
    seed_watchlist("005930")
    monkeypatch.setattr(config, "STOCK_VIEW_MIN_HOURS", 6)
    calls = []
    real = pipeline.run_stock_views
    monkeypatch.setattr(pipeline, "run_stock_views", lambda provider, history_fetcher=None, **k: calls.append(k) or real(
        provider, history_fetcher=HIST, headline_fetcher=rss(T1), **k))
    p = RoutingProvider(payload(raw("005930", crowd_sentiment=1, crowd_evidence=[T1])))
    assert pipeline.run_once(p, lambda s: (110.0, 100.0), lambda u: make_rss()) is not None
    assert calls == [{"force": False}] and p.stock_calls == 1                       # 최소 간격 판정을 적용해 호출
    with db.session() as conn:
        assert stockviews.latest(conn, "005930")[1]["status"] == "ok"
        assert conn.execute("SELECT analysis_id FROM stock_views").fetchone()[0] is not None


def test_stock_view_failure_does_not_break_main_analysis(monkeypatch):
    seed_watchlist("005930")

    def boom(*a, **k):
        raise RuntimeError("stock views exploded")
    monkeypatch.setattr(pipeline, "run_stock_views", boom)
    assert pipeline.run_once(FakeProvider(GOOD), lambda s: (110.0, 100.0), lambda u: make_rss()) is not None
    with db.session() as conn:
        assert pipeline.latest_analysis(conn)["llm_status"] == "ok"


def test_scheduled_reports_job_also_runs_views_on_trading_days(monkeypatch):
    from datetime import date
    import calendar_kr
    called = []
    monkeypatch.setattr(pipeline, "run_reports", lambda *a, **k: called.append("reports") or 0)
    monkeypatch.setattr(pipeline, "run_stock_views", lambda *a, **k: called.append(("views", k.get("force"))))
    monkeypatch.setattr(calendar_kr, "today_kst", lambda: date(2026, 11, 9))
    pipeline.scheduled_reports_job()
    assert called == ["reports", ("views", True)]
    called.clear()
    monkeypatch.setattr(calendar_kr, "today_kst", lambda: date(2026, 11, 8))
    pipeline.scheduled_reports_job()
    assert called == []


# ---------- 순위 / 화면 ----------
def seed_views(scores):
    db.init_db()
    with db.session() as conn:
        for code, name, sc, comment in scores:
            conn.execute("INSERT OR IGNORE INTO watchlist (code,market,name,added_at) VALUES (?,?,?,?)", (code, "KS", name, "2026-11-01T00:00:00+00:00"))
            if sc is not None:
                stockviews.save(conn, [stockviews.make_row(code, None, dict(
                    crowd_effect=1, crowd_score=66.7, crowd_evidence=[T1], crowd_note=None, sector_effect=-1, sector_score=33.3,
                    sector_note="섹터 근거 문장", score=sc, comment=comment), ["semis"], dict(price=100.0, chg10d=-5.0, dd60=-20.0, headlines=3, errors=[]))])


def test_top_picks_view_ranks_by_ai_score_and_lists_pending_last():
    seed_views([("000001", "A종목", 40.0, "a"), ("000002", "B종목", 80.0, "b"), ("000003", "C종목", None, None), ("000004", "D종목", 60.0, "d")])
    with db.session() as conn:
        top, ranked, _, view_at = pipeline.top_picks_view(conn)
    assert [e["name"] for e in top] == ["B종목", "D종목", "A종목"]
    assert [e["name"] for e in ranked] == ["B종목", "D종목", "A종목", "C종목"] and view_at is not None


def test_top_picks_limited_to_top_n(monkeypatch):
    monkeypatch.setattr(config, "PICK_TOP_N", 2)
    seed_views([("000001", "A", 40.0, ""), ("000002", "B", 80.0, ""), ("000003", "C", 60.0, "")])
    with db.session() as conn:
        assert [e["name"] for e in pipeline.top_picks_view(conn)[0]] == ["B", "C"]


def test_consensus_is_reference_only_and_not_used_for_ranking():
    seed_views([("000001", "낮은점수", 30.0, ""), ("000002", "높은점수", 70.0, "")])
    with db.session() as conn:
        for code, target in (("000001", 400.0), ("000002", 90.0)):              # 컨센서스가 더 낙관적인 쪽이 AI 점수는 낮다
            conn.execute("INSERT INTO consensus_snapshots (code,status,fetched_at,price,target_mean,rec_mean,n_analysts) VALUES (?, 'ok', ?, 100, ?, 1.2, 30)",
                         (code, ai_tracker.now_iso(), target))
        top = pipeline.top_picks_view(conn)[0]
    assert top[0]["name"] == "높은점수" and top[1]["upside"] == pytest.approx(3.0)       # 참고용 괴리율은 계산되지만 순위와 무관


def client():
    return TestClient(main.app)


def test_watchlist_page_shows_scores_and_hides_analyst_count_and_opinion():
    seed_views([("000001", "A종목", 62.0, "A 코멘트")])
    with db.session() as conn:
        conn.execute("INSERT INTO consensus_snapshots (code,status,fetched_at,price,target_mean,rec_mean,n_analysts,target_high,target_low) "
                     "VALUES ('000001','ok',?,100,150,1.3,35,200,100)", (ai_tracker.now_iso(),))
    with client() as c:
        html = c.get("/watchlist").text
    assert "추천 점수" in html and ">62<" in html and "대중심리" in html and "주도섹터 적합도" in html and "A 코멘트" in html
    assert "판정 근거 보기" in html and T1 in html and "섹터 근거 문장" in html and "반도체(공급망)" in html
    assert "애널리스트" not in html and "의견 평균" not in html and "35명" not in html and "1.3" not in html           # 제거 요구사항
    assert "증권사 컨센서스 (참고용 · 낙관 편향 주의)" in html and "괴리 +50%" in html                                    # 목표가는 참고로만
    assert "대중심리 50%" in html or "각 50%" in html


def test_watchlist_pending_and_failed_states():
    seed_views([("000001", "미판정", None, None)])
    with db.session() as conn:
        stockviews.save(conn, [stockviews.make_row("000001", None, error="TimeoutError: limit reached")])
    with client() as c:
        html = c.get("/watchlist").text
    assert "AI 적합도 판정 전입니다" in html and "최근 판정 실패: TimeoutError: limit reached" in html


def test_watchlist_shows_previous_view_with_failure_notice():
    seed_views([("000001", "A", 55.0, "이전")])
    with db.session() as conn:
        stockviews.save(conn, [stockviews.make_row("000001", None, error="RuntimeError: x")])
    with client() as c:
        html = c.get("/watchlist").text
    assert "최근 AI 판정 실패(이전 판정을 표시 중)" in html and ">55<" in html


def test_dashboard_card_empty_and_pending_states():
    db.init_db()
    with client() as c:
        assert "관심종목을 추가하면" in c.get("/").text
    seed_views([("000001", "A", None, None)])
    with client() as c:
        assert "아직 AI 판정 결과가 없습니다" in c.get("/").text


def test_add_triggers_view_for_that_stock_and_refresh_triggers_reports_and_views(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "run_stock_views", lambda *a, **k: calls.append(("views", a)))
    monkeypatch.setattr(pipeline, "run_reports_and_views", lambda *a, **k: calls.append("refresh"))
    monkeypatch.setattr(reports, "yf_fetch", lambda sym: consensus())
    with client() as c:
        c.post("/watchlist/add", data=dict(code="005930", market="KS", name="삼성전자"), follow_redirects=True)
        c.post("/watchlist/refresh")
    assert calls[0] == ("views", (None, None, None, None, True, {"005930"})) and calls[1] == "refresh"


def test_page_text_states_not_a_recommendation_and_proxy_limits():
    seed_views([("000001", "A", 50.0, "")])
    with client() as c:
        html = c.get("/watchlist").text
    assert "매수 추천이 아니며" in html and "섹터 판정은 지표가 있는 AI·반도체·메모리·전력 계열에만" in html
    assert "근거를 확인하지 못하면 0(중립)" in html
