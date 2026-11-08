import pytest

import config
from scoring import allocation, compute_score, evaluate, indicator_score


def test_weights_sum_to_one():
    assert sum(abs(w) for w in config.SCORE_WEIGHTS.values()) == pytest.approx(1.0)
    assert set(config.SCORE_WEIGHTS) == set(config.SCORE_SATURATION_PCT) == set(config.MACRO_INDICATORS)


def test_indicator_direction_and_saturation():
    assert indicator_score("SOX", 3.0) == 1
    assert indicator_score("SOX", 99) == 1
    assert indicator_score("USDKRW", 1.0) == -1  # 원화 약세는 비우호
    assert indicator_score("USDKRW", -0.5) == pytest.approx(0.5)
    assert indicator_score("SOX", 0) == 0


def test_neutral_when_no_change():
    assert compute_score({k: 0.0 for k in config.SCORE_WEIGHTS})["score"] == 50


def test_extremes():
    best = {k: (999 if w > 0 else -999) for k, w in config.SCORE_WEIGHTS.items()}
    worst = {k: -v for k, v in best.items()}
    assert compute_score(best)["score"] == 100
    assert compute_score(worst)["score"] == 0


def test_missing_reported_and_renormalized():
    changes = {k: None for k in config.SCORE_WEIGHTS}
    changes["SOX"] = 3.0
    r = compute_score(changes)
    assert r["score"] == 100  # 유효 지표 1개로 재정규화
    nonzero = [k for k, v in config.SCORE_WEIGHTS.items() if v != 0]   # 가중치 0 인 참고 지표는 결측으로 세지 않는다
    assert "SOX" not in r["missing"] and len(r["missing"]) == len(nonzero) - 1
    assert r["coverage"] == pytest.approx(abs(config.SCORE_WEIGHTS["SOX"]))


def test_all_missing():
    r = evaluate({})
    assert r["score"] is None and r["allocation"] is None and r["coverage"] == 0


@pytest.mark.parametrize("score,label", [
    (100, "높음"), (70, "높음"), (69.9, "다소 높음"), (55, "다소 높음"),
    (50, "중립"), (45, "중립"), (44.9, "다소 낮음"), (30, "다소 낮음"),
    (29.9, "낮음"), (0, "낮음"),
])
def test_allocation_bands(score, label):
    a = allocation(score)
    assert a["label"] == label
    assert a["equity_max"] <= config.EQUITY_CAP_PCT
    assert a["equity_max"] + a["cash_min"] == 100
    assert a["equity_min"] + a["cash_max"] == 100


def test_allocation_cap_enforced(monkeypatch):
    monkeypatch.setattr(config, "ALLOCATION_BANDS", [(0, "x", 80, 95)])
    a = allocation(50)
    assert a["equity_max"] == 70 and a["equity_min"] == 70 and a["cash_min"] == 30


def test_allocation_none_raises():
    with pytest.raises(ValueError):
        allocation(None)
