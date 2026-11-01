from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import config
import db
from collectors import macro

templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="K-MacroSight", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index(request: Request):
    with db.session() as conn:
        rows = macro.latest(conn)
    return templates.TemplateResponse(request, "index.html", {"rows": rows})


@app.post("/refresh")
def refresh():
    rows = macro.collect_all()
    with db.session() as conn:
        macro.save(conn, rows)
    return RedirectResponse("/", status_code=303)
