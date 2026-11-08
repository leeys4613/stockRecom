import json

from fastapi.testclient import TestClient

import config
import db
import main


def client():
    return TestClient(main.app)


def test_manifest_valid_and_icons_exist():
    with client() as c:
        r = c.get("/manifest.webmanifest")
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
        m = json.loads(r.text)
        assert m["display"] == "standalone" and m["start_url"] == "/"
        sizes = {i["sizes"] for i in m["icons"]}
        assert {"192x192", "512x512"} <= sizes
        for icon in m["icons"]:
            ir = c.get(icon["src"])
            assert ir.status_code == 200 and ir.content[:8] == b"\x89PNG\r\n\x1a\n"
        assert c.get("/static/apple-touch-icon.png").status_code == 200


def test_service_worker_root_scope_and_not_cached():
    with client() as c:
        r = c.get("/sw.js")
        assert r.status_code == 200 and "javascript" in r.headers["content-type"]
        assert r.headers["cache-control"] == "no-cache"
        assert "fetch" in r.text and "POST" not in r.text  # GET 만 가로챈다


def test_sw_never_caches_health_and_page_has_offline_banner():
    with client() as c:
        sw = c.get("/sw.js").text
        assert "pathname === '/health'" in sw  # 온라인 판별용 요청은 캐시하지 않음
        html = c.get("/").text
        assert 'id="offline-banner"' in html and "fetch('/health'" in html
        assert "저장된 화면입니다" in html and "data-generated=" in html


def test_pages_link_pwa_and_nav():
    with client() as c:
        for path in ("/", "/performance"):
            html = c.get(path).text
            assert 'rel="manifest"' in html and "apple-touch-icon" in html
            assert "serviceWorker.register('/sw.js')" in html
            assert 'href="/performance"' in html and "화면 생성" in html


def test_performance_page_empty():
    with client() as c:
        r = c.get("/performance")
        assert r.status_code == 200
        assert "아직 결과가 채워진 예측이 없습니다" in r.text
        assert "예측 기록이 없습니다" in r.text


def test_performance_page_with_data_flags_small_sample():
    with client() as c:  # lifespan 이 DB 를 초기화한다
        with db.session() as conn:
            for i, (score, ret) in enumerate([(70, 1.0), (30, -0.5)]):
                aid = conn.execute(
                    "INSERT INTO analyses (created_at,llm_status,final_score,base_score,coverage,"
                    "adjustment) VALUES (?, 'ok', ?, ?, 1, 0)",
                    ("2026-11-0%dT00:00:00+00:00" % (i + 1), score, score)).lastrowid
                conn.execute("INSERT INTO outcomes (analysis_id,ref_date,ref_close,d1_ret,updated_at)"
                             " VALUES (?,?,100,?, 'x')", (aid, "2026-11-0%d" % (i + 1), ret))
        r = c.get("/performance")
        assert r.status_code == 200
        assert "표본 부족" in r.text and "방향 적중률" in r.text and "Brier" in r.text
        assert "대기" in r.text  # 5일 후 결과는 아직 없음


def test_server_binds_only_via_launchd_config_to_localhost():
    plist = (config.BASE_DIR / "scripts" / "com.stockrecom.server.plist").read_text()
    assert "127.0.0.1" in plist and "0.0.0.0" not in plist
