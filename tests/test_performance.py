from datetime import date

import pytest

import db
import performance as perf

# 2026-11: 06(금) 09(월) 10 11 12 13 16(월)
CLOSES = [(date(2026, 11, d), 100.0 + i) for i, d in enumerate([6, 9, 10, 11, 12, 13, 16])]


def test_before_close_uses_previous_close():
    # 11-09(월) 07:30 KST = 11-08 22:30 UTC → 기준 11-06 종가(100)
    o = perf.compute_outcome("2026-11-08T22:30:00+00:00", CLOSES)
    assert o["ref_date"] == "2026-11-06" and o["ref_close"] == 100.0
    assert o["d1_date"] == "2026-11-09" and o["d1_ret"] == pytest.approx(1.0)
    assert o["d5_date"] == "2026-11-13" and o["d5_ret"] == pytest.approx(5.0)


def test_after_close_uses_same_day_close():
    # 11-09 16:30 KST = 07:30 UTC → 기준 11-09 종가(101)
    o = perf.compute_outcome("2026-11-09T07:30:00+00:00", CLOSES)
    assert o["ref_date"] == "2026-11-09"
    assert o["d1_date"] == "2026-11-10" and o["d1_ret"] == pytest.approx((102 / 101 - 1) * 100)


def test_weekend_uses_last_trading_close():
    # 11-08(일) 12:00 KST → 기준 11-06
    o = perf.compute_outcome("2026-11-08T03:00:00+00:00", CLOSES)
    assert o["ref_date"] == "2026-11-06"


def test_after_close_without_todays_data_is_deferred():
    # 11-17(화) 16:30 KST 인데 데이터는 11-16까지 → 잘못된 기준을 잡지 않고 대기
    assert perf.compute_outcome("2026-11-17T07:30:00+00:00", CLOSES) is None


def test_partial_outcome_when_future_not_available():
    # 11-12 07:30 KST → 기준 11-11(인덱스 3). d1 은 있고 d5 는 아직 없음
    o = perf.compute_outcome("2026-11-11T22:30:00+00:00", CLOSES)
    assert o["ref_date"] == "2026-11-11"
    assert o["d1_ret"] is not None and o["d5_ret"] is None and o["d5_date"] is None


def insert_analysis(conn, created_at, score):
    cur = conn.execute(
        "INSERT INTO analyses (created_at,llm_status,final_score,base_score,coverage,adjustment) "
        "VALUES (?,?,?,?,1,0)", (created_at, "ok", score, score))
    return cur.lastrowid


def test_fill_outcomes_incremental_and_failure_safe():
    db.init_db()
    with db.session() as conn:
        aid = insert_analysis(conn, "2026-11-11T22:30:00+00:00", 70)  # 11-12 07:30 KST
        skipped = conn.execute(  # 점수 없는(skipped) 분석은 대상 아님
            "INSERT INTO analyses (created_at,llm_status,adjustment) VALUES "
            "('2026-11-11T22:30:00+00:00','skipped',0)").lastrowid
        r = perf.fill_outcomes(conn, lambda: CLOSES)
        assert r == dict(status="ok", updated=1, error=None)
        row = conn.execute("SELECT * FROM outcomes WHERE analysis_id=?", (aid,)).fetchone()
        assert row["d1_ret"] is not None and row["d5_ret"] is None
        assert conn.execute("SELECT 1 FROM outcomes WHERE analysis_id=?", (skipped,)).fetchone() is None

        # d5 가 아직 비어 있으니 다음 실행에서 다시 대상이 된다 (새 데이터가 오면 채워짐)
        more = CLOSES + [(date(2026, 11, d), 107.0 + d) for d in (17, 18, 19)]
        perf.fill_outcomes(conn, lambda: more)
        row = conn.execute("SELECT * FROM outcomes WHERE analysis_id=?", (aid,)).fetchone()
        assert row["d5_ret"] is not None

        # 완료된 건은 더 이상 데이터를 가져오지 않는다
        def boom():
            raise AssertionError("fetch 호출되면 안 됨")
        assert perf.fill_outcomes(conn, boom)["updated"] == 0


def test_fill_outcomes_fetch_failure_returns_status():
    db.init_db()
    with db.session() as conn:
        insert_analysis(conn, "2026-11-11T22:30:00+00:00", 70)

        def boom():
            raise TimeoutError("yf down")
        r = perf.fill_outcomes(conn, boom)
        assert r["status"] == "failed" and "yf down" in r["error"]


def test_summarize_metrics_and_small_sample_flag():
    db.init_db()
    with db.session() as conn:
        # (score, d1_ret): 적중 2, 불일치 1, 하락 후 낮은 점수 적중 1
        data = [(80, 1.0), (65, -0.5), (55, 0.4), (20, -1.0)]
        for i, (score, ret) in enumerate(data):
            aid = insert_analysis(conn, "2026-11-0%dT00:00:00+00:00" % (i + 1), score)
            conn.execute("INSERT INTO outcomes (analysis_id,ref_date,ref_close,d1_ret,updated_at)"
                         " VALUES (?,?,100,?, 'x')", (aid, "2026-11-0%d" % (i + 1), ret))
        s = perf.summarize(conn, "d1")
    assert s["n"] == 4 and s["n_days"] == 4
    assert s["hit_rate"] == pytest.approx(3 / 4)       # 80↑ 맞음, 65↑ 틀림, 55↑ 맞음, 20↓ 맞음
    assert s["base_up_rate"] == pytest.approx(0.5)
    expected = ((0.8 - 1) ** 2 + (0.65 - 0) ** 2 + (0.55 - 1) ** 2 + (0.2 - 0) ** 2) / 4
    assert s["brier"] == pytest.approx(expected)
    assert s["brier_baseline"] == pytest.approx(0.25)
    assert s["bands"]["green"]["n"] == 2 and s["bands"]["yellow"]["n"] == 1
    assert s["bands"]["red"]["avg_ret"] == pytest.approx(-1.0)
    assert s["enough"] is False  # 서로 다른 기준일 30일 미만


def test_summarize_empty():
    db.init_db()
    with db.session() as conn:
        s = perf.summarize(conn, "d5")
    assert s["n"] == 0 and s["hit_rate"] is None and s["enough"] is False
