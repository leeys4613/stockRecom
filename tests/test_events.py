import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import alerts
import config
import db
import events as E
import main
from analyzer import event_llm
from tests.conftest import FakeProvider, make_rss

TODAY = date.today()
H1 = "삼성전자, 다음 주 목요일 3분기 잠정실적 발표 예정"
H2 = "삼성전자 신제품 공개 행사 연내 개최 전망"


def d(n):
    return (TODAY + timedelta(days=n)).isoformat()


def ev(**kw):
    base = dict(type="실적발표", title="3분기 잠정실적 발표", date=d(5), date_note="", impact_level=2, direction="호재",
                priced_in_level=1, evidence=H1, reason="시장 기대치와의 차이에 따라 변동이 큰 정기 이벤트")
    base.update(kw)
    return base


def closes(n=260, drift=0.0, last_jump=0.0):
    c, out = 100.0, []
    for i in range(n):
        c *= 1 + (0.3 if i % 2 else -0.3) / 100 + drift / 100
        out.append(c)
    out[-1] *= 1 + last_jump / 100
    return out


# ---------- 순수 계산 ----------
def test_price_stats_fields():
    s = E.price_stats(closes())
    assert set(s) >= {"price", "r5", "r10", "dd60", "avg_abs_move_1y", "p95_abs_move_1y", "big_move_days_1y"}
    assert s["big_move_days_1y"] == 0 and s["avg_abs_move_1y"] == pytest.approx(0.3, abs=0.05) and s["dd60"] <= 0
    big = closes()
    big[100] *= 1.12
    assert E.price_stats(big)["big_move_days_1y"] >= 1
    with pytest.raises(ValueError):
        E.price_stats([1.0] * 10)


def test_assess_keeps_verified_events_only():
    out = event_llm.assess([ev()], [H1], TODAY)
    assert len(out) == 1 and out[0]["date"] == d(5) and out[0]["impact"] == 2 and out[0]["evidence"] == H1
    assert event_llm.assess([ev(evidence="지어낸 헤드라인이라서 근거가 없습니다")], [H1], TODAY) == []        # 근거 없으면 버린다
    assert event_llm.assess([ev(title="")], [H1], TODAY) == []
    assert event_llm.assess(None, [H1], TODAY) == []


def test_assess_date_rules_and_enum_fallbacks():
    far = event_llm.assess([ev(date=d(config.EVENT_MAX_DAYS + 10), date_note="")], [H1], TODAY)[0]
    assert far["date"] is None and far["date_note"] == "날짜 확인 필요"                                  # 너무 먼 날짜는 미확정
    past = event_llm.assess([ev(date=d(-2))], [H1], TODAY)[0]
    assert past["date"] is None                                                                      # 이미 지난 날짜는 일정이 아니다
    bad = event_llm.assess([ev(date="내일쯤", date_note="이달 중")], [H1], TODAY)[0]
    assert bad["date"] is None and bad["date_note"] == "이달 중"
    odd = event_llm.assess([ev(type="외계인", direction="??", impact_level=9, priced_in_level=-3)], [H1], TODAY)[0]
    assert odd["event_type"] == "기타" and odd["direction"] == "불명" and odd["impact"] == 3 and odd["priced_in"] == 0


def test_sell_news_flag_is_computed_by_code_not_ai():
    hot = event_llm.assess([ev(direction="호재", priced_in_level=0)], [H1], TODAY, r10=config.EVENT_RUNUP_PCT + 1)[0]
    assert "sell_news_risk" in hot["flags"]                                                           # 이미 많이 올랐다 → 셀온 가능성
    priced = event_llm.assess([ev(direction="호재", priced_in_level=2)], [H1], TODAY, r10=0.0)[0]
    assert "sell_news_risk" in priced["flags"]
    calm = event_llm.assess([ev(direction="호재", priced_in_level=1)], [H1], TODAY, r10=2.0)[0]
    assert calm["flags"] == []
    bad = event_llm.assess([ev(direction="악재", priced_in_level=3)], [H1], TODAY, r10=30.0)[0]
    assert bad["flags"] == []                                                                         # 악재에는 호재 셀온 표시를 달지 않는다


