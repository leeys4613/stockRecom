"""공포·환희 보조 신호 (역발상 관점). 매수 가능성 점수와 별개로 표시하며 점수에 섞지 않는다.

점수는 추세·위험선호를 따라가므로(VIX↑·낙폭↑ → 점수↓), "공포에 사고 환희에 판다"는 관점과 정반대로 움직일 수 있다.
그래서 같은 입력(VIX, KOSPI 10거래일 변화·고점 대비)을 반대 방향으로 읽는 규칙 기반 신호를 따로 둔다.
임계값은 경험적 어림값이며 아직 검증되지 않았다 (결과는 analyses.fear_greed 에 쌓아 사후 검증한다).
"""

def _tier(v, rules, below):
    """rules: [(임계값, 점수)] 강한 순. 조건을 만족하는 첫 점수."""
    for thr, pts in rules:
        if (v <= thr) if below else (v >= thr):
            return pts
    return 0


def assess(vix=None, vix_avg=None, kospi_10d=None, kospi_dd=None):
    """값이 없는 입력은 건너뛴다. 반환: level/label/net/factors/available. 판단 가능한 입력이 없으면 None."""
    factors = []   # (이름, 설명, 부호있는 점수: 공포 +, 환희 -)

    def add(name, text, pts):
        if pts:
            factors.append(dict(name=name, text=text, points=pts))

    avail = 0
    if vix is not None:
        avail += 1
        f = _tier(vix, [(30, 2), (25, 1)], below=False)
        g = _tier(vix, [(13, 2), (15, 1)], below=True)
        add("VIX", f"VIX {vix:.1f} ({'매우 높음' if f == 2 else '높음'})" if f else f"VIX {vix:.1f} ({'매우 낮음' if g == 2 else '낮음'})", f - g)
    if vix_avg is not None:
        avail += 1
        f = 1 if vix_avg >= 25 else 0
        g = 1 if vix_avg <= 14 else 0
        add("VIX 10일 평균", f"VIX 10일 평균 {vix_avg:.1f} ({'높음' if f else '낮음'})", f - g)
    if kospi_10d is not None:
        avail += 1
        f = _tier(kospi_10d, [(-8, 2), (-4, 1)], below=True)
        g = _tier(kospi_10d, [(8, 2), (4, 1)], below=False)
        add("KOSPI 10거래일", f"KOSPI 10거래일 {kospi_10d:+.1f}%", f - g)
    if kospi_dd is not None:
        avail += 1
        f = _tier(kospi_dd, [(-8, 2), (-4, 1)], below=True)
        add("KOSPI 고점 대비", f"KOSPI 10일 고점 대비 {kospi_dd:+.1f}%", f)
    if avail < 2:
        return None
    net = sum(f["points"] for f in factors)
    if net >= 5:
        level, label = "extreme_fear", "공포 과열"
    elif net >= 3:
        level, label = "fear", "공포"
    elif net <= -5:
        level, label = "extreme_greed", "환희 과열"
    elif net <= -3:
        level, label = "greed", "낙관"
    else:
        level, label = "neutral", "보통"
    return dict(level=level, label=label, net=net, factors=factors, available=avail)


def divergence(signal, score):
    """추세 점수와 공포·환희 신호가 엇갈릴 때의 안내 문구 (없으면 None). 매수·매도 권유가 아니다."""
    if not signal or score is None:
        return None
    if score < 45 and signal["net"] >= 3:
        return ("추세 점수는 낮지만 시장은 공포 구간입니다 — 두 신호가 엇갈립니다. 역발상 관점에서는 반등을 노려볼 만한 구간으로 보기도 하지만, "
                "추세가 무너지는 중이면 추가 하락도 흔합니다.")
    if score >= 55 and signal["net"] <= -3:
        return ("점수는 높지만 시장은 낙관이 과열된 구간입니다 — 두 신호가 엇갈립니다. 역발상 관점에서는 과열을 경계하는 구간입니다.")
    return None
