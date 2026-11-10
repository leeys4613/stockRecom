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
