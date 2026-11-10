import pytest
from fastapi.testclient import TestClient

import config
import db
import main
import screener as S

DATES = [f"2026-11-{d:02d}" for d in range(2, 12)] + []   # 임의의 거래일 라벨


def dates(n):
    return [f"2026-10-{d:02d}" if d <= 31 else "" for d in range(1, n + 1)]


def series(n, start, daily, vol=10_000_000):
    """n개 일봉: start 에서 매일 daily(%) 씩 변동. 날짜는 D001..D0nn (지수와 같은 라벨을 공유)."""
    out, c = [], start
    for i in range(n):
        out.append((f"D{i:03d}", c, vol))
        c *= 1 + daily / 100
    return out


def idx_bars(n=25, daily=0.2, start=1000.0):
    return [(d, c) for d, c, _ in series(n, start, daily)]


def mk(n=25, d1=0.2, d2=None, start=10_000.0, vol=10_000_000):
    """마지막 5일의 일간 상승률을 d2 로, 그 이전은 d1 로."""
    d2 = d1 if d2 is None else d2
    out, c = [], start
    for i in range(n):
        out.append((f"D{i:03d}", c, vol))
        c *= 1 + (d2 if i >= n - 6 else d1) / 100
    return out


IDX = lambda: S.index_returns(idx_bars())


def test_index_returns_shape():
    i = IDX()
    assert i["asof"] == "D024" and i["r3"] == pytest.approx(((1.002) ** 3 - 1) * 100, rel=1e-3) and len(i["daily"]) == 5


def test_outperformer_passes_and_laggard_fails():
    idx = IDX()
    up = S.evaluate(mk(d2=2.0), idx)
    assert up["ex3"] > 0 and up["ex5"] > 0 and up["beat_days"] == 5 and S.passes(up)
    down = S.evaluate(mk(d2=-1.0), idx)
    assert down["ex3"] < 0 and not S.passes(down)


def test_requires_both_windows_and_beat_days():
    idx = IDX()
    m = S.evaluate(mk(d2=2.0), idx)
    assert S.passes(m)
    assert not S.passes(dict(m, ex3=-0.1)) and not S.passes(dict(m, ex5=-0.1))      # 3일·5일 모두 이겨야 한다
    assert not S.passes(dict(m, beat_days=config.SCREEN_BEAT_DAYS - 1))
    assert not S.passes(dict(m, price=500)) and not S.passes(dict(m, value20=1e8))   # 동전주·저유동성 제외


def test_stale_or_short_history_excluded():
    idx = IDX()
    assert S.evaluate(mk()[:-1], idx) is None            # 기준일 일봉이 없다(거래정지·지연)
    assert S.evaluate(mk(n=15), idx) is None             # 이력 부족


def test_flags():
    idx = IDX()
    hot = S.evaluate(mk(d2=7.0), idx)                    # 5일 약 +40%
    assert "overheat" in S.flags_for(hot)
    bars = mk(d2=2.0)
    bars[-5:] = [(d, c, 50_000_000) for d, c, _ in bars[-5:]]
    assert "volume_surge" in S.flags_for(S.evaluate(bars, idx))


def seed_master():
    db.init_db()
    with db.session() as conn:
        conn.execute("DELETE FROM stock_master")
        conn.executemany("INSERT INTO stock_master (code,name,market,name_norm,choseong,source,updated_at) VALUES (?,?,?,?,?,?,?)",
                         [("000001", "강세대형", "KS", "x", "x", "t", "x"), ("000002", "강세소형", "KS", "x", "x", "t", "x"),
                          ("000003", "약세종목", "KQ", "x", "x", "t", "x"), ("000004", "조회실패", "KQ", "x", "x", "t", "x"),
                          ("000005", "시총모름", "KS", "x", "x", "t", "x")])


def fake_chunk(symbols):
    data = {"000001.KS": mk(d2=2.0), "000002.KS": mk(d2=3.0), "000003.KQ": mk(d2=-2.0), "000005.KS": mk(d2=1.5)}
    return {s: data[s] for s in symbols if s in data}


