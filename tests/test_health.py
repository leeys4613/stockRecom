from fastapi.testclient import TestClient


def test_health_and_db_created(tmp_path, monkeypatch):
    import config
    dbfile = tmp_path / "t.db"
    monkeypatch.setattr(config, "DB_PATH", dbfile)
    import main
    with TestClient(main.app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
    assert dbfile.exists()
