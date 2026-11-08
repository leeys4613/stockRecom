import sqlite3

import pytest
from fastapi.testclient import TestClient

import allocations as A
import config
import db
import main
import pipeline
from scoring import engine
from tests.conftest import GOOD, FakeProvider, fake_rss, macro_ok

FLOORS = [70, 55, 45, 30]
EMINS = [50, 40, 30, 15, 0]
EMAXS = [70, 55, 45, 30, 15]


def good_form(**over):
    d = {f"floor_{i}": v for i, v in enumerate(FLOORS)}
    d.update({f"emin_{i}": v for i, v in enumerate(EMINS)})
    d.update({f"emax_{i}": v for i, v in enumerate(EMAXS)})
    d["note"] = ""
    d.update(over)
    return d


def test_validate_ok_matches_default_bands():
    bands = A.validate(FLOORS, EMINS, EMAXS)
    assert bands == [list(b) for b in config.ALLOCATION_BANDS]
    assert bands[-1][0] == 0  # 마지막 구간 하한은 0 고정


@pytest.mark.parametrize("floors,emins,emaxs", [
    ([55, 70, 45, 30], EMINS, EMAXS),                    # 경계 뒤집힘
    ([70, 70, 45, 30], EMINS, EMAXS),                    # 경계 겹침
    ([101, 55, 45, 30], EMINS, EMAXS),                   # 100 초과
    ([70, 55, 45, 0], EMINS, EMAXS),                     # 하한 0 은 마지막 구간 전용
    ([70.5, 55, 45, 30], EMINS, EMAXS),                  # 정수 아님
    (FLOORS, [50, 40, 30, 15, 0], [71, 55, 45, 30, 15]),  # 주식 상한(70) 초과
    (FLOORS, [60, 40, 30, 15, 0], [50, 55, 45, 30, 15]),  # 최소 > 최대
    (FLOORS, [-1, 40, 30, 15, 0], EMAXS),                # 음수
    (FLOORS, [50, 40, 30, 15, 20], EMAXS),               # 낮은 구간 최소가 더 큼(단조 위반)
    (FLOORS, EMINS, [70, 55, 45, 30, 40]),               # 낮은 구간 최대가 더 큼(단조 위반)
    (FLOORS, EMINS, ["nan", 55, 45, 30, 15]),
    (FLOORS, EMINS, ["abc", 55, 45, 30, 15]),
    (FLOORS[:3], EMINS, EMAXS),                          # 개수 오류
])
def test_validate_rejects(floors, emins, emaxs):
    with pytest.raises(A.AllocationError):
        A.validate(floors, emins, emaxs)


def test_seed_and_versioning():
    db.init_db()
    with db.session() as conn:
        v1 = A.get_active(conn)
        assert v1["id"] == 1 and v1["bands"] == [list(b) for b in config.ALLOCATION_BANDS]
        nid = A.create_version(conn, [80, 60, 50, 35], [40, 30, 20, 10, 0], [60, 50, 40, 25, 10], "보수적")
        assert nid == 2 and A.get_active(conn)["label"] == "v2 보수적"
        assert A.get(conn, 1)["bands"][0][2:] == [50, 70]  # 이전 버전 불변
        with pytest.raises(A.AllocationError):
            A.create_version(conn, FLOORS, EMINS, [70, 55, 45, 30, 99])
        assert len(A.list_all(conn)) == 2


def test_engine_allocation_with_custom_bands_and_boundaries():
    bands = A.validate([80, 60, 50, 35], [40, 30, 20, 10, 0], [60, 50, 40, 25, 10])
    a = engine.allocation(80, bands)          # 경계값은 위 구간
    assert a["label"] == "높음" and (a["equity_min"], a["equity_max"]) == (40, 60)
    assert (a["cash_min"], a["cash_max"]) == (40, 60)
    assert engine.allocation(79.9, bands)["label"] == "다소 높음"
    assert engine.allocation(0, bands)["label"] == "낮음"
    assert engine.allocation(100, bands)["equity_max"] <= config.EQUITY_CAP_PCT


def test_engine_cap_is_enforced_even_for_bad_bands():
    # 검증을 우회해 직접 넘겨도 엔진이 상한을 지킨다 (안전 장치 이중화)
    a = engine.allocation(90, [[0, "높음", 50, 100]])
    assert a["equity_max"] == config.EQUITY_CAP_PCT and a["cash_min"] == 100 - config.EQUITY_CAP_PCT


