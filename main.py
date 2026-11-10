from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import alerts
import analog
import history
import allocations
import config
import db
import performance
import pipeline
import screener
import stockmaster
import weights as weights_mod
from scoring import picks as picks_mod
from scoring import sentiment
from scoring import policy as policy_mod
from collectors import macro, news, reports

templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))


def _kst(iso):
    if not iso:
        return "-"
    try:
        return (datetime.fromisoformat(iso).astimezone(ZoneInfo(config.TIMEZONE))
                .strftime("%m-%d %H:%M"))
    except ValueError:
        return iso


templates.env.filters["kst"] = _kst
templates.env.globals["now_kst"] = lambda: datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%m-%d %H:%M")


def build_scheduler():
    sched = BackgroundScheduler(timezone=config.TIMEZONE)
    for hour, minute in config.SCHEDULE_TIMES:
        sched.add_job(pipeline.scheduled_job, CronTrigger(hour=hour, minute=minute),
                      id=f"analysis-{hour:02d}{minute:02d}", coalesce=True,
                      max_instances=1, misfire_grace_time=1800)
    sched.add_job(pipeline.scheduled_reports_job,
                  CronTrigger(hour=config.REPORTS_SCHEDULE[0], minute=config.REPORTS_SCHEDULE[1]),
                  id="reports-daily", coalesce=True, max_instances=1, misfire_grace_time=3600)
    sched.add_job(pipeline.scheduled_screen_job,
                  CronTrigger(hour=config.SCREEN_SCHEDULE[0], minute=config.SCREEN_SCHEDULE[1]),
                  id="screen-daily", coalesce=True, max_instances=1, misfire_grace_time=3600)
    return sched


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    sched = None
    if config.SCHEDULER_ENABLED:
        sched = build_scheduler()
        sched.start()
    app.state.scheduler = sched
    yield
    if sched:
        sched.shutdown(wait=False)


app = FastAPI(title="K-MacroSight", lifespan=lifespan)
STATIC_DIR = config.BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index(request: Request):
    with db.session() as conn:
        top, _, cons_at, view_at = pipeline.top_picks_view(conn)
        ctx = dict(active="dash", analysis=pipeline.latest_analysis(conn), picks=top, picks_at=cons_at,
                   view_at=view_at, watch_count=len(reports.watchlist(conn)))
    return templates.TemplateResponse(request, "index.html", ctx)


@app.get("/screener")
def screener_page(request: Request):
    with db.session() as conn:
        last, ok, items = screener.latest(conn)
        watch = {w["code"] for w in reports.watchlist(conn)}
    sections = [dict(key=k, title=title, index=idx, items=[i for i in items if i["market"] == k])
                for k, title, idx in (("KS", "코스피", "KOSPI"), ("KQ", "코스닥", "KOSDAQ"))]
    ctx = dict(active="screen", last=last, run=ok, sections=sections, watch=watch, flag_text=screener.FLAG_TEXT,
               markets=reports.MARKETS, cfg=config, level_text=policy_mod.LEVEL_TEXT)
    return templates.TemplateResponse(request, "screener.html", ctx)


@app.get("/screener/review")
def screener_review_page(request: Request):
    with db.session() as conn:
        rv = screener.review(conn)
    return templates.TemplateResponse(request, "screener_review.html", dict(active="screen", rv=rv))


@app.post("/screener/refresh")
def screener_refresh(background: BackgroundTasks):
    background.add_task(screener.run, db.session)
    return RedirectResponse("/screener", status_code=303)


@app.get("/analog")
def analog_page(request: Request):
    with db.session() as conn:
        ks, vx = history.load(conn, "KOSPI"), history.load(conn, "VIX")
        live = {r["key"]: r["value"] for r in macro.latest(conn) if r["freshness"] == "fresh" and r["value"] is not None}
    rows = analog.build_rows(ks, vx) if ks and vx else []
    ctx = dict(active="analog", ready=bool(rows), sep=config.ANALOG_SEP_DAYS,
               span=dict(start=rows[0]["date"] if rows else "-", end=ks[-1][0] if ks else "-", n=len(rows)))
    if rows:
        bt, base = analog.backtest(rows)
        keys = ("VIX", "VIXAVG", "KOSPI10D", "KOSPIDD")
        if all(k in live for k in keys):
            cur = dict(vix=live["VIX"], vixavg=live["VIXAVG"], k10=live["KOSPI10D"], kdd=live["KOSPIDD"])
            cur_src = "앱의 최신 지표"
        else:
            cur = {k: rows[-1][k] for k in ("vix", "vixavg", "k10", "kdd")}
            cur_src = f"일봉 기준 {rows[-1]['date']}"
        sig = sentiment.assess(vix=cur["vix"], vix_avg=cur["vixavg"], kospi_10d=cur["k10"], kospi_dd=cur["kdd"])
        pool = [r for r in rows if r["i"] < rows[-1]["i"] - config.ANALOG_SEP_DAYS]      # 지금과 겹치는 최근 구간은 제외
        near, near_sum = analog.nearest(pool, cur)
        ctx.update(bt=bt, base=base, cur=cur, cur_src=cur_src, cur_label=sig["label"] if sig else "판단 불가", near=near, near_sum=near_sum)
    return templates.TemplateResponse(request, "analog.html", ctx)


