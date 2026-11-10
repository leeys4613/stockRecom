"""종합 분석 파이프라인: 매크로·뉴스 수집 → 규칙 점수 → LLM 보정 → 저장.

신선하지 않은(stale/failed) 지표는 점수에서 결측으로 취급한다 (오래된 값으로 점수를 내지 않는다).
"""
import json
import threading
import time
from datetime import datetime, timezone

import calendar_kr
import config
import db
import alerts
import allocations
import gauge
import history
import performance
import screener
import stockviews
import weights as weights_mod
from analyzer import llm, stock_llm
from collectors import ai_tracker, macro, news, reports
from scoring import engine, picks
from scoring import policy as policy_mod
from scoring import levels, sentiment
from scoring import stock_view

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
    cx = capex["total"]
    if basket and cx:
        dd = sum(r["dd60"] for r in basket) / len(basket)
        q = cx["qoq"]
        hist = cx["qoq_history"]
        decel = len(hist) >= 2 and hist[-1]["qoq"] < hist[-2]["qoq"] - config.CAPEX_QOQ_DECEL_PP
        tail = f" 최근 분기 대비 증가율은 직전({hist[-2]['qoq']:+.0f}%)보다 낮아졌습니다." if decel else ""
        if cx.get("overall"):
            tail += f" 분기·2분기·3분기 기준 증가율 추세는 '{cx['overall']}'입니다."
        if q >= config.CAPEX_QOQ_STRONG and dd < -15:
            reading = (f"빅테크 설비투자는 직전 분기보다 {q:+.0f}% 늘고 있지만 AI 대형주는 고점 대비 크게 빠져 있습니다. "
                       f"시장이 투자의 지속성·수익성을 의심하는 상태로 읽힙니다.{tail}")
        elif q >= config.CAPEX_QOQ_STRONG:
            reading = f"빅테크 설비투자가 직전 분기보다 {q:+.0f}% 늘고 AI 대형주 주가도 유지되고 있습니다. 투자 사이클에 대한 시장 신뢰가 유지되는 상태입니다.{tail}"
        elif q >= 0:
            reading = f"빅테크 설비투자 증가가 직전 분기 대비 {q:+.0f}%로 둔화됐습니다. 데이터센터 건설 속도 둔화 신호인지 확인이 필요합니다.{tail}"
        else:
            reading = f"빅테크 설비투자가 직전 분기보다 {q:+.0f}% 줄었습니다. 건설 속도 둔화의 직접 신호라 가장 주의해서 볼 대목입니다."
    # 건설 속도 신호: 전력·전기 인프라(수요 측 병목). 한국 전력기기주는 코스피 급락 영향이 섞이므로 미국 전력주로 판정한다
    pw_us = [r for r in rows if r["grp"] == "power" and not r["ticker"].endswith(".KS") and r["price"] is not None and not r["stale"]]
    pw_kr = [r for r in rows if r["grp"] == "power" and r["ticker"].endswith(".KS") and r["price"] is not None and not r["stale"]]
    build_reading = None
    if len(pw_us) >= 3:
        d = sum(r["dd60"] for r in pw_us) / len(pw_us)
        kr = f", 한국 전력기기주 평균 {sum(r['dd60'] for r in pw_kr) / len(pw_kr):+.0f}%(코스피 급락 영향 포함)" if pw_kr else ""
        head = f"미국 전력·전기 인프라주는 60일 고점 대비 평균 {d:+.0f}%{kr}. "
        if d > -10:
            build_reading = head + "비교적 견조해 건설 수요 자체는 유지되는 신호이며, AI 종목 약세는 사업자 수익성·자금 조달 우려에 가깝습니다."
        elif d > -25:
            build_reading = head + "상당히 조정받고 있어, 건설 속도 우려가 인프라 전반으로 번지는 중간 단계로 읽힙니다."
        else:
            build_reading = head + "크게 약세여서, 데이터센터 건설 속도 둔화 우려가 공급망 전반에 반영된 상태로 읽힙니다."
    cx_total = capex["total"]
    trend_rows = ([("합계", cx_total["trend"], cx_total["overall"])] if cx_total else []) + \
                 [(p["ticker"], p["trend"], p["trend_overall"]) for p in capex["per"] if p.get("latest")]
    return dict(rows=rows, groups=groups, capex=capex, reading=reading, build_reading=build_reading,
                capex_trend_rows=trend_rows)


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
            qoq_pct=r1(view["capex"]["total"]["qoq"]), yoy_pct=r1(view["capex"]["total"]["yoy"]),
            growth_trend=[dict(horizon=t["label"], now_pct=r1(t["now"]), prev_pct=r1(t["prev"]), delta_pp=r1(t["delta"]),
                               verdict=t["verdict"]) for t in view["capex"]["total"]["trend"]],
            growth_trend_overall=view["capex"]["total"]["overall"],
            qoq_history=[dict(quarter=h["period"], qoq_pct=r1(h["qoq"])) for h in view["capex"]["total"]["qoq_history"]],
            n=view["capex"]["total"]["n"]),
        "bigtech_capex": [dict(t=p["ticker"], quarter=p.get("latest"), qoq_pct=r1(p.get("qoq")), yoy_pct=r1(p.get("yoy")),
                               flags=p.get("flags", []), growth_trend_overall=p.get("trend_overall"),
                               growth_trend=[dict(horizon=t["label"], now_pct=r1(t["now"]), prev_pct=r1(t["prev"]),
                                                  verdict=t["verdict"]) for t in p.get("trend", [])])
                          for p in view["capex"]["per"]],
        "capex_note": "분기 대비(QoQ)가 기본 지표(투자 속도 변화), 전년 대비(YoY)는 보조. growth_trend 는 1·2·3분기 전 대비 이번 증가율과 직전 증가율의 비교(둔화/가속)",
        "reading": view["reading"],
        "construction_reading": view["build_reading"],
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
            "weight 0": "가중치가 0이거나 indicator_weights 에 없는 지표(예: POWER10D 전력 인프라)는 점수에 반영되지 않는 참고 지표다. "
                        "데이터센터 건설 속도의 간접 신호(전력·변압기 병목, 지역 규제)로 해석에 활용하라",
            "kind=feature": "value 자체가 추세·수준 지표(점수 입력): KOSPI10D=KOSPI 최근 10거래일 변화율(%), "
                            "KOSPIDD=최근 10거래일 고점 대비 낙폭(%), VIXAVG=VIX 10일 평균(20이 중립, 높을수록 위험 국면). "
                            "하루 반등에 점수가 과대평가되지 않도록 넣은 지표다",
        },
    }


