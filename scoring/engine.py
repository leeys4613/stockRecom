"""규칙 기반 매수 가능성 점수(0~100)와 점수 구간별 비중 규칙. 확률이 아니라 점수다."""
import config


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def indicator_score(key, change_pct, saturation=None):
    """변동률(%) -> -1..+1 (부호는 점수에 미치는 방향 반영, 포화 임계값에서 ±1)."""
    sat = (saturation or config.SCORE_SATURATION_PCT)[key]
    sign = config.SCORE_DIRECTION[key]
    return sign * _clamp(change_pct / sat)


def compute_score(changes, weights=None, saturation=None):
    """changes: {key: 변동률% 또는 None}. 결측 지표는 제외하고 가중치를 재정규화한다.

    반환: dict(score, missing, contributions, coverage)
      score: 0~100 (유효 지표가 없으면 None)
      coverage: 사용된 가중치 비율(0~1)
    """
    weights = weights or config.SCORE_WEIGHTS
    total = sum(abs(w) for w in weights.values())
    used, missing, contributions = 0.0, [], {}
    acc = 0.0
    for key, w in weights.items():
        ch = changes.get(key)
        if ch is None:
            missing.append(key)
            continue
        s = indicator_score(key, ch, saturation)
        used += abs(w)
        acc += abs(w) * s
        contributions[key] = abs(w) * s
    if used == 0:
        return dict(score=None, missing=missing, contributions={}, coverage=0.0)
    score = 50 + 50 * (acc / used)
    return dict(score=round(_clamp(score, 0, 100), 1), missing=missing,
                contributions=contributions, coverage=used / total)


def allocation(score, bands=None):
    """점수 -> (라벨, 주식 최소%, 주식 최대%, 현금 최소%, 현금 최대%). 합계 100 보정.

    bands: [(점수 하한, 라벨, 주식 최소, 주식 최대), ...] 하한 내림차순. 없으면 config 기본값.
    주식 상한(EQUITY_CAP_PCT)은 어떤 규칙에서도 넘지 않는 안전 장치다.
    """
    if score is None:
        raise ValueError("score is None")
    score = _clamp(score, 0, 100)
    for floor, label, lo, hi in (bands or config.ALLOCATION_BANDS):
        if score >= floor:
            break
    hi = min(hi, config.EQUITY_CAP_PCT)
    lo = min(lo, hi)
    return dict(label=label, equity_min=lo, equity_max=hi,
                cash_min=100 - hi, cash_max=100 - lo)


def evaluate(changes):
    """점수 + 비중을 한 번에. 점수 산출 불가 시 allocation 은 None."""
    res = compute_score(changes)
    res["allocation"] = allocation(res["score"]) if res["score"] is not None else None
    return res