@app.post("/analog/refresh")
def analog_refresh(background: BackgroundTasks):
    background.add_task(history.refresh, db.session)
    return RedirectResponse("/analog", status_code=303)


@app.get("/tracker")
def tracker_page(request: Request):
    with db.session() as conn:
        ctx = dict(active="tracker", tracker=pipeline.tracker_view(conn), group_names=config.AI_GROUP_NAMES)
    return templates.TemplateResponse(request, "tracker.html", ctx)


@app.get("/market")
def market_page(request: Request):
    with db.session() as conn:
        ctx = dict(active="market", rows=macro.latest(conn), news=news.latest(conn))
    return templates.TemplateResponse(request, "market.html", ctx)


@app.post("/refresh")
def refresh(background: BackgroundTasks):
    """수집+분석은 오래 걸릴 수 있어(LLM) 백그라운드로 돌리고 즉시 대시보드로 돌아간다."""
    background.add_task(pipeline.run_once)
    return RedirectResponse("/", status_code=303)


@app.get("/performance")
def performance_page(request: Request, v: int | None = None):
    with db.session() as conn:
        versions = []
        for w in weights_mod.list_all(conn):
            s1 = performance.summarize(conn, "d1", w["id"])
            versions.append(dict(w, d1=s1, d5=performance.summarize(conn, "d5", w["id"])))
        ctx = dict(active="perf", selected=v, versions=versions,
                   s1=performance.summarize(conn, "d1", v), s5=performance.summarize(conn, "d5", v),
                   recent=performance.recent(conn))
    return templates.TemplateResponse(request, "performance.html", ctx)


def _settings_ctx(conn, **extra):
    active = weights_mod.get_active(conn)
    # 정책/지표 도입 전의 구 버전이 적용 중이면 없는 지표는 0 / 기본 포화값으로 표시한다
    rows = [dict(key=k, name=name, direction=config.SCORE_DIRECTION[k],
                 share=abs(active["weights"].get(k, 0.0)) * 100,
                 sat=active["saturation"].get(k, config.SCORE_SATURATION_PCT[k]))
            for k, (name, _) in config.MACRO_INDICATORS.items()]
    policy = {**config.POLICY_CAPS_DEFAULT, **(active["policy"] or {})}   # 상한이 없던 영역(ai, mem)은 기본값 표시
    a_active = allocations.get_active(conn)
    return dict(active="settings", rows=rows, current=active, history=weights_mod.list_all(conn),
                indicators=config.MACRO_INDICATORS, sat_min=weights_mod.SAT_MIN,
                sat_max=weights_mod.SAT_MAX, error=None, saved=None,
                bands=a_active["bands"], a_current=a_active, a_history=allocations.list_all(conn),
                cap=config.EQUITY_CAP_PCT, policy=policy, a_error=None, a_saved=None, **extra)


@app.get("/settings")
def settings_page(request: Request, saved: int | None = None, a_saved: int | None = None):
    with db.session() as conn:
        ctx = _settings_ctx(conn)
    ctx["saved"], ctx["a_saved"] = saved, a_saved
    return templates.TemplateResponse(request, "settings.html", ctx)


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    importance = {k: form.get(f"imp_{k}") for k in config.MACRO_INDICATORS}
    saturation = {k: form.get(f"sat_{k}") for k in config.MACRO_INDICATORS}
    note = str(form.get("note") or "")
    policy = {"us": form.get("pol_us"), "kr": form.get("pol_kr"), "ai": form.get("pol_ai"), "mem": form.get("pol_mem")}
    try:
        with db.session() as conn:
            new_id = weights_mod.create_version(conn, importance, saturation, note, policy)
    except weights_mod.WeightError as e:
        with db.session() as conn:
            ctx = _settings_ctx(conn)
        ctx["error"] = str(e)
        return templates.TemplateResponse(request, "settings.html", ctx, status_code=400)
    return RedirectResponse(f"/settings?saved={new_id}", status_code=303)


@app.post("/settings/allocation")
async def settings_allocation_save(request: Request):
    form = await request.form()
    floors = [form.get(f"floor_{i}") for i in range(allocations.N - 1)]
    emins = [form.get(f"emin_{i}") for i in range(allocations.N)]
    emaxs = [form.get(f"emax_{i}") for i in range(allocations.N)]
    try:
        with db.session() as conn:
            new_id = allocations.create_version(conn, floors, emins, emaxs, str(form.get("note") or ""))
    except allocations.AllocationError as e:
        with db.session() as conn:
            ctx = _settings_ctx(conn)
        ctx["a_error"] = str(e)
        return templates.TemplateResponse(request, "settings.html", ctx, status_code=400)
    return RedirectResponse(f"/settings?a_saved={new_id}#allocation", status_code=303)


