"""종합 분석 파이프라인: 매크로·뉴스 수집 → 규칙 점수 → LLM 보정 → 저장.

신선하지 않은(stale/failed) 지표는 점수에서 결측으로 취급한다 (오래된 값으로 점수를 내지 않는다).
"""
import json
import threading
from datetime import datetime, timezone

import calendar_kr
import config
import db
import allocations
import gauge
import performance
import weights as weights_mod
from analyzer import llm
from collectors import ai_tracker, macro, news, reports
from scoring import engine, picks
from scoring import policy as policy_mod

_lock = threading.Lock()


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def score_inputs(macro_rows):
    """지표별 점수 입력. 일반 지표는 전일 변동률, 파생(추세/수준) 지표는 값 자체(중립값 보정).
    최신이 아닌(stale/failed) 지표는 None(결측) 처리한다."""
    out = {}
    for r in macro_rows:
        if r["freshness"] != "fresh":
            out[r["key"]] = None
        elif r["kind"] == "feature":
            out[r["key"]] = r["value"] - config.SCORE_CENTER.get(r["key"], 0.0)
        else:
            out[r["key"]] = r["change_pct"]
    return out


def build_context(macro_rows, news_rows, scored):
    policy_topics = {t for ts in config.POLICY_TOPICS.values() for t in ts}
    return {
        "base_score": scored["score"],
        "coverage": round(scored["coverage"], 2),
        "missing": scored["missing"],
        "contributions": {k: round(v, 3) for k, v in scored["contributions"].items()},
        "macro": [dict(key=r["key"], name=r["name"], kind=r["kind"],
                       value=None if r["value"] is None else round(r["value"], 2),
                       change_pct=None if r["change_pct"] is None else round(r["change_pct"], 2),
                       freshness=r["freshness"]) for r in macro_rows],
        "news": {n["name"]: [i["title"] for i in n["headlines"]] for n in news_rows
                 if n["status"] == "ok" and n["topic"] not in policy_topics},
        "policy_news": {r: [i["title"] for n in news_rows if n["topic"] in topics and n["status"] == "ok"
                            for i in n["headlines"]] for r, topics in config.POLICY_TOPICS.items()},
        "news_unavailable": [n["name"] for n in news_rows if n["status"] != "ok"],
    }


def tracker_view(conn):
    """대시보드/AI 입력용 AI 투자 사이클 트래커: 종목별·종목군별 + 빅테크 CAPEX + 규칙 기반 한 줄 해석."""
    rows = ai_tracker.latest(conn)
    groups = ai_tracker.group_summary(rows)
    capex = ai_tracker.capex_summary(conn)
    basket = [r for r in rows if r["grp"] in config.AI_BASKET_GROUPS and r["price"] is not None and not r["stale"]]
    reading = None
    if basket and capex["total"]:
        dd = sum(r["dd60"] for r in basket) / len(basket)
        if capex["total"]["yoy"] > 10 and dd < -15:
            reading = "빅테크 설비투자는 늘고 있지만 AI 대형주는 고점 대비 크게 빠져 있습니다. 시장이 투자의 지속성·수익성을 의심하는 상태로 읽힙니다."
        elif capex["total"]["yoy"] > 10 and dd > -8:
            reading = "빅테크 설비투자 증가와 AI 대형주 주가가 함께 유지되고 있습니다. 투자 사이클에 대한 시장 신뢰가 유지되는 상태입니다."
        elif capex["total"]["yoy"] <= 10:
            reading = "빅테크 설비투자 증가세가 둔화됐습니다. 데이터센터 건설 속도 둔화 신호인지 확인이 필요합니다."
    return dict(rows=rows, groups=groups, capex=capex, reading=reading)