MC = {"000001.KS": 2e12, "000002.KS": 1e11}      # 000005 는 시총 조회 실패


def fake_mcap(sym):
    if sym not in MC:
        raise RuntimeError("no mcap")
    return MC[sym]


def test_full_run_applies_cap_filter_ranks_and_records_counts():
    seed_master()
    rid = S.run(db.session, fake_chunk, lambda s: idx_bars(), fake_mcap, delay=0)
    with db.session() as conn:
        last, run, items = S.latest(conn)
    assert rid == run["id"] and run["status"] == "ok"
    assert [i["code"] for i in items] == ["000001"]                 # 강세소형은 시총 미달, 시총모름은 제외, 약세는 탈락
    assert run["universe_n"] == 5 and run["loaded_n"] == 4 and run["failed_n"] == 1     # 조회실패 1
    assert run["passed_n"] == 3 and run["mcap_missing_n"] == 1 and run["final_n"] == 1
    assert items[0]["mcap"] == 2e12 and items[0]["beat_days"] == 5 and run["asof"] == "D024"


def test_rank_by_average_excess_and_mcap_cache(monkeypatch):
    seed_master()
    MC2 = {"000001.KS": 2e12, "000002.KS": 9e11, "000005.KS": 7e11}
    calls = []
    f = lambda s: calls.append(s) or MC2[s]  # noqa: E731
    S.run(db.session, fake_chunk, lambda s: idx_bars(), f, delay=0)
    with db.session() as conn:
        _, _, items = S.latest(conn)
    assert [i["code"] for i in items] == ["000002", "000001", "000005"]       # 초과수익 큰 순
    n = len(calls)
    S.run(db.session, fake_chunk, lambda s: idx_bars(), f, delay=0)
    assert len(calls) == n                                                      # 시총은 캐시 사용


def test_index_failure_is_recorded_and_previous_result_kept():
    seed_master()
    S.run(db.session, fake_chunk, lambda s: idx_bars(), lambda s: 2e12, delay=0)

    def boom(sym):
        raise RuntimeError("idx down")
    S.run(db.session, fake_chunk, boom, lambda s: 2e12, delay=0)
    with db.session() as conn:
        last, run, items = S.latest(conn)
    assert last["status"] == "failed" and "지수 조회 실패" in last["error"] and run["status"] == "ok" and items


def test_page_renders_filters_and_refresh(monkeypatch):
    seed_master()
    S.run(db.session, fake_chunk, lambda s: idx_bars(), lambda s: 2e12, delay=0)
    with TestClient(main.app) as c:
        html = c.get("/screener").text
        assert "지수 대비 강세 종목" in html and "강세대형" in html and "투자 권유가 아닙니다" in html and "예측하지 않으며" in html
        assert "＋ 관심" in html and "매도" not in html and "조회 실패/기준일 데이터 없음" in html
        ks = html[html.index('id="screen-list-KS"'):html.index('id="sec-KQ"')]
        kq = html[html.index('id="screen-list-KQ"'):]
        assert "강세대형" in ks and "강세대형" not in kq                      # 코스피/코스닥을 따로 보여준다
        assert "KOSPI 대비" in html and "KOSDAQ 대비" in html and "조건에 맞는 종목이 없습니다" in kq
        calls = []
        monkeypatch.setattr(S, "run", lambda f, *a: calls.append(1))
        assert c.post("/screener/refresh", follow_redirects=False).status_code == 303
    assert calls == [1]


def test_page_without_results_and_scheduler_job(monkeypatch):
    db.init_db()
    with TestClient(main.app) as c:
        assert "아직 스캔 결과가 없습니다" in c.get("/screener").text
    ids = [j.id for j in main.build_scheduler().get_jobs()]
    assert "screen-daily" in ids


# ---------- 2단계: AI 보강 ----------
import json  # noqa: E402

from analyzer import screen_llm  # noqa: E402
from tests.conftest import FakeProvider, make_rss  # noqa: E402

T1 = "강세대형, 3분기 영업이익 시장 기대 크게 웃돌아"


