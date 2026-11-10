import pytest
from fastapi.testclient import TestClient

import alerts
import config
import db
import main
import stockviews
from collectors import reports

SUB = dict(endpoint="https://push.example.com/abc", keys=dict(p256dh="p", auth="a"))


def setup_stock(conn, code="005930", name="삼성전자"):
    conn.execute("INSERT INTO watchlist (code,market,name,added_at) VALUES (?,?,?, 'x')", (code, "KS", name))


def add_score(conn, code, score):
    stockviews.save(conn, [stockviews.make_row(code, None, dict(
        crowd_effect=0, crowd_score=score, crowd_evidence=[], crowd_note=None, sector_effect=0, sector_score=score,
        sector_note=None, score=score, comment="c"), [], {})])


class Sender:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, sub, payload, pem):
        self.calls.append((sub, payload))
        return self.ok


def test_fires_once_on_upward_crossing_and_rearms_with_hysteresis():
    db.init_db()
    s = Sender()
    with db.session() as conn:
        setup_stock(conn)
        alerts.subscribe(conn, SUB)
        alerts.set_alert(conn, "005930", True, 70)
        add_score(conn, "005930", 65)
        assert alerts.check_and_notify(conn, s) == []                      # 기준 미만
        add_score(conn, "005930", 72)
        assert alerts.check_and_notify(conn, s) == ["005930"]              # 상향 돌파 → 알림
        add_score(conn, "005930", 80)
        assert alerts.check_and_notify(conn, s) == []                      # 기준 위에 머무는 동안 반복 없음
        add_score(conn, "005930", 68)
        assert alerts.check_and_notify(conn, s) == []                      # 69~ 사이(히스테리시스)는 재무장 안 함
        add_score(conn, "005930", 71)
        assert alerts.check_and_notify(conn, s) == []
        add_score(conn, "005930", 60)
        alerts.check_and_notify(conn, s)                                   # 65 미만으로 내려가 재무장
        add_score(conn, "005930", 75)
        assert alerts.check_and_notify(conn, s) == ["005930"]
    assert len(s.calls) == 2
    payload = s.calls[0][1]
    assert "삼성전자" in payload["title"] and "72" in payload["title"] and "매수 추천이 아닙니다" in payload["body"]


def test_alert_off_or_no_devices_or_failed_view_does_not_fire():
    db.init_db()
    s = Sender()
    with db.session() as conn:
        setup_stock(conn)
        add_score(conn, "005930", 90)
        assert alerts.check_and_notify(conn, s) == []                      # 알림 꺼짐
        alerts.set_alert(conn, "005930", True, 70)
        assert alerts.check_and_notify(conn, s) == []                      # 등록된 기기 없음 → 발송 불가, fired 표시 안 함
        assert conn.execute("SELECT alert_fired FROM watchlist").fetchone()[0] == 0
        alerts.subscribe(conn, SUB)
        stockviews.save(conn, [stockviews.make_row("005930", None, error="fail")])   # 최신 시도가 실패여도 마지막 성공 점수 기준
        assert alerts.check_and_notify(conn, s) == ["005930"]


def test_expired_subscription_is_removed():
    db.init_db()
    with db.session() as conn:
        setup_stock(conn)
        alerts.subscribe(conn, SUB)
        assert alerts.notify_all(conn, dict(title="t", body="b"), Sender(ok=False)) == 0
        assert alerts.subscription_count(conn) == 0


def test_vapid_key_is_stable_and_valid_p256_point():
    db.init_db()
    with db.session() as conn:
        pem1, pub1 = alerts.vapid_keys(conn)
        pem2, pub2 = alerts.vapid_keys(conn)
    assert pem1 == pem2 and pub1 == pub2
    import base64
    raw = base64.urlsafe_b64decode(pub1 + "=" * (-len(pub1) % 4))
    assert len(raw) == 65 and raw[0] == 4


@pytest.mark.parametrize("bad", [None, {}, dict(endpoint="http://x", keys=dict(p256dh="p", auth="a")),
                                 dict(endpoint="https://x", keys={}), dict(endpoint=5, keys=dict(p256dh="p", auth="a"))])