def tracker_context(view):
    """AI 입력용 요약(값 반올림)."""
    r1 = lambda x: None if x is None else round(x, 1)  # noqa: E731
    return {
        "groups": {config.AI_GROUP_NAMES[g]: dict(n=v["n"], chg10d=r1(v["chg10d"]), dd60=r1(v["dd60"]))
                   for g, v in view["groups"].items()},
        "tickers": [dict(t=r["ticker"], name=r["name"], group=config.AI_GROUP_NAMES[r["grp"]], chg1d=r1(r["chg1d"]),
                         chg10d=r1(r["chg10d"]), dd60=r1(r["dd60"]), stale=r["stale"]) for r in view["rows"]],
        "bigtech_capex_total": None if not view["capex"]["total"] else dict(
            quarter=view["capex"]["total"]["period"], usd_bn=round(view["capex"]["total"]["capex"] / 1e9, 1),
            yoy_pct=r1(view["capex"]["total"]["yoy"]), n=view["capex"]["total"]["n"]),
        "bigtech_capex": [dict(t=p["ticker"], quarter=p.get("latest"), yoy_pct=r1(p.get("yoy")), flags=p.get("flags", []))
                          for p in view["capex"]["per"]],
        "reading": view["reading"],
    }


def algorithm_info(wset, aset, scored):
    """AI 가 현재 알고리즘 구조에 맞춰 해석할 수 있도록 점수 공식·구간표·정책 상한·규칙 점수 구간을 알려준다."""
    rule = None
    if scored["score"] is not None:
        a = engine.allocation(scored["score"], aset["bands"])
        rule = dict(score=scored["score"], band=f"매수 가능성 {a['label']}",
                    equity=f"{a['equity_min']}~{a['equity_max']}%")
    return {
        "formula": "최종 점수 = 규칙 점수(지표 가중 합산) + 정책 반영(미국/한국, 단계/3×상한) + 일반 보정. 구간별 권장 주식 비중이 정해진다.",
        "rule_score_band": rule,
        "policy_caps_pts": wset.get("policy") or {},
        "bands": [dict(score_from=b[0], band=f"매수 가능성 {b[1]}", equity=f"{b[2]}~{b[3]}%") for b in aset["bands"]],
        "indicator_weights": {k: round(v, 3) for k, v in wset["weights"].items()},
        "indicator_notes": {
            "kind=change": "value 는 현재값, change_pct 는 전일 변동률(점수 입력)",
            "kind=feature": "value 자체가 추세·수준 지표(점수 입력): KOSPI10D=KOSPI 최근 10거래일 변화율(%), "
                            "KOSPIDD=최근 10거래일 고점 대비 낙폭(%), VIXAVG=VIX 10일 평균(20이 중립, 높을수록 위험 국면). "
                            "하루 반등에 점수가 과대평가되지 않도록 넣은 지표다",
        },
    }


def run_once(provider=None, macro_fetcher=None, news_fetcher=None, closes_fetcher=None, history_fetcher=None,
             capex_fetcher=None):
    """한 번의 종합 분석. 이미 실행 중이면 None. 성공 시 저장된 analyses.id."""
    if not _lock.acquire(blocking=False):
        return None
    try:
        ai_snaps = ai_tracker.collect(history_fetcher)
        macro_rows_new = macro.collect_all(macro_fetcher or macro.yf_fetch, history_fetcher, ai_snaps)
        news_new = news.collect_all(news_fetcher or news.http_fetch)
        with db.session() as conn:
            macro.save(conn, macro_rows_new)
            news.save(conn, news_new)
            ai_tracker.save(conn, ai_snaps)
            macro_rows = macro.latest(conn)
            news_rows = news.latest(conn)
            capex_needed = ai_tracker.capex_due(conn)
        if capex_needed:  # 분기 데이터라 하루 1번이면 충분. 실패해도 분석은 계속한다
            collected, _failed = ai_tracker.collect_capex(capex_fetcher)
            if collected:
                with db.session() as conn:
                    ai_tracker.save_capex(conn, collected)
        with db.session() as conn:
            tracker = tracker_view(conn)

        with db.session() as conn:
            wset = weights_mod.get_active(conn)  # 이번 분석에 쓸 가중치/비중 규칙 버전 (분석 도중 바뀌어도 일관되게)
            aset = allocations.get_active(conn)
        scored = engine.compute_score(score_inputs(macro_rows), wset["weights"], wset["saturation"])

        ctx = build_context(macro_rows, news_rows, scored)
        ctx["algorithm"] = algorithm_info(wset, aset, scored)
        ctx["ai_tracker"] = tracker_context(tracker)
        if scored["score"] is None:
            result = dict(status="skipped", provider=None, analysis=None,
                          error="유효한 지표가 없어 분석 생략")
            base = final = None
            adj = 0
            pol = None
        else:
            result = llm.analyze(ctx, provider)
            base = scored["score"]
            caps = wset.get("policy") or {}   # 구 버전(정책 상한 없음)은 정책 반영 0
            if result["analysis"]:
                adj = _clamp(result["analysis"].adjustment, -config.LLM_MAX_ADJUSTMENT, config.LLM_MAX_ADJUSTMENT)
                pol = policy_mod.assess(result["analysis"], ctx["policy_news"], caps)
            else:
                adj, pol = 0, policy_mod.empty(caps)
            final = round(_clamp(base + policy_mod.total(pol) + adj, 0, 100), 1)

        a = result["analysis"]
        with db.session() as conn:
            cur = conn.execute(
                "INSERT INTO analyses (created_at,llm_status,provider,base_score,adjustment,"
                "final_score,coverage,missing,summary,risk_comment,error,inputs,weight_set_id,"
                "allocation_set_id,policy_us,policy_kr,policy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 result["status"], result["provider"], base, adj, final, scored["coverage"],
                 json.dumps(scored["missing"]),
                 json.dumps(a.summary_lines, ensure_ascii=False) if a else None,
                 a.risk_comment if a else None, result["error"],
                 json.dumps(ctx, ensure_ascii=False),
                 wset["id"], aset["id"],
                 pol["us"]["points"] if pol else None, pol["kr"]["points"] if pol else None,
                 json.dumps(pol, ensure_ascii=False) if pol else None))
            analysis_id = cur.lastrowid
        _fill_outcomes_best_effort(closes_fetcher)
        return analysis_id
    finally:
        _lock.release()