class ScreenProvider(FakeProvider):
    def __init__(self, payload=None, **kw):
        super().__init__({}, **kw)
        self.payload, self.prompts = payload, []

    def complete(self, prompt, system=None, schema=None):
        self.prompts.append(prompt)
        if self.exc:
            raise self.exc
        return self.payload


def ai_payload(**kw):
    item = dict(code="000001", reason_type="실적·펀더멘털", reason="3분기 실적이 기대를 웃돌았다는 보도가 이어졌다.", news_tone=2,
                tone_evidence=[T1], catalyst_kind="지속성", risk="")
    item.update(kw)
    return dict(items=[item])


def run_ai(provider, titles=(T1,), **kw):
    seed_master()
    rss = lambda url: make_rss([(t, f"https://e.com/{n}", 2) for n, t in enumerate(titles)])  # noqa: E731
    return S.run(db.session, fake_chunk, lambda s: idx_bars(), lambda s: 2e12, delay=0, provider=provider,
                 headline_fetcher=rss, **kw)


def test_ai_augmentation_saved_with_evidence_and_codes_only_numbers(monkeypatch):
    monkeypatch.setattr(config, "STOCK_VIEW_DELAY_SEC", 0)
    p = ScreenProvider(ai_payload())
    run_ai(p)
    with db.session() as conn:
        _, run, items = S.latest(conn)
    a = next(i for i in items if i["code"] == "000001")["ai"]
    assert run["ai_status"] == "ok" and a["reason_type"] == "실적·펀더멘털" and a["news_tone"] == 2 and a["tone_evidence"] == [T1]
    ctx = json.loads(p.prompts[0].split("<input>")[1].split("</input>")[0])
    s0 = next(s for s in ctx["stocks"] if s["code"] == "000001")
    assert s0["code"] == "000001" and s0["headlines"] == [T1] and s0["excess5"] > 0 and "mcap" not in s0       # 계산된 수치를 입력으로만 준다


def test_unsupported_evidence_or_no_headlines_is_neutralized(monkeypatch):
    monkeypatch.setattr(config, "STOCK_VIEW_DELAY_SEC", 0)
    run_ai(ScreenProvider(ai_payload(tone_evidence=["지어낸 헤드라인입니다 근거 없음"], news_tone=3)))
    with db.session() as conn:
        a = next(i for i in S.latest(conn)[2] if i["code"] == "000001")["ai"]
    assert a["news_tone"] == 0 and a["tone_evidence"] == [] and "근거 헤드라인을 확인하지 못해" in a["tone_note"]
    run_ai(ScreenProvider(ai_payload()), titles=())
    with db.session() as conn:
        _, run, items = S.latest(conn)
    assert items == [] and run["nobg_n"] == run["pool_n"] > 0          # 기사가 없어 배경을 확인할 수 없으면 추천에서 뺀다


def test_invalid_kind_values_fall_back():
    a = screen_llm.assess(dict(reason_type="외계인", catalyst_kind="x", news_tone=0), [T1])
    assert a["reason_type"] == "뚜렷한 배경 없음" and a["catalyst_kind"] == "불명"


def test_ai_failure_keeps_stage1_results(monkeypatch):
    monkeypatch.setattr(config, "STOCK_VIEW_DELAY_SEC", 0)
    run_ai(ScreenProvider(exc=RuntimeError("limit reached")))
    with db.session() as conn:
        _, run, items = S.latest(conn)
    assert run["ai_status"] == "failed" and "limit reached" in run["ai_error"] and items and items[0]["ai"] is None
    with TestClient(main.app) as c:
        html = c.get("/screener").text
    assert 'id="ai-failed"' in html and "수치만 표시" in html and "강세대형" in html


