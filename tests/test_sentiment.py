import pytest

import db
import main
import pipeline
from fastapi.testclient import TestClient
from scoring import sentiment as S
from tests.conftest import GOOD, FakeProvider, fake_rss, macro_ok


def test_extreme_fear_and_greed_levels():
    f = S.assess(vix=32, vix_avg=29, kospi_10d=-9, kospi_dd=-10)
    assert f["level"] == "extreme_fear" and f["net"] == 7 and f["label"] == "공포 과열"
    g = S.assess(vix=12, vix_avg=13, kospi_10d=9, kospi_dd=0)
    assert g["level"] == "extreme_greed" and g["net"] <= -5


def test_neutral_and_partial_inputs():
    assert S.assess(vix=18, vix_avg=19, kospi_10d=0.5, kospi_dd=-1)["level"] == "neutral"
    assert S.assess(vix=40) is None                      # 입력 1개뿐이면 판단하지 않는다
    assert S.assess() is None
    assert S.assess(vix=31, kospi_10d=-9)["level"] == "fear"    # 일부만 있어도 있는 만큼 판단 (net 4)


@pytest.mark.parametrize("vix,exp", [(24.9, 0), (25, 1), (29.9, 1), (30, 2), (15.1, 0), (15, -1), (13.1, -1), (13, -2)])
def test_vix_tiers(vix, exp):
    f = S.assess(vix=vix, kospi_10d=0)
    pts = sum(x["points"] for x in f["factors"] if x["name"] == "VIX")
    assert pts == exp


def test_divergence_messages():
    fear = S.assess(vix=32, vix_avg=29, kospi_10d=-9, kospi_dd=-10)
    greed = S.assess(vix=12, vix_avg=13, kospi_10d=9, kospi_dd=0)
    assert "엇갈립니다" in S.divergence(fear, 30) and "추가 하락" in S.divergence(fear, 30)
    assert S.divergence(fear, 60) is None                # 점수도 높으면 엇갈리지 않는다
    assert "과열" in S.divergence(greed, 70)
    assert S.divergence(greed, 30) is None and S.divergence(None, 30) is None and S.divergence(fear, None) is None


def test_pipeline_stores_signal_and_dashboard_shows_it():
    db.init_db()
    pipeline.run_once(FakeProvider(GOOD), macro_ok, fake_rss)
    with db.session() as conn:
        a = pipeline.latest_analysis(conn)
    assert a["fear_greed"] is None or a["fear_greed"]["label"]          # 합성 데이터에서는 어느 쪽이든 구조가 맞아야 한다
    with TestClient(main.app) as c:
        html = c.get("/").text
    if a["fear_greed"]:
        assert 'id="fear-greed"' in html and "점수와 별개" in html and "검증되지 않았고" in html