def test_migration_adds_allocation_set_to_existing_db(tmp_path):
    path = tmp_path / "old2.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE analyses (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            llm_status TEXT NOT NULL, provider TEXT, base_score REAL, adjustment INTEGER NOT NULL DEFAULT 0,
            final_score REAL, coverage REAL, missing TEXT, summary TEXT, risk_comment TEXT, error TEXT, inputs TEXT,
            weight_set_id INTEGER);
        CREATE TABLE weight_sets (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            label TEXT NOT NULL, weights TEXT NOT NULL, saturation TEXT NOT NULL);
        INSERT INTO weight_sets (created_at,label,weights,saturation) VALUES ('x','v1 초기안','{}','{}');
        INSERT INTO analyses (created_at,llm_status,final_score,weight_set_id)
            VALUES ('2026-11-08T00:00:00+00:00','ok',52.0,1);
    """)
    old.commit()
    old.close()
    db.init_db(path)
    db.init_db(path)  # 멱등
    with db.session(path) as conn:
        assert conn.execute("SELECT allocation_set_id FROM analyses").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM allocation_sets").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM weight_sets").fetchone()[0] == 1  # 기존 가중치 시드 유지


def test_past_analysis_keeps_its_own_allocation_rule():
    db.init_db()
    pipeline.run_once(FakeProvider(GOOD), macro_ok, fake_rss)  # 규칙 v1 로 기록
    with db.session() as conn:
        before = pipeline.latest_analysis(conn)
        A.create_version(conn, [90, 80, 70, 60], [10, 8, 6, 4, 0], [20, 16, 12, 8, 4], "매우 보수적")
        after = pipeline.latest_analysis(conn)
        assert after["allocation"] == before["allocation"]            # 과거 기록은 그대로
        assert after["allocation_label"] == "v1 초기안"
    pipeline.run_once(FakeProvider(GOOD), macro_ok, fake_rss)          # 새 분석은 v2
    with db.session() as conn:
        new = pipeline.latest_analysis(conn)
        ids = [r[0] for r in conn.execute("SELECT allocation_set_id FROM analyses ORDER BY id")]
    assert ids == [1, 2] and new["allocation_label"].startswith("v2")
    assert new["allocation"]["equity_max"] <= 20  # 새 규칙 적용


def client():
    return TestClient(main.app)


def test_settings_page_shows_allocation_form():
    with client() as c:
        r = c.get("/settings")
        assert r.status_code == 200 and 'id="allocation"' in r.text
        for i in range(4):
            assert f'name="floor_{i}"' in r.text
        for i in range(5):
            assert f'name="emin_{i}"' in r.text and f'name="emax_{i}"' in r.text
        assert "70%" in r.text and "v1 초기안" in r.text


def test_allocation_save_creates_version_without_touching_weights():
    with client() as c:
        r = c.post("/settings/allocation", data=good_form(floor_0="80", note="실험"), follow_redirects=True)
        assert r.status_code == 200 and "비중 규칙이 저장되었습니다" in r.text and "v2 실험" in r.text
        with db.session() as conn:
            assert A.get_active(conn)["bands"][0][0] == 80
            assert conn.execute("SELECT COUNT(*) FROM weight_sets").fetchone()[0] == 1  # 가중치 버전은 그대로


@pytest.mark.parametrize("bad", [
    {"floor_0": "50"}, {"emax_0": "90"}, {"emin_4": "abc"}, {"emax_4": "40"}, {"floor_1": "60.5"},
])
def test_allocation_rejects_invalid_without_new_version(bad):
    with client() as c:
        r = c.post("/settings/allocation", data=good_form(**bad))
        assert r.status_code == 400 and "저장 실패" in r.text
        with db.session() as conn:
            assert len(A.list_all(conn)) == 1


def test_allocation_missing_field_and_xss():
    with client() as c:
        data = good_form()
        del data["emax_2"]
        assert c.post("/settings/allocation", data=data).status_code == 400
        r = c.post("/settings/allocation", data=good_form(note="<img src=x onerror=alert(1)>"),
                   follow_redirects=True)
        assert "<img src=x" not in r.text and "&lt;img" in r.text


def test_dashboard_shows_allocation_rule_version():
    db.init_db()
    pipeline.run_once(FakeProvider(GOOD), macro_ok, fake_rss)
    with client() as c:
        r = c.get("/")
        assert "구간 (v1 초기안)" in r.text and "권장 주식 비중" in r.text
