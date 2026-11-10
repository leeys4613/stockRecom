from datetime import date

import calendar_kr
import config
import db
import main
import pipeline
from tests.conftest import GOOD, FakeProvider, fake_rss, history_down, macro_ok


def run(provider, macro_fetcher=macro_ok, history_fetcher=None):
    db.init_db()
    return pipeline.run_once(provider, macro_fetcher, fake_rss, history_fetcher=history_fetcher)


def latest():
    with db.session() as conn:
        return pipeline.latest_analysis(conn)


def test_score_plus_adjustment_saved_with_inputs():
    assert run(FakeProvider(GOOD)) is not None
    a = latest()
    assert a["llm_status"] == "ok" and a["provider"] == "fake"
    assert a["adjustment"] == 4
    assert a["final_score"] == min(100, round(a["base_score"] + 4, 1))
    assert a["summary"] == ["a", "b", "c", "d"] and a["allocation"] is not None
    with db.session() as conn:
        assert "macro" in conn.execute("SELECT inputs FROM analyses").fetchone()[0]


def test_llm_failure_falls_back_to_rule_score():
    run(FakeProvider(exc=RuntimeError("limit")))
    a = latest()
    assert a["llm_status"] == "failed" and a["adjustment"] == 0
    assert a["final_score"] == a["base_score"] and a["summary"] == []
    assert "limit" in a["error"]


def test_failed_indicator_excluded_and_reported_missing():
    def fetch(symbol):
        if symbol == "^SOX":
            raise RuntimeError("down")
        return macro_ok(symbol)
    run(FakeProvider(GOOD), fetch)
    a = latest()
    assert "SOX" in a["missing"] and a["coverage"] < 1.0


def test_all_indicators_failed_skips_llm():
    def fetch(symbol):
        raise RuntimeError("down")
    p = FakeProvider(GOOD)
    run(p, fetch, history_down)          # 추세 지표(KOSPI/VIX 이력)도 실패
    a = latest()
    assert a["llm_status"] == "skipped" and a["final_score"] is None
    assert p.calls == 0 and a["allocation"] is None


def test_concurrent_run_is_skipped():
    db.init_db()
    assert pipeline._lock.acquire(blocking=False)
    try:
        assert pipeline.run_once(FakeProvider(GOOD), macro_ok, fake_rss) is None
    finally:
        pipeline._lock.release()


def test_signal_thresholds():
    db.init_db()
    for score, sig in ((75, "green"), (50, "yellow"), (20, "red")):
        with db.session() as conn:
            conn.execute("INSERT INTO analyses (created_at,llm_status,final_score,base_score,"
                         "coverage,adjustment) VALUES ('2026-01-01T00:00:00+00:00','ok',?,?,1,0)",
                         (score, score))
            assert pipeline.latest_analysis(conn)["signal"] == sig


def test_trading_calendar():
    assert calendar_kr.is_trading_day(date(2026, 11, 9))       # 월요일
    assert not calendar_kr.is_trading_day(date(2026, 11, 7))   # 토요일
    assert not calendar_kr.is_trading_day(date(2026, 12, 25))  # 성탄절
    assert not calendar_kr.is_trading_day(date(2026, 5, 1))    # 근로자의 날(KRX 휴장)
    assert not calendar_kr.is_trading_day(date(2026, 12, 31))  # KRX 연말 휴장


def test_scheduled_job_skips_non_trading_day(monkeypatch):
    monkeypatch.setattr(calendar_kr, "today_kst", lambda: date(2026, 11, 8))  # 일요일
    monkeypatch.setattr(pipeline, "run_once", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert pipeline.scheduled_job() is None


def test_scheduler_jobs_analysis_times_plus_daily_reports():
    s = main.build_scheduler()
    ids = {j.id for j in s.get_jobs()}
    assert len(config.SCHEDULE_TIMES) == 4
    assert {f"analysis-{h:02d}{m:02d}" for h, m in config.SCHEDULE_TIMES} <= ids
    assert {"reports-daily", "screen-daily", "events-daily"} <= ids and len(ids) == 7


def test_reports_job_skips_non_trading_day(monkeypatch):
    monkeypatch.setattr(calendar_kr, "today_kst", lambda: date(2026, 11, 8))  # 일요일
    monkeypatch.setattr(pipeline, "run_reports", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert pipeline.scheduled_reports_job() is None
