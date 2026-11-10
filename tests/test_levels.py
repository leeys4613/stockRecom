import math

import pytest

import config
import db
from scoring import levels as L


def bars(closes, spread=0.01, vol=1000):
    return [dict(high=c * (1 + spread), low=c * (1 - spread), close=c, volume=vol) for c in closes]


def wave(n=140, lo=90.0, hi=110.0, period=20):
    """lo~hi 를 오가는 박스권 (고점 110 부근 / 저점 90 부근에서 반복 반응)."""
    mid, amp = (lo + hi) / 2, (hi - lo) / 2
    return [mid + amp * math.sin(2 * math.pi * i / period) for i in range(n)]


def test_box_range_finds_support_below_and_resistance_above():
    closes = wave()
    closes[-1] = 100.0     # 박스 중간
    r = L.compute(bars(closes))
    assert r["supports"] and r["resistances"]
    s, res = r["supports"][0], r["resistances"][0]
    assert 85 <= s["price"] < 100 < res["price"] <= 115
    assert s["dist_pct"] < 0 < res["dist_pct"]
    assert s["touches"] >= 3 and s["strength"] in ("중", "강")        # 여러 번 반응한 가격대
    assert len(r["supports"]) <= 2 and len(r["resistances"]) <= 2


def test_levels_sorted_nearest_first_and_on_correct_side():
    r = L.compute(bars(wave()))
    price = r["price"]
    assert all(x["price"] < price for x in r["supports"]) and all(x["price"] > price for x in r["resistances"])
    assert [x["dist_pct"] for x in r["supports"]] == sorted((x["dist_pct"] for x in r["supports"]), reverse=True)
    assert [x["dist_pct"] for x in r["resistances"]] == sorted(x["dist_pct"] for x in r["resistances"])


def test_volume_node_marked_where_volume_concentrates():
    closes = wave()
    rows = bars(closes)
    for rr in rows:               # 저점(약 90) 부근에서만 거래량 폭증
        if rr["close"] < 93:
            rr["volume"] = 10000
    rows[-1] = dict(high=101, low=99, close=100.0, volume=1000)
    r = L.compute(rows)
    assert any(x["vol_node"] for x in r["supports"])


def test_new_high_has_no_resistance_and_flags_it():
    closes = [100 + i * 0.5 for i in range(120)]      # 꾸준한 상승 → 신고가
    r = L.compute(bars(closes))
    assert r["at_high"] and r["resistances"] == []
    assert r["supports"] == [] or r["supports"][0]["price"] < r["price"]


def test_new_low_has_no_support_and_flags_it():
    closes = [200 - i * 0.5 for i in range(120)]
    r = L.compute(bars(closes))
    assert r["at_low"] and r["supports"] == []


def test_volume_ratio_and_insufficient_data():
    rows = bars(wave())
    for rr in rows[-5:]:
        rr["volume"] = 3000
    assert L.compute(rows)["vol_ratio"] == pytest.approx(3.0, rel=0.05)
    with pytest.raises(ValueError):
        L.compute(bars(wave(n=config.LEVELS_MIN_BARS - 1)))


def test_price_rounding_for_krw_and_small_prices():
    assert L.compute(bars([x * 1000 for x in wave()]))["price"] == round(L.compute(bars([x * 1000 for x in wave()]))["price"])
    small = L.compute(bars([x / 100 for x in wave()]))
    assert isinstance(small["price"], float)


def test_pipeline_stores_levels_in_view_metrics_and_page_shows_them():
    from fastapi.testclient import TestClient

    import main
    import stockviews
    from tests import test_stock_views as sv

    sv.seed_watchlist("005930")
    sv.seed_market()
    closes = wave()
    closes[-1] = 100.0
    p = sv.RoutingProvider(sv.payload(sv.raw("005930")))
    assert sv.run_views(p, ohlcv_fetcher=lambda sym: bars(closes)) == 1
    with db.session() as conn:
        _, v = stockviews.latest(conn, "005930")
    lv = v["metrics"]["levels"]
    assert lv["supports"] and lv["resistances"]
    ctx = p.prompts[0]
    assert "supports" not in ctx and "resistances" not in ctx          # 지지·저항은 AI 판정 입력/출력에 섞이지 않는다
    with TestClient(main.app) as c:
        html = c.get("/watchlist").text
    assert 'class="levels"' in html and "지지 ▼" in html and "저항 ▲" in html and "투자 권유가 아닙니다" in html
    assert "매도" not in html


def test_levels_failure_does_not_block_view_and_is_recorded():
    import stockviews
    from tests import test_stock_views as sv

    sv.seed_watchlist("005930")
    sv.seed_market()
    p = sv.RoutingProvider(sv.payload(sv.raw("005930")))
    assert sv.run_views(p) == 1                                         # 기본 ohlcv 조회는 테스트에서 차단됨
    with db.session() as conn:
        _, v = stockviews.latest(conn, "005930")
    assert v["status"] == "ok" and v["metrics"]["levels"] is None
    assert any("일봉/지지저항" in e for e in v["metrics"]["errors"])