def fear_greed_signal(macro_rows):
    """최신(fresh) 지표로 공포·환희 보조 신호를 계산한다. 점수·LLM 입력에는 쓰지 않는다."""
    v = {r["key"]: r["value"] for r in macro_rows if r["freshness"] == "fresh" and r["value"] is not None}
    return sentiment.assess(vix=v.get("VIX"), vix_avg=v.get("VIXAVG"), kospi_10d=v.get("KOSPI10D"),
                            kospi_dd=v.get("KOSPIDD"))


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
        fg = fear_greed_signal(macro_rows)
        with db.session() as conn:
            cur = conn.execute(
                "INSERT INTO analyses (created_at,llm_status,provider,base_score,adjustment,"
                "final_score,coverage,missing,summary,risk_comment,error,inputs,weight_set_id,"
                "allocation_set_id,policy_us,policy_kr,policy,fear_greed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 result["status"], result["provider"], base, adj, final, scored["coverage"],
                 json.dumps(scored["missing"]),
                 json.dumps(a.summary_lines, ensure_ascii=False) if a else None,
                 a.risk_comment if a else None, result["error"],
                 json.dumps(ctx, ensure_ascii=False),
                 wset["id"], aset["id"],
                 pol["us"]["points"] if pol else None, pol["kr"]["points"] if pol else None,
                 json.dumps(pol, ensure_ascii=False) if pol else None,
                 json.dumps(fg, ensure_ascii=False) if fg else None))
            analysis_id = cur.lastrowid
        _fill_outcomes_best_effort(closes_fetcher)
        _stock_views_best_effort(provider, history_fetcher)
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
    d["fear_greed"] = json.loads(d["fear_greed"]) if d.get("fear_greed") else None
    d["fg_divergence"] = sentiment.divergence(d["fear_greed"], d["final_score"])
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


