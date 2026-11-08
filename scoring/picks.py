"""컨센서스 기반 Top Picks 선별 (순수 함수). 예측이 아니라 증권사 의견을 정리한 순위다.

점수 = 괴리율(목표가 평균/현재가-1) + 투자의견 평균 + 목표가 변화(추이)를 -1..+1 로 정규화해 가중 합산.
다음은 후보에서 제외한다: 조회 실패/오래된 데이터, 컨센서스 없음, 애널리스트 부족, 괴리 과대(데이터 이상 의심).
"""
from datetime import datetime, timezone

import config


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


FLAG_TEXT = {
    "failed": "최근 조회 실패",
    "stale": "오래된 데이터",
    "no_consensus": "컨센서스 없음",
    "few_analysts": "애널리스트 부족",
    "outlier": "괴리 과대(데이터 확인 필요)",
    "no_data": "데이터 없음",
}
EXCLUDING = {"stale", "no_consensus", "few_analysts", "outlier", "no_data"}  # failed 는 참고용


def opinion_text(rec_mean):
    if rec_mean is None:
        return None
    return ("강력 매수 의견 우세" if rec_mean <= 1.5 else "매수 의견 우세" if rec_mean <= 2.5 else
            "중립" if rec_mean <= 3.5 else "매도 의견 우세")


def evaluate(row, now=None):
    now = now or datetime.now(timezone.utc)
    out = dict(code=row["stock"]["code"], name=row["stock"]["name"], market=row["stock"]["market"],
               flags=[], upside=None, trend=None, trend_days=None, score=None, eligible=False,
               snap=row["ok"], last=row["last"], opinion=None)
    ok, last = row["ok"], row["last"]
    if ok is None:
        out["flags"] = ["failed", "no_data"] if last else ["no_data"]
        return out
    if last and last["status"] == "failed":
        out["flags"].append("failed")
    age = now - datetime.fromisoformat(ok["fetched_at"])
    out["age_days"] = age.total_seconds() / 86400
    if age.days >= config.PICK_STALE_DAYS:
        out["flags"].append("stale")
    out["opinion"] = opinion_text(ok["rec_mean"])
    if ok["target_mean"] is None or ok["price"] in (None, 0) or not ok["n_analysts"]:
        out["flags"].append("no_consensus")
        return out
    if ok["n_analysts"] < config.PICK_MIN_ANALYSTS:
        out["flags"].append("few_analysts")
    out["upside"] = ok["target_mean"] / ok["price"] - 1
    if out["upside"] > config.PICK_MAX_UPSIDE:
        out["flags"].append("outlier")
    base = row.get("base")
    if base and base["target_mean"]:
        out["trend"] = ok["target_mean"] / base["target_mean"] - 1
        out["trend_days"] = (datetime.fromisoformat(ok["fetched_at"])
                             - datetime.fromisoformat(base["fetched_at"])).total_seconds() / 86400

    comps = {"upside": _clamp(out["upside"] / config.PICK_UPSIDE_SAT)}
    if ok["rec_mean"] is not None:
        comps["opinion"] = _clamp((3 - ok["rec_mean"]) / 2)
    if out["trend"] is not None:
        comps["trend"] = _clamp(out["trend"] / config.PICK_TREND_SAT)
    wsum = sum(config.PICK_WEIGHTS[k] for k in comps)  # 없는 항목은 제외하고 가중치 재정규화
    out["score"] = round(50 + 50 * sum(config.PICK_WEIGHTS[k] * v for k, v in comps.items()) / wsum, 1)
    out["components"] = comps
    out["eligible"] = not (set(out["flags"]) & EXCLUDING)
    return out


def top_picks(rows, now=None, n=None):
    evaluated = [evaluate(r, now) for r in rows]
    picks = sorted((e for e in evaluated if e["eligible"] and e["upside"] > 0),
                   key=lambda e: e["score"], reverse=True)
    return picks[: (n or config.PICK_TOP_N)], evaluated