def test_page_shows_ai_note_and_skips_ai_when_no_candidates(monkeypatch):
    monkeypatch.setattr(config, "STOCK_VIEW_DELAY_SEC", 0)
    run_ai(ScreenProvider(ai_payload(risk="5일 급등으로 과열 우려")))
    with TestClient(main.app) as c:
        html = c.get("/screener").text
    assert "실적·펀더멘털" in html and "재료 지속성" in html and "리스크 · 5일 급등으로 과열 우려" in html and "근거 기사" in html
    assert "매도" not in html
    seed_master()
    S.run(db.session, lambda syms: {s: mk(d2=-2.0) for s in syms}, lambda s: idx_bars(), lambda s: 2e12, delay=0,
          provider=ScreenProvider(ai_payload()))        # 모두 약세 → 후보 없음
    with db.session() as conn:
        _, run, items = S.latest(conn)
    assert run["ai_status"] == "skipped" and items == []


def test_low_load_ratio_is_recorded_as_failure_not_empty_result():
    seed_master()
    S.run(db.session, fake_chunk, lambda s: idx_bars(), lambda s: 2e12, delay=0, ai=False)        # 정상 결과 먼저
    S.run(db.session, lambda syms: {}, lambda s: idx_bars(), lambda s: 2e12, delay=0, ai=False)   # 전부 실패
    with db.session() as conn:
        last, run, items = S.latest(conn)
    assert last["status"] == "failed" and "성공률이 낮습니다" in last["error"] and run["status"] == "ok" and items


def test_raise_fd_limit_never_lowers_or_raises_errors():
    import resource
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    S.raise_fd_limit(16)
    assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] >= min(soft, 16)


def test_ten_day_excess_is_required_and_part_of_rank():
    idx = IDX()
    m = S.evaluate(mk(d2=2.0), idx)
    assert m["ex10"] > 0 and S.passes(m)
    assert not S.passes(dict(m, ex10=-0.1))                                          # 10일도 지수를 이겨야 한다
    a, b = dict(ex3=10, ex5=10, ex10=10), dict(ex3=30, ex5=0, ex10=0)
    assert S.rank_key(a) < S.rank_key(b) or S.rank_key(a) == S.rank_key(b)           # 평균이 같으면 동률
    assert S.rank_key(dict(ex3=20, ex5=20, ex10=20)) < S.rank_key(dict(ex3=5, ex5=5, ex10=5))


def test_no_background_stocks_are_removed_and_counted(monkeypatch):
    monkeypatch.setattr(config, "STOCK_VIEW_DELAY_SEC", 0)
    p = ScreenProvider(dict(items=[
        dict(code="000001", reason_type="실적·펀더멘털", reason="r", news_tone=2, tone_evidence=[T1], catalyst_kind="지속성", risk=""),
        dict(code="000002", reason_type="뚜렷한 배경 없음", reason="", news_tone=0, tone_evidence=[], catalyst_kind="불명", risk=""),
        dict(code="000005", reason_type="업황·테마", reason="r", news_tone=1, tone_evidence=[T1], catalyst_kind="불명", risk="")]))
    run_ai(p)
    with db.session() as conn:
        _, run, items = S.latest(conn)
    assert [i["code"] for i in items] == ["000001", "000005"]                        # 순위는 남은 종목 기준으로 다시 매긴다
    assert [i["rank"] for i in items] == [1, 2] and run["nobg_n"] == 1 and run["pool_n"] == 3 and run["final_n"] == 2
    assert items[0]["r10"] is not None and items[0]["ex10"] > 0


def test_ai_failure_shows_numeric_ranking_without_dropping():
    run_ai(ScreenProvider(exc=RuntimeError("down")))
    with db.session() as conn:
        _, run, items = S.latest(conn)
    assert run["ai_status"] == "failed" and run["nobg_n"] == 0 and len(items) == 3