def run_reports_and_views(provider=None):
    """컨센서스 스냅샷(참고용) + 관심종목 AI 적합도 판정(수동 갱신/일일 작업)."""
    n = run_reports()
    run_stock_views(provider, force=True)
    return n


def scheduled_reports_job():
    if not calendar_kr.is_trading_day(calendar_kr.today_kst()):
        return None
    return run_reports_and_views()


def scheduled_screen_job():
    """장 마감 후 지수 대비 강세 종목 스캔 (거래일만)."""
    if not calendar_kr.is_trading_day(calendar_kr.today_kst()):
        return None
    rid = screener.run(db.session)
    try:    # 과거 비교용 장기 일봉도 같이 갱신한다(실패해도 스캔 결과에는 영향 없음)
        history.refresh(db.session)
    except Exception:
        pass
    return rid


_stock_lock = threading.Lock()


def _stock_inputs(item, history_fetcher, headline_fetcher, industry_fetcher, ohlcv_fetcher=None):
    """종목 하나의 판정 입력: 가격 지표, 최근 헤드라인, 소속 테마 그룹. 일부가 실패해도 나머지는 계속한다."""
    from collectors import macro

    sym = reports.symbol(item["code"], item["market"])
    out = dict(code=item["code"], name=item["name"], market=item["market"], price=None, titles=[], groups=[], errors=[])
    try:
        price, c1, c10, dd = ai_tracker.metrics_from_closes((history_fetcher or macro.yf_closes)(sym))
        out["price"] = dict(price=round(price, 2), chg1d=round(c1, 1), chg10d=round(c10, 1), dd60=round(dd, 1))
    except Exception as e:
        out["errors"].append(f"가격 이력: {type(e).__name__}: {e}"[:120])
    try:   # 지지·저항은 숫자를 코드가 계산한다. 실패해도 나머지 판정에는 영향이 없다
        out["levels"] = levels.compute((ohlcv_fetcher or macro.yf_ohlcv)(sym))
    except Exception as e:
        out["levels"] = None
        out["errors"].append(f"일봉/지지저항: {type(e).__name__}: {e}"[:120])
    try:
        out["titles"] = news.stock_headlines(item["name"], headline_fetcher)
    except Exception as e:
        out["errors"].append(f"뉴스: {type(e).__name__}: {e}"[:120])
    industry = None
    if item["code"] not in config.STOCK_SECTOR_GROUPS:
        try:
            industry = (industry_fetcher or reports.yf_industry)(sym)
        except Exception:
            industry = None
    out["groups"] = stock_view.sector_groups_for(item["code"], industry)
    return out


def _market_context(conn, tview):
    a = latest_analysis(conn)
    if a is None:
        return None
    al = a["allocation"]
    return dict(
        final_score=a["final_score"], band=f"매수 가능성 {al['label']}" if al else None,
        equity_range=f"{al['equity_min']}~{al['equity_max']}%" if al else None,
        summary=a["summary"],
        judgments={r: f"{v['level_text']}({v['effect']:+d})" for r, v in (a["policy"] or {}).items()},
        ai_tracker=dict(groups=tracker_context(tview)["groups"], reading=tview["reading"],
                        construction_reading=tview["build_reading"]))