# ---------- 실행/저장 ----------
class EvProvider(FakeProvider):
    def __init__(self, items=None, **kw):
        super().__init__({}, **kw)
        self.items, self.prompts = items, []

    def complete(self, prompt, system=None, schema=None):
        self.prompts.append(prompt)
        if self.exc:
            raise self.exc
        return dict(items=self.items)


def seed(*codes):
    from tests import test_stock_views as sv
    sv.seed_watchlist(*codes)


def rss(*titles):
    return lambda url: make_rss([(t, f"https://e.com/{i}", 3) for i, t in enumerate(titles)])


def run(items, titles=(H1,), provider=None, **kw):
    p = provider or EvProvider(items)
    rid = E.run(db.session, p, rss(*titles), lambda sym: closes(), today=TODAY, **kw)
    return rid, p


def rows():
    with db.session() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id")]


def test_run_extracts_saves_and_sends_context(monkeypatch):
    monkeypatch.setattr(config, "STOCK_VIEW_DELAY_SEC", 0)
    seed("005930")
    rid, p = run([dict(code="005930", events=[ev()])])
    with db.session() as conn:
        r = dict(conn.execute("SELECT * FROM event_runs WHERE id=?", (rid,)).fetchone())
    assert r["status"] == "ok" and r["n_stocks"] == 1 and r["n_events"] == 1
    e = rows()[0]
    assert e["code"] == "005930" and e["date"] == d(5) and e["impact"] == 2 and json.loads(e["evidence"]) == [H1] and e["price_seen"] > 0
    ctx = json.loads(p.prompts[0].split("<input>")[1].split("</input>")[0])
    s0 = ctx["stocks"][0]
    assert ctx["today"] == TODAY.isoformat() and s0["headlines"][0]["title"] == H1 and s0["headlines"][0]["date"]
    assert "avg_abs_move_1y" in s0["price"] and s0["past_reactions"] == []                          # 과거 데이터(변동성)를 AI에 준다


def test_same_event_is_not_duplicated_and_undated_gets_its_date(monkeypatch):
    monkeypatch.setattr(config, "STOCK_VIEW_DELAY_SEC", 0)
    seed("005930")
    run([dict(code="005930", events=[ev(date="", date_note="다음 주")])])
    assert len(rows()) == 1 and rows()[0]["date"] is None
    run([dict(code="005930", events=[ev(date=d(6), title="3분기 잠정 실적 발표일 확정")])])       # 뒤늦게 날짜 확정 + 표현 변경
    r = rows()
    assert len(r) == 1 and r[0]["date"] == d(6) and r[0]["ukey"].endswith(d(6))
    run([dict(code="005930", events=[ev(date=d(6))])])
    assert len(rows()) == 1                                                                         # 매일 같은 이벤트가 나와도 한 건


def test_ai_failure_and_no_headlines_are_recorded(monkeypatch):
    monkeypatch.setattr(config, "STOCK_VIEW_DELAY_SEC", 0)
    seed("005930")
    rid, _ = run(None, provider=EvProvider(exc=RuntimeError("limit reached")))
    with db.session() as conn:
        r = conn.execute("SELECT * FROM event_runs WHERE id=?", (rid,)).fetchone()
    assert r["status"] == "failed" and "limit reached" in r["error"] and rows() == []
    p = EvProvider([])
    rid = E.run(db.session, p, lambda url: make_rss([]), lambda s: closes(), today=TODAY)
    assert p.prompts == []                                                                          # 기사가 없으면 AI 를 부르지 않는다
    with db.session() as conn:
        assert conn.execute("SELECT status,n_events FROM event_runs WHERE id=?", (rid,)).fetchone()["n_events"] == 0