def test_markets_are_selected_separately_with_own_top_n(monkeypatch):
    monkeypatch.setattr(config, "SCREEN_TOP_N", 1)
    monkeypatch.setattr(config, "SCREEN_AI_POOL", 2)
    seed_master()
    with db.session() as conn:                       # 코스닥 강세 종목 하나 추가
        conn.execute("INSERT INTO stock_master (code,name,market,name_norm,choseong,source,updated_at) VALUES ('000006','강세코스닥','KQ','x','x','t','x')")
    chunk = lambda syms: {s: v for s, v in {**{k: mk(d2=2.0) for k in ("000001.KS", "000002.KS", "000005.KS")},  # noqa: E731
                                             "000006.KQ": mk(d2=2.5)}.items() if s in syms}
    S.run(db.session, chunk, lambda s: idx_bars(), lambda s: 2e12, delay=0, ai=False)
    with db.session() as conn:
        _, run, items = S.latest(conn)
    by = {m: [i["code"] for i in items if i["market"] == m] for m in ("KS", "KQ")}
    assert len(by["KS"]) == 1 and by["KQ"] == ["000006"]                            # 시장별로 각각 TOP_N 개
    assert all(i["rank"] == 1 for i in items)                                       # 순위는 시장 안에서 매긴다
    assert run["pool_n"] == 3                                                       # AI 풀: 코스피 2 + 코스닥 1


# ---------- 업종 대장주 ----------
def set_industry(**by_code):
    with db.session() as conn:
        for code, ind in by_code.items():
            conn.execute("UPDATE stock_master SET industry=? WHERE code=?", (ind, code))


def test_leader_is_largest_mcap_in_same_industry_with_own_performance():
    seed_master()
    set_industry(**{"000001": "소프트웨어", "000002": "소프트웨어", "000005": "소프트웨어", "000003": "철강"})
    MCAPS = {"000001.KS": 2e12, "000002.KS": 9e11, "000005.KS": 6e12, "000003.KQ": 1e12}
    S.run(db.session, fake_chunk, lambda s: idx_bars(), lambda s: MCAPS[s], delay=0, ai=False)
    with db.session() as conn:
        _, _, items = S.latest(conn)
    by = {i["code"]: i for i in items}
    lead = by["000001"]["leader"]
    assert lead["code"] == "000005" and lead["name"] == "시총모름" and lead["mcap"] == 6e12 and lead["is_self"] is False
    assert lead["industry"] == "소프트웨어" and lead["ex5"] > 0 and lead["r5"] is not None       # 대장주의 최근 성과도 함께
    assert by["000005"]["leader"]["is_self"] is True                                           # 자신이 대장주


def test_leader_missing_when_no_industry_or_no_mcap():
    seed_master()                                            # 업종 정보 없음
    S.run(db.session, fake_chunk, lambda s: idx_bars(), lambda s: 2e12, delay=0, ai=False)
    with db.session() as conn:
        _, _, items = S.latest(conn)
    assert items and all(i["leader"] is None for i in items)
    set_industry(**{"000001": "소프트웨어", "000002": "소프트웨어", "000005": "소프트웨어"})
    calls = []

    def flaky(sym):
        calls.append(sym)
        if sym == "000001.KS":
            return 2e12
        raise RuntimeError("no mcap")
    with db.session() as conn:
        conn.execute("DELETE FROM mcap_cache")
    S.run(db.session, fake_chunk, lambda s: idx_bars(), flaky, delay=0, ai=False)
    with db.session() as conn:
        _, _, items = S.latest(conn)
    lead = items[0]["leader"]
    assert lead["code"] == "000001"                                    # 확인된 시총만으로 판단(시총 모르는 종목은 후보에서 빠짐)


def test_leader_shown_on_page():
    seed_master()
    set_industry(**{"000001": "소프트웨어", "000002": "소프트웨어", "000005": "소프트웨어"})
    S.run(db.session, fake_chunk, lambda s: idx_bars(), lambda s: {"000001.KS": 2e12, "000002.KS": 9e11, "000005.KS": 6e12}[s],
          delay=0, ai=False)
    with TestClient(main.app) as c:
        html = c.get("/screener").text
    assert "업종 <b>소프트웨어</b>" in html and "대장주 <b>시총모름</b>" in html and "업종 대장주(시총 1위)" in html
    assert "KRX 표준 업종명" in html and "매도" not in html


# ---------- 성과 검증 루프 ----------
from datetime import date, timedelta  # noqa: E402