def run_stock_views(provider=None, history_fetcher=None, headline_fetcher=None, industry_fetcher=None, force=False, only=None,
                    ohlcv_fetcher=None):
    """관심종목의 대중심리·주도섹터 적합도를 판정해 저장한다. 반환: 저장한 종목 수, 건너뛰면 None(실행 중/최소 간격 이내)."""
    if not _stock_lock.acquire(blocking=False):
        return None
    try:
        with db.session() as conn:
            items = reports.watchlist(conn)
            if only:
                items = [w for w in items if w["code"] in only]
            if not items:
                return 0
            if not force and not stockviews.due(conn, [w["code"] for w in items], config.STOCK_VIEW_MIN_HOURS):
                return None
            rows = macro.latest(conn)
            tview = tracker_view(conn)
            market = _market_context(conn, tview)
            analysis_id = (conn.execute("SELECT MAX(id) FROM analyses").fetchone()[0])
        kospi = {r["key"]: r["value"] for r in rows if r["key"] in ("KOSPI10D", "KOSPIDD") and r["freshness"] == "fresh"}
        strength = stock_view.sector_strength(tview["groups"], kospi.get("KOSPI10D"))
        inputs = []
        for i, it in enumerate(items):
            if i and config.STOCK_VIEW_DELAY_SEC:
                time.sleep(config.STOCK_VIEW_DELAY_SEC)
            inputs.append(_stock_inputs(it, history_fetcher, headline_fetcher, industry_fetcher, ohlcv_fetcher))
        ctx = dict(
            market=market, kospi=dict(chg10d=None if "KOSPI10D" not in kospi else round(kospi["KOSPI10D"], 1),
                                      dd10=None if "KOSPIDD" not in kospi else round(kospi["KOSPIDD"], 1)),
            sector_strength=[dict(group=r["name"], chg10d=round(r["chg10d"], 1), dd60=None if r["dd60"] is None else round(r["dd60"], 1),
                                  rs10=None if r["rs10"] is None else round(r["rs10"], 1), rank=r["rank"]) for r in strength],
            stocks=[dict(code=x["code"], name=x["name"], sector_groups=[config.AI_GROUP_NAMES[g] for g in x["groups"]],
                         price=x["price"], headlines=x["titles"]) for x in inputs])
        expected = {x["code"] for x in inputs}
        try:
            prov = provider or llm.get_provider()
            result = stock_llm.analyze_stocks(ctx, prov, expected)
        except Exception as e:
            result = dict(status="failed", items={}, error=f"{type(e).__name__}: {e}"[:300])
        out = []
        for x in inputs:
            metrics = dict(x["price"] or {}, headlines=len(x["titles"]), errors=x["errors"], levels=x.get("levels"))
            raw = result["items"].get(x["code"])
            if result["status"] != "ok" or raw is None:
                out.append(stockviews.make_row(x["code"], analysis_id, groups=x["groups"], metrics=metrics,
                                               error=result["error"] or "AI 판정에서 이 종목이 빠졌습니다"))
            else:
                out.append(stockviews.make_row(x["code"], analysis_id, stock_view.assess(raw, x["titles"], x["groups"]),
                                               x["groups"], metrics))
        with db.session() as conn:
            stockviews.save(conn, out)
        try:   # 알림 실패가 판정 저장에 영향을 주지 않는다
            with db.session() as conn:
                alerts.check_and_notify(conn)
        except Exception:
            pass
        return len(out)
    finally:
        _stock_lock.release()


def _stock_views_best_effort(provider, history_fetcher):
    try:
        run_stock_views(provider, history_fetcher, force=False)
    except Exception:
        pass


def top_picks_view(conn, now=None):
    """(추천 Top N, 순위가 매겨진 전체 목록, 최신 컨센서스 수집 시각, 최신 판정 시각).

    순위는 AI 적합도 점수(대중심리 + 주도섹터)다. 증권사 컨센서스는 참고 정보로만 각 항목에 붙는다."""
    rows = reports.stock_rows(conn)
    evaluated = [picks.evaluate(r, now) for r in rows]
    for e in evaluated:
        e["view_last"], e["view"] = stockviews.latest(conn, e["code"])
    ranked = sorted((e for e in evaluated if e["view"]), key=lambda e: e["view"]["score"], reverse=True)
    pending = [e for e in evaluated if not e["view"]]
    last_cons = max((r["ok"]["fetched_at"] for r in rows if r["ok"]), default=None)
    return ranked[:config.PICK_TOP_N], ranked + pending, last_cons, stockviews.last_time(conn)
