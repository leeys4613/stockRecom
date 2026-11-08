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
from collectors import macro, news, reports
from scoring import engine, picks
from scoring import policy as policy_mod

_lock = threading.Lock()


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def build_context(macro_rows, news_rows, scored):
    policy_topics = set(config.POLICY_TOPICS.values())
    return {
        "base_score": scored["score"],
        "coverage": round(scored["coverage"], 2),
        "missing": scored["missing"],
        "contributions": {k: round(v, 3) for k, v in scored["contributions"].items()},
        "macro": [dict(key=r["key"], name=r["name"], value=r["value"],
                       change_pct=None if r["change_pct"] is None else round(r["change_pct"], 2),
                       freshness=r["freshness"]) for r in macro_rows],
        "news": {n["name"]: [i["title"] for i in n["headlines"]] for n in news_rows
                 if n["status"] == "ok" and n["topic"] not in policy_topics},
        "policy_news": {r: [i["title"] for n in news_rows if n["topic"] == t and n["status"] == "ok"
                            for i in n["headlines"]] for r, t in config.POLICY_TOPICS.items()},
        "news_unavailable": [n["name"] for n in news_rows if n["status"] != "ok"],
    }


def run_once(provider=None, macro_fetcher=None, news_fetcher=None, closes_fetcher=None):
    """한 번의 종합 분석. 이미 실행 중이면 None. 성공 시 저장된 analyses.id."""
    if not _lock.acquire(blocking=False):
        return None
    try:
        macro_rows_new = macro.collect_all(macro_fetcher or macro.yf_fetch)
        news_new = news.collect_all(news_fetcher or news.http_fetch)
        with db.session() as conn:
            macro.save(conn, macro_rows_new)
            news.save(conn, news_new)
            macro_rows = macro.latest(conn)
            news_rows = news.latest(conn)

        with db.session() as conn:
            wset = weights_mod.get_active(conn)  # 이번 분석에 쓸 가중치/비중 규칙 버전 (분석 도중 바뀌어도 일관되게)
            aset = allocations.get_active(conn)
        changes = {r["key"]: (r["change_pct"] if r["freshness"] == "fresh" else None)
                   for r in macro_rows}
        scored = engine.compute_score(changes, wset["weights"], wset["saturation"])

        if scored["score"] is None:
            result = dict(status="skipped", provider=None, analysis=None,
                          error="유효한 지표가 없어 분석 생략")
            base = final = None
            adj = 0
            pol = None
        else:
            ctx = build_context(macro_rows, news_rows, scored)
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
                 json.dumps(a.summary_3lines, ensure_ascii=False) if a else None,
                 a.risk_comment if a else None, result["error"],
                 json.dumps(build_context(macro_rows, news_rows, scored), ensure_ascii=False),
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