def insert_run(asof, items):
    """items: [(code, market, status, extra dict)]"""
    db.init_db()
    with db.session() as conn:
        rid = conn.execute("INSERT INTO screen_runs (created_at,status,asof) VALUES ('2026-11-01T00:00:00+00:00','ok',?)", (asof,)).lastrowid
        for n, (code, market, status, ex) in enumerate(items, 1):
            conn.execute("INSERT INTO screen_items (run_id,rank,code,name,market,price,status,ai,flags,leader) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (rid, n if status == "kept" else 0, code, code, market, 100.0, status,
                          json.dumps(ex.get("ai")) if ex.get("ai") else None, json.dumps(ex.get("flags", [])),
                          json.dumps(ex.get("leader")) if ex.get("leader") else None))
    return rid


def day_labels(start, n):
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


def bars_from(dates, closes):
    return [(d, c, 1000.0) for d, c in zip(dates, closes)]


def test_status_recorded_for_nobg_and_cut_and_hidden_on_page(monkeypatch):
    monkeypatch.setattr(config, "STOCK_VIEW_DELAY_SEC", 0)
    monkeypatch.setattr(config, "SCREEN_TOP_N", 1)
    p = ScreenProvider(dict(items=[
        dict(code="000001", reason_type="실적·펀더멘털", reason="r", news_tone=1, tone_evidence=[T1], catalyst_kind="지속성", risk=""),
        dict(code="000002", reason_type="뚜렷한 배경 없음", reason="", news_tone=0, tone_evidence=[], catalyst_kind="불명", risk=""),
        dict(code="000005", reason_type="업황·테마", reason="r", news_tone=1, tone_evidence=[T1], catalyst_kind="불명", risk="")]))
    run_ai(p)
    with db.session() as conn:
        st = {r["code"]: (r["status"], r["rank"]) for r in conn.execute("SELECT code,status,rank FROM screen_items")}
        _, _, items = S.latest(conn)
    assert st["000001"] == ("kept", 1) and st["000002"] == ("nobg", 0) and st["000005"] == ("cut", 0)
    assert [i["code"] for i in items] == ["000001"]                       # 화면에는 추천만


def test_fill_outcomes_computes_excess_vs_own_index_after_5_and_10_days():
    start = date.today() - timedelta(days=30)
    ds = day_labels(start, 25)
    asof = ds[5]
    insert_run(asof, [("000001", "KS", "kept", {}), ("000003", "KQ", "nobg", {})])
    # 000001: asof 종가 100 → 5일 뒤 110, 10일 뒤 121 / KOSPI: 1000 → 5일 뒤 1020, 10일 뒤 1030
    c1 = [100.0] * 5 + [100.0] + [100 + 2 * i for i in range(1, 6)] + [110 + 2.2 * i for i in range(1, 15)]
    c1 = c1[:25]
    ks = [1000.0] * 5 + [1000.0] + [1000 + 4 * i for i in range(1, 6)] + [1020 + 2 * i for i in range(1, 15)]
    kq = [700.0] * 25
    c3 = [50.0] * 25
    bars = {"000001.KS": bars_from(ds, c1), "000003.KQ": bars_from(ds, c3)}
    idx = {"^KS11": list(zip(ds, ks[:25])), "^KQ11": list(zip(ds, kq))}
    n = S.fill_outcomes(db.session, lambda syms: {s: bars[s] for s in syms if s in bars}, lambda sym: idx[sym])
    assert n == 2
    with db.session() as conn:
        r = {x["code"]: x for x in conn.execute("SELECT * FROM screen_items")}
    a = r["000001"]
    assert a["fwd5"] == pytest.approx((c1[10] / c1[5] - 1) * 100, abs=0.01)
    assert a["fwd5_ex"] == pytest.approx((c1[10] / c1[5] - 1) * 100 - (ks[10] / ks[5] - 1) * 100, abs=0.01)
    assert a["fwd10_ex"] == pytest.approx((c1[15] / c1[5] - 1) * 100 - (ks[15] / ks[5] - 1) * 100, abs=0.01)
    assert r["000003"]["fwd5"] == 0 and r["000003"]["fwd5_ex"] == 0                     # 코스닥 종목은 KOSDAQ 대비
    assert S.fill_outcomes(db.session, lambda s: {}, lambda s: []) == 0               # 이미 채워진 것은 다시 받지 않는다


