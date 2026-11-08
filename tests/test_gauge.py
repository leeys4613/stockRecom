import math

import pytest
from fastapi.testclient import TestClient

import allocations as A
import config
import db
import gauge
import main
import pipeline
from tests.conftest import GOOD, FakeProvider, fake_rss, macro_ok

BANDS = [list(b) for b in config.ALLOCATION_BANDS]


def test_angle_mapping():
    assert gauge._angle(0) == 180 and gauge._angle(50) == 90 and gauge._angle(100) == 0
    assert gauge._angle(-5) == 180 and gauge._angle(120) == 0  # 범위 밖은 끝으로 고정


def test_needle_points_to_correct_side():
    cx, cy = gauge.CX, gauge.CY
    left = gauge.build(0, BANDS)["needle"]
    mid = gauge.build(50, BANDS)["needle"]
    right = gauge.build(100, BANDS)["needle"]
    assert left["x"] < cx and left["y"] == pytest.approx(cy, abs=0.01)    # 왼쪽 끝, 수평
    assert mid["x"] == pytest.approx(cx, abs=0.01) and mid["y"] < cy      # 정중앙 위
    assert right["x"] > cx and right["y"] == pytest.approx(cy, abs=0.01)  # 오른쪽 끝
    r = math.hypot(mid["x"] - cx, mid["y"] - cy)
    assert r == pytest.approx(gauge.R_IN - 6)


def test_segments_cover_0_to_100_contiguously_with_default_bands():
    g = gauge.build(50, BANDS)
    segs = g["segments"]
    assert len(segs) == 5 and [s["label"] for s in segs] == [b[1] for b in BANDS]
    assert segs[0]["upper"] == 100 and segs[-1]["floor"] == 0
    for hi, lo in zip(segs, segs[1:]):
        assert hi["floor"] == lo["upper"]  # 빈틈/겹침 없음
    assert all(s["path"].startswith("M ") and s["path"].endswith("Z") for s in segs)
    assert len({s["color"] for s in segs}) == 5


def test_segments_follow_custom_bands():
    bands = A.validate([80, 60, 50, 35], [40, 30, 20, 10, 0], [60, 50, 40, 25, 10])
    g = gauge.build(55, bands)
    assert [s["floor"] for s in g["segments"]] == [80, 60, 50, 35, 0]
    assert g["label"] == "중립" and g["band_index"] == 2  # 55 는 50~60 구간


@pytest.mark.parametrize("score,idx", [(100, 0), (70, 0), (69.9, 1), (55, 1), (45, 2), (30, 3), (29.9, 4), (0, 4)])
def test_band_index_boundaries(score, idx):
    assert gauge.band_index(score, BANDS) == idx
    assert gauge.build(score, BANDS)["band_index"] == idx


def test_no_score_gives_grey_gauge_without_needle():
    g = gauge.build(None, BANDS)
    assert g["needle"] is None and g["band_index"] is None and g["color"] == gauge.NEUTRAL
    assert len(g["segments"]) == 5


def test_base_tick_only_when_adjusted():
    assert gauge.build(52, BANDS, base_score=52.0)["base_tick"] is None
    assert gauge.build(52, BANDS, base_score=52.3)["base_tick"] is None   # 0.5 미만 차이는 생략
    t = gauge.build(50, BANDS, base_score=58.0)["base_tick"]
    assert t is not None and math.hypot(t["x0"] - gauge.CX, t["y0"] - gauge.CY) == pytest.approx(gauge.R_OUT + 2, abs=0.05)  # 좌표를 소수 2자리로 반올림


def test_dashboard_renders_gauge_with_band_color_and_label():
    db.init_db()
    pipeline.run_once(FakeProvider(GOOD), macro_ok, fake_rss)
    with TestClient(main.app) as c:
        html = c.get("/").text
    assert 'id="gauge"' in html and 'id="needle"' in html
    assert html.count("<path") == 5
    assert "시장 우호도 점수" in html and "권장 주식 비중" in html
    assert 'aria-label="시장 우호도 점수' in html
    for color in gauge.BAND_COLORS:
        assert color in html
    assert "매수" not in html and "매도" not in html  # 투자 지시 문구를 쓰지 않는다


def test_dashboard_gauge_without_score():
    db.init_db()

    def fail(symbol):
        raise RuntimeError("down")
    pipeline.run_once(FakeProvider(GOOD), fail, fake_rss)
    with TestClient(main.app) as c:
        html = c.get("/").text
    assert 'id="gauge"' in html and 'id="needle"' not in html
    assert "최신 지표가 없습니다" in html
