from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
import db
import main
import pipeline
from collectors import reports
from scoring import picks
from tests.conftest import consensus

NOW = datetime(2026, 11, 9, 7, 0, tzinfo=timezone.utc)


def iso(days_ago=0):
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


def add(conn, code="005930", market="KS", name="삼성전자", **fields):
    conn.execute("INSERT OR IGNORE INTO watchlist (code,market,name,added_at) VALUES (?,?,?,?)",
                 (code, market, name, iso(30)))
    return code


def snap(conn, code, days_ago=0, status="ok", error=None, **over):
    d = dict(price=100.0, target_mean=130.0, target_high=160.0, target_low=100.0, target_median=130.0,
             rec_mean=1.8, rec_key="buy", n_analysts=12, strong_buy=4, buy=6, hold=2, sell=0, strong_sell=0)
    d.update(over)
    if status == "failed":
        d = {k: None for k in d}
    conn.execute(
        "INSERT INTO consensus_snapshots (code,status,error,fetched_at,price,target_mean,target_high,target_low,"
        "target_median,rec_mean,rec_key,n_analysts,strong_buy,buy,hold,sell,strong_sell) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, status, error, iso(days_ago), d["price"], d["target_mean"], d["target_high"], d["target_low"],
         d["target_median"], d["rec_mean"], d["rec_key"], d["n_analysts"], d["strong_buy"], d["buy"],
         d["hold"], d["sell"], d["strong_sell"]))


def evaluated(conn):
    return {e["code"]: e for e in picks.top_picks(reports.stock_rows(conn), NOW)[1]}


# ---------- 수집/관리 ----------
def test_collect_one_ok_and_failed_are_recorded_not_raised():
    ok = reports.collect_one("005930", "KS", lambda sym: consensus())
    assert ok["status"] == "ok" and ok["target_mean"] == 130.0 and ok["n_analysts"] == 12
    boom = reports.collect_one("005930", "KS", lambda sym: (_ for _ in ()).throw(TimeoutError("yf down")))
    assert boom["status"] == "failed" and "yf down" in boom["error"] and boom["target_mean"] is None


def test_symbol_building_uses_validated_code_and_market():
    seen = []
    reports.collect_one("005930", "KQ", lambda sym: seen.append(sym) or consensus())
    assert seen == ["005930.KQ"]


def test_add_stock_validates_and_checks_by_fetching():
    db.init_db()
    with db.session() as conn:
        assert reports.add_stock(conn, " 005930 ", "ks", "  삼성   전자 ", lambda s: consensus()) == "005930"
        w = reports.watchlist(conn)[0]
        assert w["name"] == "삼성 전자" and w["market"] == "KS"
        assert conn.execute("SELECT COUNT(*) FROM consensus_snapshots").fetchone()[0] == 1  # 첫 스냅샷 저장
        for code, market, msg in (("12345", "KS", "6자리"), ("ABCDEFG", "KS", "6자리"), ("0059 0", "KS", "6자리"),
                                  ("005930; DROP", "KS", "6자리"), ("000660", "NY", "시장"),
                                  ("005930", "KS", "이미")):
            with pytest.raises(reports.WatchlistError, match=msg):
                reports.add_stock(conn, code, market, "x", lambda s: consensus())
        assert len(reports.watchlist(conn)) == 1