def _fill_outcomes_best_effort(closes_fetcher):
    """과거 예측의 사후 수익률 채우기. 실패해도 이번 분석 결과에는 영향 없다."""
    try:
        with db.session() as conn:
            performance.fill_outcomes(conn, closes_fetcher)
    except Exception:
        pass


def latest_analysis(conn):
    row = conn.execute("SELECT * FROM analyses ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    d = dict(row)
    d["missing"] = json.loads(d["missing"]) if d["missing"] else []
    d["summary"] = json.loads(d["summary"]) if d["summary"] else []
    d["policy"] = json.loads(d["policy"]) if d.get("policy") else None
    d["policy_total"] = policy_mod.total(d["policy"]) if d["policy"] else 0.0
    ws = weights_mod.get(conn, d.get("weight_set_id")) if d.get("weight_set_id") else None
    d["weight_label"] = ws["label"] if ws else None
    # 비중은 그 분석 당시의 규칙 버전으로 계산한다 (규칙을 나중에 바꿔도 과거 기록은 그대로)
    aset = (allocations.get(conn, d.get("allocation_set_id")) if d.get("allocation_set_id")
            else None) or allocations.get_active(conn)
    d["allocation_label"] = aset["label"]
    d["gauge"] = gauge.build(d["final_score"], aset["bands"], d["base_score"])
    d["allocbar"] = None  # allocation 계산 뒤에 채운다
    d["allocation"] = (engine.allocation(d["final_score"], aset["bands"])
                       if d["final_score"] is not None else None)
    if d["allocation"]:
        d["allocbar"] = gauge.allocation_bar(d["allocation"], config.EQUITY_CAP_PCT)
    s = d["final_score"]
    d["signal"] = (None if s is None else
                   "green" if s >= config.SIGNAL_GREEN else
                   "yellow" if s >= config.SIGNAL_YELLOW else "red")
    return d


def scheduled_job():
    """스케줄러 진입점: 거래일에만 실행."""
    if not calendar_kr.is_trading_day(calendar_kr.today_kst()):
        return None
    return run_once()


def run_reports(fetcher=None, delay=None):
    """관심종목 컨센서스 스냅샷 (하루 1회 + 수동). 이미 실행 중이면 None."""
    return reports.collect_watchlist(db.session, fetcher, delay)


def scheduled_reports_job():
    if not calendar_kr.is_trading_day(calendar_kr.today_kst()):
        return None
    return run_reports()


def top_picks_view(conn, now=None):
    """(Top Picks, 전체 평가 목록, 최신 수집 시각)."""
    rows = reports.stock_rows(conn)
    top, evaluated = picks.top_picks(rows, now)
    last = max((r["ok"]["fetched_at"] for r in rows if r["ok"]), default=None)
    return top, evaluated, last
