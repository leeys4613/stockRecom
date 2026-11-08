"""정책 반영: AI 가 -3~+3 단계로 판정한 미국 정책/한국 증시 부양정책을 점수(±상한)로 환산한다.

AI 에게 점수를 직접 쓰게 하지 않는다(단계만 판정). 환산은 코드가 하고, 근거 헤드라인이 실제 입력에 없으면
그 판정은 0 으로 처리한다 (근거 없는 주장 차단). 점수 반영폭은 가중치 세트의 상한(±점)으로 제한된다.
"""
import config

REGIONS = ("us", "kr")
REGION_NAME = {"us": "미국 정책·정치", "kr": "한국 증시 부양·자본시장 정책"}
LEVEL_TEXT = {-3: "매우 부정", -2: "부정", -1: "약한 부정", 0: "영향 없음", 1: "약한 긍정", 2: "긍정", 3: "매우 긍정"}
MIN_EVIDENCE_CHARS = 10  # 이보다 짧은 근거 문자열은 우연히 일치할 수 있어 인정하지 않음


def _norm(s):
    return "".join(str(s or "").split()).casefold()


def supported_evidence(evidence, titles):
    """근거 문자열 중 실제 입력 헤드라인(의 일부 포함)과 일치하는 것만 남긴다."""
    ts = [_norm(t) for t in titles]
    out, seen = [], set()
    for e in evidence or []:
        en = _norm(e)
        if len(en) >= MIN_EVIDENCE_CHARS and any(en in t for t in ts) and en not in seen:
            seen.add(en)
            out.append(e)
    return out


def assess(analysis, headlines, caps):
    """analysis: llm.Analysis, headlines: {"us": [...], "kr": [...]}, caps: {"us": 상한, "kr": 상한}.

    반환: {region: dict(effect, level_text, points, cap, evidence, note)}
    """
    out = {}
    for r in REGIONS:
        titles = headlines.get(r) or []
        effect = int(getattr(analysis, f"{r}_policy_effect", 0) or 0)
        effect = max(-config.POLICY_LEVEL_MAX, min(config.POLICY_LEVEL_MAX, effect))
        evidence = supported_evidence(getattr(analysis, f"{r}_policy_evidence", []), titles)
        note = None
        if effect != 0 and not titles:
            effect, note = 0, "최근 관련 기사가 없어 0 처리"
        elif effect != 0 and not evidence:
            effect, note = 0, "근거 헤드라인을 확인하지 못해 0 처리"
        cap = float(caps.get(r, 0) or 0)
        out[r] = dict(effect=effect, level_text=LEVEL_TEXT[effect],
                      points=round(effect / config.POLICY_LEVEL_MAX * cap, 1), cap=cap,
                      evidence=evidence if effect != 0 else [], note=note, n_headlines=len(titles))
    return out


def total(assessment):
    return round(sum(a["points"] for a in assessment.values()), 1) if assessment else 0.0


def empty(caps=None):
    """AI 분석을 못 했을 때(폴백): 정책 반영 0."""
    caps = caps or {}
    return {r: dict(effect=0, level_text=LEVEL_TEXT[0], points=0.0, cap=float(caps.get(r, 0) or 0),
                    evidence=[], note="AI 분석 없음", n_headlines=0) for r in REGIONS}