def test_add_stock_rejects_unfetchable_symbol_without_adding():
    db.init_db()
    with db.session() as conn:
        def none(sym):
            raise ValueError("조회 결과 없음")
        with pytest.raises(reports.WatchlistError, match="조회 실패"):
            reports.add_stock(conn, "999999", "KS", "없는종목", none)
        assert reports.watchlist(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM consensus_snapshots").fetchone()[0] == 0


def test_watchlist_max_and_remove_keeps_history(monkeypatch):
    db.init_db()
    monkeypatch.setattr(config, "WATCHLIST_MAX", 2)
    with db.session() as conn:
        reports.add_stock(conn, "000001", "KS", "a", lambda s: consensus())
        reports.add_stock(conn, "000002", "KS", "b", lambda s: consensus())
        with pytest.raises(reports.WatchlistError, match="최대 2개"):
            reports.add_stock(conn, "000003", "KS", "c", lambda s: consensus())
        reports.remove_stock(conn, "000001")
        assert [w["code"] for w in reports.watchlist(conn)] == ["000002"]
        assert conn.execute("SELECT COUNT(*) FROM consensus_snapshots WHERE code='000001'").fetchone()[0] == 1


def test_collect_watchlist_saves_each_and_survives_partial_failure():
    db.init_db()
    with db.session() as conn:
        add(conn, "000001")
        add(conn, "000002")

    def fetch(sym):
        if sym.startswith("000002"):
            raise RuntimeError("boom")
        return consensus()
    assert pipeline.run_reports(fetch, delay=0) == 2
    with db.session() as conn:
        rows = {r["code"]: r["status"] for r in conn.execute("SELECT code,status FROM consensus_snapshots")}
    assert rows == {"000001": "ok", "000002": "failed"}


def test_collect_watchlist_concurrent_run_skipped():
    db.init_db()
    assert reports._lock.acquire(blocking=False)
    try:
        assert pipeline.run_reports(lambda s: consensus(), delay=0) is None
    finally:
        reports._lock.release()


# ---------- 선별 ----------
def test_upside_opinion_trend_and_score():
    db.init_db()
    with db.session() as conn:
        add(conn)
        snap(conn, "005930", days_ago=7, target_mean=120.0)               # 비교 기준(7일 전)
        snap(conn, "005930", days_ago=0, price=100.0, target_mean=132.0, rec_mean=1.0)
        e = evaluated(conn)["005930"]
    assert e["upside"] == pytest.approx(0.32) and e["trend"] == pytest.approx(0.10)
    assert e["trend_days"] == pytest.approx(7)
    assert e["eligible"] and e["flags"] == []
    # 괴리 0.32/0.5=0.64, 의견 +1, 추이 0.10/0.10=+1 → 50+50*(0.4*0.64+0.35+0.25)/1
    assert e["score"] == pytest.approx(50 + 50 * (0.4 * 0.64 + 0.35 + 0.25), abs=0.1)


def test_trend_missing_renormalizes_weights():
    db.init_db()
    with db.session() as conn:
        add(conn)
        snap(conn, "005930", target_mean=125.0, rec_mean=3.0)
        e = evaluated(conn)["005930"]
    assert e["trend"] is None and e["eligible"]
    # 괴리 0.25/0.5=0.5, 의견 0 → (0.4*0.5)/(0.4+0.35)
    assert e["score"] == pytest.approx(50 + 50 * (0.4 * 0.5) / 0.75, abs=0.1)


@pytest.mark.parametrize("fields,flag", [
    (dict(n_analysts=3), "few_analysts"),
    (dict(target_mean=None, n_analysts=None), "no_consensus"),
    (dict(target_mean=250.0), "outlier"),            # +150% 괴리: 데이터 이상 의심
    (dict(price=None), "no_consensus"),
])
def test_ineligible_flags(fields, flag):
    db.init_db()
    with db.session() as conn:
        add(conn)
        snap(conn, "005930", **fields)
        top, ev = picks.top_picks(reports.stock_rows(conn), NOW)
    assert flag in ev[0]["flags"] and not ev[0]["eligible"] and top == []


def test_stale_snapshot_excluded_and_recent_failure_is_informational():
    db.init_db()
    with db.session() as conn:
        add(conn, "000001")
        snap(conn, "000001", days_ago=4)                                      # 4일 전 → stale
        add(conn, "000002")
        snap(conn, "000002", days_ago=1)
        snap(conn, "000002", days_ago=0, status="failed", error="TimeoutError: x")  # 최근 실패, 어제 값은 유효
        ev = evaluated(conn)
    assert "stale" in ev["000001"]["flags"] and not ev["000001"]["eligible"]
    assert "failed" in ev["000002"]["flags"] and ev["000002"]["eligible"]
    assert ev["000002"]["snap"]["status"] == "ok"


def test_no_data_only_failure():
    db.init_db()
    with db.session() as conn:
        add(conn)
        snap(conn, "005930", status="failed", error="ValueError: 조회 결과 없음")
        e = evaluated(conn)["005930"]
    assert set(e["flags"]) == {"failed", "no_data"} and not e["eligible"] and e["score"] is None


def test_top_picks_ranked_filtered_and_limited(monkeypatch):
    monkeypatch.setattr(config, "PICK_TOP_N", 2)
    db.init_db()
    with db.session() as conn:
        for code, target, rec in (("000001", 110.0, 3.0), ("000002", 140.0, 1.2), ("000003", 125.0, 1.5),
                                  ("000004", 90.0, 1.0)):   # 000004: 목표가 < 현재가 → 후보 아님
            add(conn, code)
            snap(conn, code, target_mean=target, rec_mean=rec)
        top, _ = picks.top_picks(reports.stock_rows(conn), NOW)
    assert [p["code"] for p in top] == ["000002", "000003"]


def test_trend_baseline_needs_min_days():
    db.init_db()
    with db.session() as conn:
        add(conn)
        snap(conn, "005930", days_ago=3, target_mean=100.0)   # 5일 미만 → 비교 기준 아님
        snap(conn, "005930", days_ago=0, target_mean=130.0)
        e = evaluated(conn)["005930"]
    assert e["trend"] is None


# ---------- 화면 ----------
def client():
    return TestClient(main.app)


def test_dashboard_empty_states():
    with client() as c:
        assert "관심종목을 추가하면" in c.get("/").text
    with client() as c:
        with db.session() as conn:
            add(conn)
            snap(conn, "005930", n_analysts=2)  # 후보 조건 미충족
        assert "조건을 충족하는 후보가 없습니다" in c.get("/").text


def test_dashboard_shows_top_picks_card():
    with client() as c:
        with db.session() as conn:
            add(conn, "005930", name="삼성전자")
            snap(conn, "005930", days_ago=0)
            conn.execute("UPDATE consensus_snapshots SET fetched_at=?", (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
        html = c.get("/").text
    assert 'id="picks"' in html and "삼성전자" in html and "+30%" in html and "컨센서스 점수" in html
    assert "축적 중" in html and "매수 추천이 아닙니다" in html


def test_watchlist_add_remove_flow_and_links(monkeypatch):
    monkeypatch.setattr(reports, "yf_fetch", lambda sym: consensus())
    with client() as c:
        r = c.post("/watchlist/add", data=dict(code="005930", market="KS", name="삼성전자"), follow_redirects=True)
        assert r.status_code == 200 and "추가 완료" in r.text and "삼성전자" in r.text
        assert "https://finance.naver.com/item/main.naver?code=005930" in r.text
        assert "https://consensus.hankyung.com/" in r.text and 'rel="noopener noreferrer"' in r.text
        assert 'href="/watchlist"' in r.text  # 탭
        r = c.post("/watchlist/remove", data=dict(code="005930"), follow_redirects=True)
        assert "관심종목이 없습니다" in r.text


@pytest.mark.parametrize("data,msg", [
    (dict(code="12", market="KS", name="x"), "6자리"),
    (dict(code="005930", market="XX", name="x"), "시장"),
    (dict(market="KS", name="x"), "6자리"),   # code 누락
])
def test_watchlist_add_rejects_invalid(data, msg):
    with client() as c:
        r = c.post("/watchlist/add", data=data)
        assert r.status_code == 400 and msg in r.text
        with db.session() as conn:
            assert reports.watchlist(conn) == []


def test_watchlist_add_unfetchable_shows_error():
    with client() as c:  # conftest 의 yf_fetch 는 네트워크 없음 → 예외
        r = c.post("/watchlist/add", data=dict(code="005930", market="KS", name="x"))
        assert r.status_code == 400 and "조회 실패" in r.text
        with db.session() as conn:
            assert reports.watchlist(conn) == []


def test_watchlist_name_is_escaped(monkeypatch):
    monkeypatch.setattr(reports, "yf_fetch", lambda sym: consensus())
    with client() as c:
        r = c.post("/watchlist/add", data=dict(code="005930", market="KS", name="<script>x</script>"),
                   follow_redirects=True)
        assert "<script>x</script>" not in r.text and "&lt;script&gt;" in r.text


def test_watchlist_page_shows_flags_and_failure_reason():
    with client() as c:
        with db.session() as conn:
            add(conn)
            snap(conn, "005930", target_mean=300.0)               # 괴리 +200%
            snap(conn, "005930", status="failed", error="TimeoutError: slow")
        r = c.get("/watchlist")
    assert "괴리 과대" in r.text and "최근 조회 실패" in r.text and "TimeoutError: slow" in r.text
    assert 'badge fresh">Top Picks 후보</span>' not in r.text  # 후보 배지(설명문과 구분)