def test_fill_skips_when_too_recent_or_data_missing():
    today = date.today()
    insert_run((today - timedelta(days=2)).strftime("%Y-%m-%d"), [("000001", "KS", "kept", {})])    # 아직 5일 안 지남
    assert S.fill_outcomes(db.session, lambda s: 1 / 0, lambda s: 1 / 0) == 0                    # 조회 자체를 하지 않는다
    ds = day_labels(today - timedelta(days=20), 8)
    insert_run(ds[2], [("000002", "KS", "kept", {})])
    short = bars_from(ds, [100.0] * 8)                                                          # 5일 뒤 데이터가 아직 없음(2+5=7 < 8 이므로 있음) → 10일은 없음
    n = S.fill_outcomes(db.session, lambda syms: {s: short for s in syms}, lambda sym: list(zip(ds, [1000.0] * 8)))
    with db.session() as conn:
        r = conn.execute("SELECT fwd5, fwd10 FROM screen_items WHERE code='000002'").fetchone()
    assert r["fwd5"] == 0 and r["fwd10"] is None and n >= 1                                      # 있는 것만 채우고 나머지는 대기


def test_review_groups_by_type_and_flags_small_samples():
    ai = lambda kind, tone=2, ck="지속성": dict(reason_type=kind, news_tone=tone, catalyst_kind=ck)  # noqa: E731
    lead = lambda self_, ex5: dict(is_self=self_, ex5=ex5)  # noqa: E731
    rid = insert_run("2026-11-02", [
        ("000001", "KS", "kept", dict(ai=ai("실적·펀더멘털"), flags=["overheat"], leader=lead(False, 5.0))),
        ("000002", "KS", "kept", dict(ai=ai("업황·테마", 0, "일회성"), leader=lead(False, -3.0))),
        ("000003", "KQ", "nobg", dict(ai=ai("뚜렷한 배경 없음", 0, "불명"))),
        ("000004", "KQ", "kept", {})])
    with db.session() as conn:
        for code, f5, e5, f10, e10 in (("000001", 8.0, 6.0, 12.0, 9.0), ("000002", -4.0, -5.0, None, None),
                                       ("000003", -6.0, -7.0, -9.0, -8.0)):
            conn.execute("UPDATE screen_items SET fwd5=?,fwd5_ex=?,fwd10=?,fwd10_ex=? WHERE run_id=? AND code=?", (f5, e5, f10, e10, rid, code))
        rv = S.review(conn)
    assert rv["n_items"] == 4 and rv["n_done"] == 3 and rv["n_pending"] == 1 and rv["n_runs"] == 1
    g = {x["title"]: {i["label"]: i for i in x["items"]} for x in rv["groups"]}
    assert g["추천 여부"]["추천(화면에 표시)"]["d5"]["n"] == 2 and g["추천 여부"]["AI가 배경 불명으로 제외"]["d5"]["mean"] == -7.0
    assert g["강세 배경(AI)"]["실적·펀더멘털"]["d5"]["mean"] == 6.0 and g["강세 배경(AI)"]["실적·펀더멘털"]["d10"]["hit"] == 100
    assert g["재료 성격(AI)"]["일회성"]["d10"]["n"] == 0                                       # 10일 값 없으면 표본 0
    assert "대장주도 강세" in g["업종 대장주"] and "대장주는 지수 이하" in g["업종 대장주"]
    assert "과열 표시 있음" in g["단기 과열 플래그"] and all(i["small"] for x in rv["groups"] for i in x["items"])   # 표본 30 미만


def test_review_page_renders_with_and_without_data():
    db.init_db()
    with TestClient(main.app) as c:
        html = c.get("/screener/review").text
        assert "강세 종목 성과 검증" in html and "아직 결과가 없는 것이 정상" in html and "과적합" in html
        assert "성과 검증 보기" in c.get("/screener").text
    rid = insert_run("2026-11-02", [("000001", "KS", "kept", dict(ai=dict(reason_type="실적·펀더멘털", news_tone=1, catalyst_kind="지속성")))])
    with db.session() as conn:
        conn.execute("UPDATE screen_items SET fwd5=3.0,fwd5_ex=2.0 WHERE run_id=?", (rid,))
    with TestClient(main.app) as c:
        html = c.get("/screener/review").text
        assert "표본 부족" in html and "+2.0%p" in html and "매도" not in html


