"""과거 유사 국면 분석과 공포·환희 신호 백테스트 (KOSPI 지수 기준, 순수 계산).

특징(앱의 공포·환희 신호와 같은 입력): VIX, VIX 10일 평균, KOSPI 10거래일 변화(%), KOSPI 10일 고점 대비(%).
시차: 한국 장 마감 시점에는 직전 미국 장 마감 VIX 만 알 수 있으므로, KOSPI 날짜보다 "이전" 날짜의 VIX 를 쓴다(미래 정보 차단).
수익률: 그 날 종가 진입 가정의 이후 5·20거래일 KOSPI 수익률(%). 비용·세금·개별 종목은 반영하지 않는다.
"""
import math
import statistics

import config
from scoring import sentiment

TREND = config.TREND_DAYS
LEVELS = [("extreme_fear", "공포 과열"), ("fear", "공포"), ("neutral", "보통"), ("greed", "낙관"), ("extreme_greed", "환희 과열")]
HORIZONS = (5, 20)


def build_rows(ks, vix, start=None):
    """ks, vix: [(date, close)] 오름차순 -> 날짜별 특징·신호·이후 수익률 목록."""
    start = start or config.ANALOG_START
    closes = [c for _, c in ks]
    vdates = [d for d, _ in vix]
    vcloses = [c for _, c in vix]
    rows, vp = [], -1          # vp: ks 날짜보다 이전인 마지막 VIX 인덱스
    for i, (d, c) in enumerate(ks):
        while vp + 1 < len(vdates) and vdates[vp + 1] < d:
            vp += 1
        if i < TREND or d < start or vp < TREND - 1:
            continue
        window = closes[i - TREND:i + 1]
        k10 = (c / window[0] - 1) * 100
        kdd = (c / max(window) - 1) * 100
        v = vcloses[vp]
        vavg = sum(vcloses[vp - TREND + 1:vp + 1]) / TREND
        row = dict(i=i, date=d, vix=v, vixavg=vavg, k10=k10, kdd=kdd, close=c)
        for h in HORIZONS:
            row[f"f{h}"] = (closes[i + h] / c - 1) * 100 if i + h < len(closes) else None
        sig = sentiment.assess(vix=v, vix_avg=vavg, kospi_10d=k10, kospi_dd=kdd)
        row["level"], row["net"] = (sig["level"], sig["net"]) if sig else (None, None)
        rows.append(row)
    return rows


def _pct(vals, q):
    s = sorted(vals)
    if not s:
        return None
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def dist(vals):
    """수익률 목록 -> 분포 요약. 표본이 없으면 n=0."""
    vals = [v for v in vals if v is not None]
    if not vals:
        return dict(n=0)
    return dict(n=len(vals), mean=statistics.fmean(vals), median=statistics.median(vals),
                up=sum(1 for v in vals if v > 0) / len(vals) * 100, p10=_pct(vals, 0.10), p90=_pct(vals, 0.90))


VERDICT_GAP = 7.0      # 오른 비율이 기준선보다 이만큼(%p) 이상 차이 나야 "좋았음/나빴음"
VERDICT_MIN_N = 30     # 표본이 이보다 적으면 판단하지 않고 "표본 적음"


def verdict(d, base):
    """등급의 이후 수익률 분포(d)를 기준선(base)과 견줘 쉬운 말로 요약한다.

    기준: '그 기간 뒤에 올라 있던 비율'을 평소(모든 날)와 비교. 표본이 적으면 판단 보류.
    """
    if not d.get("n"):
        return dict(label="표본 없음", cls="none", diff=None)
    diff = d["up"] - base["up"]
    if d["n"] < VERDICT_MIN_N:
        return dict(label="표본 적음 · 참고만", cls="none", diff=diff)
    if diff >= VERDICT_GAP:
        return dict(label="평소보다 좋았음", cls="fresh", diff=diff)
    if diff <= -VERDICT_GAP:
        return dict(label="평소보다 나빴음", cls="failed", diff=diff)
    return dict(label="평소와 비슷", cls="none", diff=diff)


def _episodes(rows, sep=None):
    """같은 신호가 sep 거래일 이내로 이어지면 한 에피소드. 각 에피소드의 첫 날 행을 돌려준다."""
    sep = sep or config.ANALOG_SEP_DAYS
    out, last_i = [], None
    for r in rows:
        if last_i is None or r["i"] - last_i > sep:
            out.append(r)
        last_i = r["i"]
    return out


def backtest(rows):
    """신호 등급별 이후 수익률. all=해당 등급인 모든 날(겹침 있음), ep=에피소드 첫 날(신호가 처음 켜진 날). 기준선=전체 날."""
    res = []
    for lvl, label in LEVELS:
        sel = [r for r in rows if r["level"] == lvl]
        ep = _episodes(sel)
        res.append(dict(level=lvl, label=label, days=len(sel), episodes=len(ep),
                        all={h: dist([r[f"f{h}"] for r in sel]) for h in HORIZONS},
                        ep={h: dist([r[f"f{h}"] for r in ep]) for h in HORIZONS}))
    base = {h: dist([r[f"f{h}"] for r in rows]) for h in HORIZONS}
    return res, base


def nearest(rows, current, k=None, sep=None):
    """현재 특징(vix, vixavg, k10, kdd)과 가장 가까운 과거 날들(서로 sep 거래일 이상 떨어진 k 개)."""
    k = k or config.ANALOG_K
    sep = sep or config.ANALOG_SEP_DAYS
    keys = ("vix", "vixavg", "k10", "kdd")
    sd = {x: (statistics.pstdev([r[x] for r in rows]) or 1.0) for x in keys}
    scored = sorted(rows, key=lambda r: sum(((r[x] - current[x]) / sd[x]) ** 2 for x in keys))
    chosen = []
    for r in scored:
        if all(abs(r["i"] - c["i"]) > sep for c in chosen):
            chosen.append(r)
            if len(chosen) >= k:
                break
    chosen = [dict(c, distance=math.sqrt(sum(((c[x] - current[x]) / sd[x]) ** 2 for x in keys))) for c in chosen]
    summary = {h: dist([c[f"f{h}"] for c in chosen]) for h in HORIZONS}
    return sorted(chosen, key=lambda c: c["date"], reverse=True), summary