def test_universe_is_watchlist_then_screener_picks_deduped(monkeypatch):
    seed("005930", "000660")
    with db.session() as conn:
        rid = conn.execute("INSERT INTO screen_runs (created_at,status,asof) VALUES ('2026-11-01T00:00:00+00:00','ok','2026-11-01')").lastrowid
        for code, name, st in (("005930", "삼성전자", "kept"), ("123456", "강세종목", "kept"), ("654321", "제외종목", "nobg")):
            conn.execute("INSERT INTO screen_items (run_id,rank,code,name,market,status) VALUES (?,?,?,?,?,?)", (rid, 1, code, name, "KS", st))
        u = E.universe(conn)
    assert {x["code"] for x in u[:2]} == {"005930", "000660"} and u[2]["code"] == "123456" and u[2]["src"] == "screener"   # 관심종목 먼저, 그다음 강세 종목


# ---------- 알림 ----------
SUB = dict(endpoint="https://push.example.com/e", keys=dict(p256dh="p", auth="a"))


class Sender:
    def __init__(self):
        self.calls = []

    def __call__(self, sub, payload, pem):
        self.calls.append(payload)
        return True


def put_event(conn, code="005930", days=3, impact=2, flags="[]", name="삼성전자"):
    conn.execute("INSERT INTO events (code,event_type,ukey,name,market,title,date,impact,direction,priced_in,flags,first_seen,last_seen,price_seen,r10_seen) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (code, "실적발표", f"{code}|{days}|{impact}", name, "KS", "3분기 잠정실적 발표", d(days), impact, "호재", 1, flags, "x", "x", 100.0, 5.0))


def test_alerts_fire_once_per_band_only_for_watchlist_and_big_events():
    seed("005930")
    s = Sender()
    with db.session() as conn:
        alerts.subscribe(conn, SUB)
        put_event(conn, "005930", 3, 2, flags='["sell_news_risk"]')
        put_event(conn, "000660", 3, 3, name="없는종목")            # 관심종목 아님
        put_event(conn, "005930", 2, 1)                              # 파괴력 보통 → 알리지 않음
        put_event(conn, "005930", 40, 3)                             # 아직 멀다
        assert len(E.check_alerts(conn, TODAY, s)) == 1
        assert E.check_alerts(conn, TODAY, s) == []                  # 같은 구간에서는 반복하지 않는다
    p = s.calls[0]
    assert "삼성전자" in p["title"] and "D-3" in p["title"] and "셀온 가능성" in p["body"] and "매매 권유가 아닙니다" in p["body"]


def test_alert_bands_progress_and_catch_up_on_weekends():
    seed("005930")
    s = Sender()
    with db.session() as conn:
        alerts.subscribe(conn, SUB)
        put_event(conn, "005930", 5, 3)
        assert E.check_alerts(conn, TODAY, s) == []                                    # 5일 전: 아직 알림 구간 아님(3·1일)
        assert len(E.check_alerts(conn, TODAY + timedelta(days=3), s)) == 1            # 2일 남음: 3일 구간(주말로 3일 전을 놓쳐도 보충)
        assert E.check_alerts(conn, TODAY + timedelta(days=3), s) == []
        assert len(E.check_alerts(conn, TODAY + timedelta(days=4), s)) == 1            # 1일 전: 한 번 더
        assert E.check_alerts(conn, TODAY + timedelta(days=4), s) == []
        assert E.check_alerts(conn, TODAY + timedelta(days=6), s) == []                # 지난 이벤트는 알리지 않는다
    assert len(s.calls) == 2


def test_no_devices_does_not_mark_sent():
    seed("005930")
    with db.session() as conn:
        put_event(conn, "005930", 1, 3)
        assert E.check_alerts(conn, TODAY, Sender()) == []
        assert conn.execute("SELECT COUNT(*) FROM event_alerts_sent").fetchone()[0] == 0   # 기기가 없으면 보낸 것으로 치지 않는다
        alerts.subscribe(conn, SUB)
        assert len(E.check_alerts(conn, TODAY, Sender())) == 1


# ---------- 실제 반응 채우기 ----------
def test_fill_outcomes_computes_pre_and_post_reaction():
    seed("005930")
    ev_date = TODAY - timedelta(days=8)
    with db.session() as conn:
        conn.execute("INSERT INTO events (code,event_type,ukey,name,market,title,date,impact,direction,priced_in,first_seen,last_seen,price_seen) "
                     "VALUES ('005930','실적발표','k','삼성전자','KS','발표',?,2,'호재',1,'x','x',100.0)", (ev_date.isoformat(),))
    ds = [(ev_date + timedelta(days=i - 5)).isoformat() for i in range(12)]          # 이벤트일은 인덱스 5
    cl = [100, 102, 104, 106, 110, 99, 98, 97, 96, 95, 94, 93]                       # 전날(인덱스 4, 110)까지 +10%, 이벤트일(99) 포함 3거래일 뒤(인덱스 7, 97)
    bars = [(dd, c, 1000.0) for dd, c in zip(ds, cl)]
    n = E.fill_outcomes(db.session, lambda syms: {s: bars for s in syms}, TODAY)
    assert n == 1
    r = rows()[0]
    assert r["pre_ret"] == pytest.approx(10.0, abs=0.01) and r["post_ret"] == pytest.approx((97 / 110 - 1) * 100, abs=0.01)
    assert E.fill_outcomes(db.session, lambda s: {}, TODAY) == 0                       # 다시 받지 않는다


def test_fill_skips_recent_events_and_missing_data():
    seed("005930")
    with db.session() as conn:
        conn.execute("INSERT INTO events (code,event_type,ukey,name,market,title,date,impact,direction,priced_in,first_seen,last_seen,price_seen) "
                     "VALUES ('005930','실적발표','k','삼성전자','KS','발표',?,2,'호재',1,'x','x',100.0)", (d(-2),))
    assert E.fill_outcomes(db.session, lambda s: 1 / 0, TODAY) == 0                    # 아직 5일이 안 지남 → 조회도 안 한다


# ---------- 화면 ----------
def test_page_and_view_sections(monkeypatch):
    monkeypatch.setattr(config, "STOCK_VIEW_DELAY_SEC", 0)
    seed("005930")
    run([dict(code="005930", events=[ev(date=d(2), impact_level=3, priced_in_level=3),
                                     ev(type="신제품·계약", title="신제품 공개 행사", date="", date_note="연내", evidence=H2, impact_level=1)])],
        titles=(H1, H2))
    with TestClient(main.app) as c:
        html = c.get("/events").text
    assert "이벤트 일정 · 파괴력" in html and "D-2" in html and "파괴력 파괴적" in html and "호재 선반영 · 셀온 가능성" in html
    assert "일정 미확정" in html and "연내(날짜 미확정)" in html and "근거 기사" in html and H1 in html
    assert "틀릴 수 있으니 원문을 꼭 확인" in html and "투자 권유가 아닙니다" in html and "매도" not in html
    assert "아직 지난 이벤트가 없습니다" in html and "아직 통계가 없습니다" in html


def test_stats_table_flags_small_samples(monkeypatch):
    seed("005930")
    with db.session() as conn:
        for i in range(3):
            put_event(conn, "005930", -10 - i, 2)
            conn.execute("UPDATE events SET pre_ret=?, post_ret=? WHERE id=(SELECT MAX(id) FROM events)", (6.0, -4.0))
        v = E.view(conn, TODAY)
    g = {x["title"]: x["items"] for x in v["stats"]}
    it = g["이벤트 유형별"][0]
    assert it["label"] == "실적발표" and it["n"] == 3 and it["pre"] == 6.0 and it["post"] == -4.0 and it["drop"] == 100 and it["small"] is True
    with TestClient(main.app) as c:
        assert "표본 부족(3건)" in c.get("/events").text


def test_refresh_route_and_job(monkeypatch):
    calls = []
    monkeypatch.setattr(E, "run", lambda f, *a, **k: calls.append("run"))
    with TestClient(main.app) as c:
        assert c.post("/events/refresh", follow_redirects=False).status_code == 303
    assert "run" in calls
    assert "events-daily" in [j.id for j in main.build_scheduler().get_jobs()]