def test_review_counts_only_last_run_per_asof_date():
    insert_run("2026-11-02", [("000001", "KS", "kept", {})])
    rid2 = insert_run("2026-11-02", [("000001", "KS", "kept", {})])          # 같은 기준일 재실행
    insert_run("2026-11-03", [("000001", "KS", "kept", {})])
    with db.session() as conn:
        conn.execute("UPDATE screen_items SET fwd5=1.0, fwd5_ex=1.0")
        rv = S.review(conn)
    assert rv["n_runs"] == 2 and rv["n_items"] == 2 and rv["overall"]["d5"]["n"] == 2


# ---------- 하루 급등일 제외 ----------
def jump_bars(jump=20.0, n=25):
    """최근 5일 동안 매일 +1%인데 그중 하루만 jump% 오른 종목."""
    out, c = [], 10_000.0
    for i in range(n):
        d = jump if i == n - 3 else (1.0 if i >= n - 6 else 0.2)
        out.append((f"D{i:03d}", c, 10_000_000))
        c *= 1 + d / 100
    return out


def test_one_day_jump_is_detected_and_excluded_from_passes():
    idx = IDX()
    j = S.evaluate(jump_bars(20.0), idx)
    assert j["max_day"] == pytest.approx(20.0, abs=0.5) and S.has_jump(j) and S.passes_base(j) and not S.passes(j)
    calm = S.evaluate(jump_bars(1.0), idx)
    assert calm["max_day"] < config.SCREEN_MAX_DAY_JUMP and S.passes(calm)
    edge = S.evaluate(jump_bars(config.SCREEN_MAX_DAY_JUMP), idx)
    assert S.has_jump(edge)                                                    # 경계값(15%)은 제외


def test_run_excludes_jumpers_counts_them_and_tracks_for_review():
    seed_master()
    data = {"000001.KS": mk(d2=2.0), "000002.KS": jump_bars(20.0), "000005.KS": mk(d2=1.5)}
    chunk = lambda syms: {s: v for s, v in data.items() if s in syms}  # noqa: E731
    S.run(db.session, chunk, lambda s: idx_bars(), lambda s: 2e12, delay=0, ai=False)
    with db.session() as conn:
        _, run, items = S.latest(conn)
        st = {r["code"]: r["status"] for r in conn.execute("SELECT code,status FROM screen_items")}
    assert "000002" not in [i["code"] for i in items]                          # 화면에는 안 나온다
    assert run["jump_n"] == 1 and st["000002"] == "jump"                       # 개수는 알리고, 성과 비교용으로 기록
    assert {i["code"] for i in items} == {"000001", "000005"}
    assert run["passed_n"] == 2                                                # 통과 수에는 급등 종목이 들어가지 않는다
    with TestClient(main.app) as c:
        html = c.get("/screener").text
    assert "하루 15% 이상 급등일이 있어 제외 1개" in html and "하루에 15% 이상 오른 날이 없고" in html and "강세대형" in html


def test_review_labels_jump_group():
    rid = insert_run("2026-11-02", [("000001", "KS", "kept", {}), ("000002", "KS", "jump", {})])
    with db.session() as conn:
        conn.execute("UPDATE screen_items SET fwd5=1.0, fwd5_ex=-4.0 WHERE code='000002'")
        conn.execute("UPDATE screen_items SET fwd5=1.0, fwd5_ex=2.0 WHERE code='000001'")
        rv = S.review(conn)
    g = {i["label"]: i for i in rv["groups"][0]["items"]}
    assert g["하루 급등(15%+)일 포함으로 제외"]["d5"]["mean"] == -4.0 and g["추천(화면에 표시)"]["d5"]["mean"] == 2.0
