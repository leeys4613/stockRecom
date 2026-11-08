"""관심종목 AI 적합도: 대중심리 점수 + 주도섹터 점수 -> 추천 점수 (순수 함수).

AI 는 -3~+3 단계와 근거만 낸다. 점수 환산·가중 합산은 코드가 하고, 근거 헤드라인이 실제 입력에 없으면 0 으로 처리한다.
주도섹터는 '테마 중요도'가 아니라 '시장 대비 상대 강도'가 따라야 한다(지표가 있는 AI·반도체·메모리·전력 계열만 판정 가능).
"""
import config
from scoring import policy

LEVEL_TEXT = policy.LEVEL_TEXT


def _clamp(x):
    return max(-config.STOCK_LEVEL_MAX, min(config.STOCK_LEVEL_MAX, int(x or 0)))


def score_of(effect):
    """-3..+3 -> 0..100 (0 단계 = 50점)."""
    return round(50 + 50 * effect / config.STOCK_LEVEL_MAX, 1)


def sector_groups_for(code, industry=None):
    """종목코드(6자리) -> 소속 테마 그룹 목록. 명시 매핑 > Yahoo 업종 추정 > 없음."""
    base = code.split(".")[0]
    if base in config.STOCK_SECTOR_GROUPS:
        return list(config.STOCK_SECTOR_GROUPS[base])
    return list(config.STOCK_INDUSTRY_GROUPS.get(industry or "", []))


def sector_strength(group_summary, kospi10d):
    """테마 그룹별 상대 강도(10거래일 변화 - KOSPI 10거래일 변화)와 낙폭, 강한 순 순위."""
    rows = []
    for g, name in config.AI_GROUP_NAMES.items():
        gs = group_summary.get(g)
        if not gs or gs["chg10d"] is None:
            continue
        rs = None if kospi10d is None else gs["chg10d"] - kospi10d
        rows.append(dict(group=g, name=name, chg10d=gs["chg10d"], dd60=gs["dd60"], rs10=rs, n=gs["n"]))
    rows.sort(key=lambda r: (r["rs10"] is None, -(r["rs10"] or 0)))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def assess(item, titles, groups):
    """item: 모델 출력(crowd_sentiment, crowd_evidence, sector_fit, sector_reason, comment) 또는 None(출력 누락).

    반환: dict(crowd_effect, crowd_score, crowd_evidence, crowd_note, sector_effect, sector_score, sector_note, score, comment)
    """
    item = item or {}
    effect = _clamp(item.get("crowd_sentiment"))
    evidence = policy.supported_evidence(item.get("crowd_evidence"), titles)
    c_note = None
    if effect != 0 and not titles:
        effect, c_note = 0, "최근 관련 기사가 없어 0 처리"
    elif effect != 0 and not evidence:
        effect, c_note = 0, "근거 헤드라인을 확인하지 못해 0 처리"
    elif not titles:
        c_note = "최근 관련 기사 없음"
    s_effect = _clamp(item.get("sector_fit"))
    s_note = (item.get("sector_reason") or "").strip() or None
    if not groups:
        s_effect, s_note = 0, "섹터 판정 불가 (AI·반도체·메모리·전력 계열이 아니라 섹터 지표가 없음)"
    c_score, s_score = score_of(effect), score_of(s_effect)
    w = config.STOCK_VIEW_WEIGHTS
    total = round(w["crowd"] * c_score + w["sector"] * s_score, 1)
    return dict(crowd_effect=effect, crowd_score=c_score, crowd_evidence=evidence if effect != 0 else [], crowd_note=c_note,
                sector_effect=s_effect, sector_score=s_score, sector_note=s_note, score=total,
                comment=(item.get("comment") or "").strip() or None)
