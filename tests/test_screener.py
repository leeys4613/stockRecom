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
        assert "강세대형" not in c.get("/screener?m=KQ").text and "강세대형" in c.get("/screener?m=KS").text
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
        a = next(i for i in S.latest(conn)[2] if i["code"] == "000001")["ai"]
    assert a["news_tone"] == 0 and a["reason_type"] == "뚜렷한 배경 없음" and a["reason"] is None and "기사를 찾지 못했습니다" in a["tone_note"]


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
    S.run(db.session, lambda syms: {}, lambda s: idx_bars(), lambda s: 2e12, delay=0, provider=ScreenProvider(ai_payload()))
    with db.session() as conn:
        _, run, items = S.latest(conn)
    assert run["ai_status"] == "skipped" and items == []