def test_subscribe_rejects_bad_input(bad):
    db.init_db()
    with db.session() as conn, pytest.raises(alerts.AlertError):
        alerts.subscribe(conn, bad)


@pytest.mark.parametrize("m", ["abc", "0", "100", "nan", ""])
def test_set_alert_rejects_bad_threshold(m):
    db.init_db()
    with db.session() as conn:
        setup_stock(conn)
        with pytest.raises(alerts.AlertError):
            alerts.set_alert(conn, "005930", True, m)


def test_set_alert_unknown_stock():
    db.init_db()
    with db.session() as conn, pytest.raises(alerts.AlertError):
        alerts.set_alert(conn, "999999", True, 70)


def test_migration_adds_alert_columns_to_old_watchlist(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE watchlist (code TEXT PRIMARY KEY, market TEXT NOT NULL, name TEXT NOT NULL, added_at TEXT NOT NULL)")
    c.execute("INSERT INTO watchlist VALUES ('005930','KS','삼성전자','x')")
    c.commit(); c.close()
    db.init_db(path); db.init_db(path)
    with db.session(path) as conn:
        r = conn.execute("SELECT alert_on,alert_min,alert_fired FROM watchlist").fetchone()
        assert tuple(r) == (0, None, 0)


def test_routes_and_ui():
    db.init_db()
    with db.session() as conn:
        setup_stock(conn)
    with TestClient(main.app) as c:
        k = c.get("/push/key").json()["key"]
        assert len(k) > 80
        assert c.post("/push/subscribe", json=SUB).json() == {"ok": True}
        assert c.post("/push/subscribe", json={"endpoint": "http://bad"}).status_code == 400
        assert c.post("/push/subscribe", content="notjson").status_code == 400
        html = c.get("/watchlist").text
        assert 'id="push-panel"' in html and "등록된 기기 1대" in html and 'name="min"' in html and 'value="70"' in html
        r = c.post("/watchlist/alert", data=dict(code="005930", on="1", min="65"), follow_redirects=True)
        assert r.status_code == 200 and "알림 켜짐" in r.text and 'value="65"' in r.text
        r = c.post("/watchlist/alert", data=dict(code="005930", on="1", min="150"))
        assert r.status_code == 400 and "1~99" in r.text
        r = c.post("/watchlist/alert", data=dict(code="005930", min="65"), follow_redirects=True)   # 체크 해제
        assert "알림 켜짐" not in r.text
        assert c.post("/push/unsubscribe", json={"endpoint": SUB["endpoint"]}).json() == {"ok": True}
        assert c.post("/push/test").json()["sent"] == 0


def test_push_test_sends_to_registered_device(monkeypatch):
    db.init_db()
    s = Sender()
    monkeypatch.setattr(alerts, "send_push", s)
    with TestClient(main.app) as c:
        c.post("/push/subscribe", json=SUB)
        assert c.post("/push/test").json() == {"ok": True, "sent": 1}
    assert s.calls[0][1]["title"].startswith("내손안의주식")


def test_service_worker_handles_push_and_skips_push_api_cache():
    with TestClient(main.app) as c:
        js = c.get("/sw.js").text
    assert "addEventListener('push'" in js and "notificationclick" in js and "startsWith('/push/')" in js


def test_app_name_and_icons_match_manifest():
    import json
    import struct

    from pathlib import Path
    with TestClient(main.app) as c:
        m = json.loads(c.get("/manifest.webmanifest").text)
        html = c.get("/").text
    assert m["name"] == "내손안의주식" and m["short_name"] == "내손안의주식"
    assert "<title>내손안의주식</title>" in html and "<h1>내손안의주식</h1>" in html and "MacroSight" not in html
    root = Path(main.__file__).parent / "static"
    for icon in m["icons"]:                                              # 선언한 크기와 실제 PNG 크기가 같아야 한다
        data = (root / icon["src"].split("/static/")[1]).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        w, h = struct.unpack(">II", data[16:24])
        assert f"{w}x{h}" == icon["sizes"]
    data = (root / "apple-touch-icon.png").read_bytes()
    assert struct.unpack(">II", data[16:24]) == (180, 180)
