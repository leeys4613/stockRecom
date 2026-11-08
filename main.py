from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
import db
import performance
import pipeline
from collectors import macro, news

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
        ctx = dict(active="dash", rows=macro.latest(conn), news=news.latest(conn),
                   analysis=pipeline.latest_analysis(conn))
    return templates.TemplateResponse(request, "index.html", ctx)


@app.post("/refresh")
def refresh(background: BackgroundTasks):
    """수집+분석은 오래 걸릴 수 있어(LLM) 백그라운드로 돌리고 즉시 대시보드로 돌아간다."""
    background.add_task(pipeline.run_once)
    return RedirectResponse("/", status_code=303)


@app.get("/performance")
def performance_page(request: Request):
    with db.session() as conn:
        ctx = dict(active="perf", s1=performance.summarize(conn, "d1"),
                   s5=performance.summarize(conn, "d5"), recent=performance.recent(conn))
    return templates.TemplateResponse(request, "performance.html", ctx)


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    # 루트 스코프로 서비스해야 전체 사이트를 제어할 수 있고, 갱신이 지연되지 않도록 캐시하지 않는다
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})