def _watch_ctx(conn, **extra):
    _, evaluated, cons_at, view_at = pipeline.top_picks_view(conn)
    alert = {w["code"]: w for w in reports.watchlist(conn)}
    return dict(active="watch", items=evaluated, alert=alert, push_n=alerts.subscription_count(conn),
                alert_default=config.ALERT_DEFAULT_MIN, picks_at=cons_at, view_at=view_at, markets=reports.MARKETS,
                level_text=policy_mod.LEVEL_TEXT, group_names=config.AI_GROUP_NAMES,
                master=stockmaster.info(conn),
                max_n=config.WATCHLIST_MAX, min_analysts=config.PICK_MIN_ANALYSTS,
                flag_text=picks_mod.FLAG_TEXT, error=None, added=None, **extra)


@app.get("/watchlist")
def watchlist_page(request: Request, added: str | None = None, master: int | None = None):
    with db.session() as conn:
        ctx = _watch_ctx(conn)
    ctx["added"], ctx["master_loaded"] = added, master
    return templates.TemplateResponse(request, "watchlist.html", ctx)


@app.get("/watchlist/search")
def watchlist_search(q: str = ""):
    with db.session() as conn:
        found = stockmaster.search(conn, q, limit=8)
    for f in found:
        f["market_name"] = reports.MARKETS[f["market"]]
    return JSONResponse(found, headers={"Cache-Control": "no-store"})


@app.post("/watchlist/add")
async def watchlist_add(request: Request, background: BackgroundTasks):
    form = await request.form()
    try:
        with db.session() as conn:
            code, market, name = reports.resolve(conn, form.get("q"), form.get("code"), form.get("market"),
                                                 form.get("name"))
            code = reports.add_stock(conn, code, market, name)
    except reports.WatchlistError as e:
        with db.session() as conn:
            ctx = _watch_ctx(conn)
        ctx["error"] = str(e)
        return templates.TemplateResponse(request, "watchlist.html", ctx, status_code=400)
    background.add_task(pipeline.run_stock_views, None, None, None, None, True, {code})   # 추가한 종목의 AI 적합도 판정
    return RedirectResponse(f"/watchlist?added={code}", status_code=303)


@app.post("/watchlist/master")
async def watchlist_master_upload(request: Request):
    form = await request.form()
    up = form.get("file")
    try:
        if up is None or not hasattr(up, "read"):
            raise stockmaster.StockMasterError("파일을 선택하세요")
        raw = await up.read()
        rows = stockmaster.parse_file(getattr(up, "filename", ""), raw)
        with db.session() as conn:
            n = stockmaster.replace_all(conn, rows)
    except stockmaster.StockMasterError as e:
        with db.session() as conn:
            ctx = _watch_ctx(conn)
        ctx["error"] = f"종목 목록 업로드 실패: {e}"
        return templates.TemplateResponse(request, "watchlist.html", ctx, status_code=400)
    return RedirectResponse(f"/watchlist?master={n}", status_code=303)


@app.post("/watchlist/remove")
async def watchlist_remove(request: Request):
    form = await request.form()
    with db.session() as conn:
        reports.remove_stock(conn, form.get("code"))
    return RedirectResponse("/watchlist", status_code=303)


@app.post("/watchlist/refresh")
def watchlist_refresh(background: BackgroundTasks):
    background.add_task(pipeline.run_reports_and_views)
    return RedirectResponse("/watchlist", status_code=303)


@app.post("/watchlist/alert")
async def watchlist_alert(request: Request):
    form = await request.form()
    try:
        with db.session() as conn:
            alerts.set_alert(conn, form.get("code"), form.get("on") == "1", form.get("min"))
    except alerts.AlertError as e:
        with db.session() as conn:
            ctx = _watch_ctx(conn)
        ctx["error"] = str(e)
        return templates.TemplateResponse(request, "watchlist.html", ctx, status_code=400)
    return RedirectResponse("/watchlist", status_code=303)


@app.get("/push/key")
def push_key():
    with db.session() as conn:
        _, pub = alerts.vapid_keys(conn)
    return JSONResponse({"key": pub}, headers={"Cache-Control": "no-store"})


@app.post("/push/subscribe")
async def push_subscribe(request: Request):
    try:
        data = await request.json()
        with db.session() as conn:
            alerts.subscribe(conn, data)
    except (alerts.AlertError, ValueError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True}


@app.post("/push/unsubscribe")
async def push_unsubscribe(request: Request):
    try:
        data = await request.json()
        endpoint = data["endpoint"]
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"ok": False}, status_code=400)
    with db.session() as conn:
        alerts.unsubscribe(conn, endpoint)
    return {"ok": True}


@app.post("/push/test")
def push_test():
    with db.session() as conn:
        n = alerts.notify_all(conn, dict(title="K-MacroSight 테스트 알림", body="알림이 정상적으로 도착했습니다.",
                                          url="/watchlist", tag="test"))
    return {"ok": n > 0, "sent": n}


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    # 루트 스코프로 서비스해야 전체 사이트를 제어할 수 있고, 갱신이 지연되지 않도록 캐시하지 않는다
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})
