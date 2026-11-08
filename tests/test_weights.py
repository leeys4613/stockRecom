import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import config
import db
import main
import performance as perf
import pipeline
import weights as W
from scoring import engine
from tests.conftest import GOOD, FakeProvider, fake_rss, macro_ok

KEYS = list(config.MACRO_INDICATORS)


def form(imp=None, sat=None, note=""):
    d = {f"imp_{k}": (imp or {}).get(k, 10) for k in KEYS}
    d.update({f"sat_{k}": (sat or {}).get(k, config.SCORE_SATURATION_PCT[k]) for k in KEYS})
    d["note"] = note
    return d


def vals(d, prefix):
    return {k: d[f"{prefix}_{k}"] for k in KEYS}


def test_validate_normalizes_and_keeps_direction():
    w, sat = W.validate({k: 10 for k in KEYS}, {k: 2 for k in KEYS})
    assert sum(abs(v) for v in w.values()) == pytest.approx(1.0, abs=1e-4)
    for k in KEYS:  # 방향은 config 로 고정
        assert (w[k] > 0) == (config.SCORE_DIRECTION[k] > 0)
    assert all(v == 2 for v in sat.values())


def test_validate_relative_importance():
    imp = {k: 0 for k in KEYS}
    imp["SOX"], imp["USDKRW"] = 75, 25
    w, _ = W.validate(imp, config.SCORE_SATURATION_PCT)
    assert w["SOX"] == pytest.approx(0.75) and w["USDKRW"] == pytest.approx(-0.25)
    assert w["WTI"] == 0


@pytest.mark.parametrize("mutate", [
    lambda i, s: i.pop("SOX"),                       # 지표 누락
    lambda i, s: i.update(SOX="abc"),                # 숫자 아님
    lambda i, s: i.update(SOX="nan"),                # NaN
    lambda i, s: i.update(SOX="inf"),
    lambda i, s: i.update(SOX=-5),                   # 음수(방향 뒤집기 시도)
    lambda i, s: i.update(SOX=101),
    lambda i, s: i.update({k: 0 for k in KEYS}),     # 전부 0
    lambda i, s: s.update(SOX=0),                    # 포화 0 (0 나눗셈 방지)
    lambda i, s: s.update(SOX=0.01),
    lambda i, s: s.update(SOX=21),
    lambda i, s: i.update(EXTRA=1),                  # 알 수 없는 지표
])
def test_validate_rejects_bad_input(mutate):
    imp = {k: 10 for k in KEYS}
    sat = {k: 2 for k in KEYS}
    mutate(imp, sat)
    with pytest.raises(W.WeightError):
        W.validate(imp, sat)


def test_seed_v1_and_versioning():
    db.init_db()
    with db.session() as conn:
        v1 = W.get_active(conn)
        assert v1["id"] == 1 and v1["weights"] == config.SCORE_WEIGHTS
        nid = W.create_version(conn, {k: 10 for k in KEYS}, {k: 2 for k in KEYS}, "환율 실험")
        assert nid == 2 and W.get_active(conn)["label"] == "v2 환율 실험"
        assert W.get(conn, 1)["weights"] == config.SCORE_WEIGHTS  # 이전 버전은 불변
        assert [w["id"] for w in W.list_all(conn)] == [2, 1]
        with pytest.raises(W.WeightError):
            W.create_version(conn, {k: 0 for k in KEYS}, {k: 2 for k in KEYS})
        assert len(W.list_all(conn)) == 2  # 실패한 저장은 버전을 만들지 않는다


