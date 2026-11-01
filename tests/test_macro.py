from fastapi.testclient import TestClient

import config
import db
from collectors import macro


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


def test_dashboard_with_partial_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(macro, "collect_all", lambda: [
        macro.collect_one(k, fake_fetcher) for k in config.MACRO_INDICATORS])
    import main
    with TestClient(main.app) as client:
        r = client.get("/")
        assert r.status_code == 200 and "데이터 없음" in r.text
        r = client.post("/refresh")  # 리다이렉트 후 대시보드
        assert r.status_code == 200
        assert "수집 실패" in r.text and "boom" in r.text
        assert "최신" in r.text and "WTI" in r.text
