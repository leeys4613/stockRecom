"""차트·거래량 기반 지지선/저항선 (규칙 기반 계산, LLM 이 숫자를 만들지 않는다).

방법:
  1) 스윙 고점/저점: 앞뒤 PIVOT_K 거래일보다 높은(낮은) 날의 고가(저가). 고점이 나중에 지지로, 저점이 저항으로도 작동하므로 합쳐서 본다.
  2) 가까운 가격(CLUSTER_TOL 이내)은 하나의 가격대로 묶고, 묶인 횟수(터치 수)를 센다.
  3) 거래량 집중: 그 가격대 ±CLUSTER_TOL 에서 체결된 거래량이 기간 전체의 VOL_NODE_SHARE 이상이면 "거래량 집중".
  4) 현재가 아래 가장 가까운 2개를 지지, 위 가장 가까운 2개를 저항으로 낸다. 강도: 터치 수 + 거래량 집중(+1).
과거에 반응했던 가격대를 기계적으로 찾은 것이며 앞으로도 작동한다는 보장은 없다.
"""
import config


def _round_price(p):
    return round(p) if p >= 1000 else round(p, 2)


def compute(rows, k=None, tol=None, max_dist=0.25):
    """rows: [{'high','low','close','volume'}] 오름차순. 데이터가 부족하면 ValueError."""
    k = k or config.LEVELS_PIVOT_K
    tol = tol or config.LEVELS_CLUSTER_TOL
    n = len(rows)
    if n < config.LEVELS_MIN_BARS:
        raise ValueError(f"일봉 {n}개뿐 ({config.LEVELS_MIN_BARS}개 필요)")
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    vols = [r["volume"] or 0 for r in rows]
    price = rows[-1]["close"]
    total_vol = sum(vols)

    pts = []   # (가격, 인덱스)
    for i in range(k, n - k):
        if highs[i] == max(highs[i - k:i + k + 1]):
            pts.append((highs[i], i))
        if lows[i] == min(lows[i - k:i + k + 1]):
            pts.append((lows[i], i))
    for p_, i_ in ((max(highs), highs.index(max(highs))), (min(lows), lows.index(min(lows)))):
        if i_ < n - 1:   # 기간 고점/저점도 후보 (오늘 막 찍은 극값은 아직 반응 이력이 없어 제외)
            pts.append((p_, i_))
    pts.sort()

    clusters = []
    for p, i in pts:
        if clusters and p <= clusters[-1]["sum"] / clusters[-1]["touches"] * (1 + tol):
            c = clusters[-1]
            c["sum"] += p
            c["touches"] += 1
            c["last"] = max(c["last"], i)
        else:
            clusters.append(dict(sum=p, touches=1, last=i))

    typical = [(r["high"] + r["low"] + r["close"]) / 3 for r in rows]
    out = []
    for c in clusters:
        lvl = c["sum"] / c["touches"]
        vol_in = sum(v for t, v in zip(typical, vols) if lvl * (1 - tol) <= t <= lvl * (1 + tol))
        share = vol_in / total_vol if total_vol else 0.0
        node = share >= config.LEVELS_VOL_NODE_SHARE
        score = c["touches"] + (1 if node else 0)
        out.append(dict(price=_round_price(lvl), dist_pct=round((lvl / price - 1) * 100, 1), touches=c["touches"],
                        vol_node=node, vol_share=round(share * 100), last_days_ago=n - 1 - c["last"],
                        strength="강" if score >= 4 else "중" if score >= 2 else "약"))

    sup = [x for x in out if x["dist_pct"] <= -0.5 and x["dist_pct"] >= -max_dist * 100]
    res = [x for x in out if x["dist_pct"] >= 0.5 and x["dist_pct"] <= max_dist * 100]
    sup.sort(key=lambda x: -x["dist_pct"])    # 현재가에 가까운 순
    res.sort(key=lambda x: x["dist_pct"])
    recent5 = vols[-5:]
    prior = vols[-25:-5]
    vol_ratio = (sum(recent5) / len(recent5)) / (sum(prior) / len(prior)) if prior and sum(prior) else None
    return dict(price=_round_price(price), bars=n, supports=sup[:2], resistances=res[:2],
                vol_ratio=None if vol_ratio is None else round(vol_ratio, 2),
                at_high=price >= max(r["close"] for r in rows) * 0.99, at_low=price <= min(r["close"] for r in rows) * 1.01)
