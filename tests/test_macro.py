from fastapi.testclient import TestClient

import config
import main
import pipeline
from collectors import macro
from tests.conftest import GOOD, FakeProvider, fake_rss


def fake_fetcher(symbol):
    if symbol == "^SOX":
        raise RuntimeError("boom")
    return 110.0, 100.0


def test_collect_all_handles_failure():
    rows = macro.collect_all(fake_fetcher)
    assert len(rows) == len(config.MACRO_INDICATORS)
    by = {r["key"]: r for r in rows}
    assert by["SOX"]["status"] == "failed" and "boom" in by["SOX"]["error"]
    assert by["WTI"]["status"] == "ok"
    assert abs(by["WTI"]["change_pct"] - 10.0) < 1e-9


def test_dashboard_with_partial_failure(monkeypatch):
    real = pipeline.run_once
    monkeypatch.setattr(pipeline, "run_once",
                        lambda: real(FakeProvider(GOOD), fake_fetcher, fake_rss))
    with TestClient(main.app) as client:
        r = client.get("/")
        assert r.status_code == 200 and "아직 분석 결과가 없습니다" in r.text
        assert "데이터 없음" in client.get("/market").text
        r = client.post("/refresh")  # 백그라운드 작업 완료 후 대시보드
        assert r.status_code == 200
        assert "매수 가능성 점수" in r.text and "AI 요약" in r.text and "SOX" in r.text and "점수에서 제외" in r.text
        m = client.get("/market")
        assert m.status_code == 200
        assert "수집 실패" in m.text and "boom" in m.text and "최신" in m.text and "WTI" in m.text
        assert "연준 금리 동결" in m.text and 'href="https://example.com/a"' in m.text
        assert "javascript:" not in m.text