def test_migration_of_existing_db(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE analyses (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            llm_status TEXT NOT NULL, provider TEXT, base_score REAL, adjustment INTEGER NOT NULL DEFAULT 0,
            final_score REAL, coverage REAL, missing TEXT, summary TEXT, risk_comment TEXT, error TEXT, inputs TEXT);
        INSERT INTO analyses (created_at,llm_status,base_score,final_score,adjustment)
            VALUES ('2026-11-08T00:00:00+00:00','ok',54.0,52.0,-2);
    """)
    old.commit()
    old.close()
    db.init_db(path)
    db.init_db(path)  # 멱등
    with db.session(path) as conn:
        assert conn.execute("SELECT weight_set_id FROM analyses").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM weight_sets").fetchone()[0] == 1
        assert conn.execute("SELECT final_score FROM analyses").fetchone()[0] == 52.0  # 기존 데이터 보존


def test_engine_custom_saturation_and_weights():
    changes = {k: 1.0 for k in KEYS}
    # SOX(우호 방향) +1%: 기본 포화 3% 면 1/3, 포화를 0.1% 로 낮추면 최대(1.0)
    assert engine.indicator_score("SOX", 1.0) == pytest.approx(1 / 3)
    assert engine.indicator_score("SOX", 1.0, {"SOX": 0.1}) == 1.0
    assert engine.indicator_score("USDKRW", 1.0, {"USDKRW": 0.1}) == -1.0  # 비우호 방향은 부호 유지
    only_sox = {k: 0.0 for k in KEYS}
    only_sox["SOX"] = 1.0
    assert engine.compute_score({**changes, "SOX": 3.0}, only_sox)["score"] == 100.0


def test_pipeline_uses_active_version_and_records_it():
    db.init_db()
    pipeline.run_once(FakeProvider(GOOD), macro_ok, fake_rss)  # v1
    with db.session() as conn:
        imp = {k: 0 for k in KEYS}
        imp["SOX"] = 100
        W.create_version(conn, imp, {k: 3 for k in KEYS}, "SOX만")
    pipeline.run_once(FakeProvider({**GOOD, "adjustment": 0}), macro_ok, fake_rss)  # v2
    with db.session() as conn:
        rows = conn.execute("SELECT weight_set_id, base_score FROM analyses ORDER BY id").fetchall()
        latest = pipeline.latest_analysis(conn)
    assert [r["weight_set_id"] for r in rows] == [1, 2]
    assert rows[1]["base_score"] == 100.0  # SOX +10% (포화 3%) 만 반영
    assert rows[0]["base_score"] != rows[1]["base_score"]
    assert latest["weight_label"] == "v2 SOX만"


def test_summarize_filters_by_weight_version():
    db.init_db()
    with db.session() as conn:
        for wid, score, ret in ((1, 70, 1.0), (1, 60, 0.5), (2, 30, -1.0)):
            aid = conn.execute(
                "INSERT INTO analyses (created_at,llm_status,final_score,base_score,coverage,adjustment,"
                "weight_set_id) VALUES ('2026-11-01T00:00:00+00:00','ok',?,?,1,0,?)",
                (score, score, wid)).lastrowid
            conn.execute("INSERT INTO outcomes (analysis_id,ref_date,ref_close,d1_ret,updated_at)"
                         " VALUES (?,?,100,?, 'x')", (aid, f"2026-11-0{aid}", ret))
        W.create_version(conn, {k: 10 for k in KEYS}, {k: 2 for k in KEYS})
        assert perf.summarize(conn, "d1")["n"] == 3
        assert perf.summarize(conn, "d1", 1)["n"] == 2
        assert perf.summarize(conn, "d1", 2)["n"] == 1


def client():
    return TestClient(main.app)


def test_settings_page_renders_all_indicators():
    with client() as c:
        r = c.get("/settings")
        assert r.status_code == 200
        for k, (name, _) in config.MACRO_INDICATORS.items():
            assert f'name="imp_{k}"' in r.text and f'name="sat_{k}"' in r.text and name in r.text
        assert "v1 초기안" in r.text and "과적합" in r.text


def test_settings_save_creates_new_version_and_shows_it():
    with client() as c:
        r = c.post("/settings", data=form(imp={"SOX": 50}, note="실험1"), follow_redirects=True)
        assert r.status_code == 200 and "저장되었습니다" in r.text
        assert "v2 실험1" in r.text and "v1 초기안" in r.text
        with db.session() as conn:
            assert W.get_active(conn)["id"] == 2


@pytest.mark.parametrize("bad", [
    {"imp_SOX": "-5"}, {"imp_SOX": "abc"}, {"sat_SOX": "0"}, {"sat_USDKRW": "999"},
])
def test_settings_rejects_invalid_without_creating_version(bad):
    with client() as c:
        data = {**form(), **bad}
        r = c.post("/settings", data=data)
        assert r.status_code == 400 and "저장 실패" in r.text
        with db.session() as conn:
            assert len(W.list_all(conn)) == 1


def test_settings_missing_field_rejected():
    with client() as c:
        data = form()
        del data["imp_SOX"]
        assert c.post("/settings", data=data).status_code == 400


def test_settings_note_is_escaped():
    with client() as c:
        r = c.post("/settings", data=form(note="<script>alert(1)</script>"), follow_redirects=True)
        assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;" in r.text


def test_performance_page_has_version_filter():
    with client() as c:
        c.post("/settings", data=form())
        r = c.get("/performance")
        assert r.status_code == 200 and 'id="version-filter"' in r.text and "v2" in r.text
        r = c.get("/performance?v=2")
        assert r.status_code == 200 and "버전 id 2만" in r.text
        assert c.get("/performance?v=abc").status_code == 422  # 잘못된 쿼리는 거부
