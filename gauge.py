"""반원 게이지(SVG) 기하 계산. 구간 경계는 그 분석의 비중 규칙(bands)에서 가져온다.

점수 0 은 왼쪽 끝(180°), 100 은 오른쪽 끝(0°). 구간 색은 위쪽(우호)부터 초록 → 빨강.
"""
import math

CX, CY = 100.0, 100.0
R_OUT, R_IN = 88.0, 60.0
# bands 는 점수 하한 내림차순(우호 → 비우호) 5개. 같은 순서의 색
BAND_COLORS = ["#2a9d4a", "#8fb83b", "#d9a000", "#e8742a", "#d33a3a"]
NEUTRAL = "#8a8f98"


def _angle(score):
    return 180.0 - 1.8 * max(0.0, min(100.0, score))


def _pt(angle_deg, r):
    a = math.radians(angle_deg)
    return CX + r * math.cos(a), CY - r * math.sin(a)


def _f(p):
    return f"{p[0]:.2f},{p[1]:.2f}"


def _segment(s0, s1):
    """점수 구간 [s0, s1] 의 도넛 조각 경로."""
    a0, a1 = _angle(s0), _angle(s1)
    return (f"M {_f(_pt(a0, R_OUT))} A {R_OUT} {R_OUT} 0 0 1 {_f(_pt(a1, R_OUT))} "
            f"L {_f(_pt(a1, R_IN))} A {R_IN} {R_IN} 0 0 0 {_f(_pt(a0, R_IN))} Z")


def band_index(score, bands):
    for i, b in enumerate(bands):
        if score >= b[0]:
            return i
    return len(bands) - 1


def build(score, bands, base_score=None):
    """템플릿용 dict. score 가 None 이면 바늘 없이 회색 게이지."""
    colors = BAND_COLORS if len(bands) == len(BAND_COLORS) else [NEUTRAL] * len(bands)
    segments = []
    upper = 100.0
    for i, b in enumerate(bands):
        floor = float(b[0])
        segments.append(dict(path=_segment(floor, upper), color=colors[i], label=b[1],
                             floor=b[0], upper=upper))
        upper = floor
    out = dict(segments=segments, needle=None, base_tick=None, color=NEUTRAL, label=None,
               band_index=None, cx=CX, cy=CY)
    if score is not None:
        i = band_index(score, bands)
        out.update(color=colors[i], label=bands[i][1], band_index=i)
        a = _angle(score)
        x, y = _pt(a, R_IN - 6)
        out["needle"] = dict(x=round(x, 2), y=round(y, 2), angle=round(a, 2))
    if base_score is not None and score is not None and abs(base_score - score) >= 0.5:
        a = _angle(base_score)
        x0, y0 = _pt(a, R_OUT + 2)
        x1, y1 = _pt(a, R_OUT + 9)
        out["base_tick"] = dict(x0=round(x0, 2), y0=round(y0, 2), x1=round(x1, 2), y1=round(y1, 2))
    return out
