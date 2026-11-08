"""관심종목 AI 적합도 저장/조회."""
import json
from datetime import datetime, timedelta, timezone


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save(conn, rows):
    conn.executemany(
        "INSERT INTO stock_views (created_at,code,status,error,analysis_id,crowd_effect,crowd_score,crowd_evidence,crowd_note,"
        "sector_effect,sector_score,sector_groups,sector_note,score,comment,metrics) VALUES "
        "(:created_at,:code,:status,:error,:analysis_id,:crowd_effect,:crowd_score,:crowd_evidence,:crowd_note,"
        ":sector_effect,:sector_score,:sector_groups,:sector_note,:score,:comment,:metrics)", rows)


def make_row(code, analysis_id, a=None, groups=None, metrics=None, error=None):
    base = dict(created_at=now_iso(), code=code, analysis_id=analysis_id, metrics=json.dumps(metrics or {}, ensure_ascii=False),
                sector_groups=json.dumps(groups or [], ensure_ascii=False))
    if error or a is None:
        return dict(base, status="failed", error=error or "판정 결과 없음", crowd_effect=None, crowd_score=None,
                    crowd_evidence=None, crowd_note=None, sector_effect=None, sector_score=None, sector_note=None,
                    score=None, comment=None)
    return dict(base, status="ok", error=None, crowd_effect=a["crowd_effect"], crowd_score=a["crowd_score"],
                crowd_evidence=json.dumps(a["crowd_evidence"], ensure_ascii=False), crowd_note=a["crowd_note"],
                sector_effect=a["sector_effect"], sector_score=a["sector_score"], sector_note=a["sector_note"],
                score=a["score"], comment=a["comment"])


def _parse(r):
    d = dict(r)
    d["crowd_evidence"] = json.loads(d["crowd_evidence"]) if d.get("crowd_evidence") else []
    d["sector_groups"] = json.loads(d["sector_groups"]) if d.get("sector_groups") else []
    d["metrics"] = json.loads(d["metrics"]) if d.get("metrics") else {}
    return d


def latest(conn, code):
    """최신 시도와 최신 성공(실패해도 마지막 성공 판정은 stale 로 보여준다)."""
    last = conn.execute("SELECT * FROM stock_views WHERE code=? ORDER BY id DESC LIMIT 1", (code,)).fetchone()
    ok = conn.execute("SELECT * FROM stock_views WHERE code=? AND status='ok' ORDER BY id DESC LIMIT 1", (code,)).fetchone()
    return (_parse(last) if last else None), (_parse(ok) if ok else None)


def last_time(conn):
    return conn.execute("SELECT MAX(created_at) FROM stock_views").fetchone()[0]


def due(conn, codes, min_hours):
    """다시 판정해야 하나: 판정이 없는 종목이 있거나, 마지막 판정이 min_hours 보다 오래됐다."""
    if not codes:
        return False
    for c in codes:
        if conn.execute("SELECT 1 FROM stock_views WHERE code=? AND status='ok' LIMIT 1", (c,)).fetchone() is None:
            return True
    t = last_time(conn)
    return t is None or datetime.now(timezone.utc) - datetime.fromisoformat(t) > timedelta(hours=min_hours)
