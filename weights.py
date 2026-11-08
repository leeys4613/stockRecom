"""점수 가중치 세트 관리. 세트는 불변 버전이며, 저장하면 새 버전이 생기고 이후 분석부터 적용된다.

사용자는 지표별 '중요도(0~100)'와 '포화 변동률'만 조정한다. 방향(오르면 점수 ↑/↓)은 config.SCORE_DIRECTION 으로
고정되고, 중요도는 절대값 합이 1이 되도록 정규화해 저장한다.
"""
import json
import math
from datetime import datetime, timezone

import config

SAT_MIN, SAT_MAX = 0.1, 20.0  # 포화 변동률(%) 허용 범위


class WeightError(ValueError):
    pass


def _row(r):
    return dict(id=r["id"], created_at=r["created_at"], label=r["label"],
                weights=json.loads(r["weights"]), saturation=json.loads(r["saturation"]))


def get_active(conn):
    """현재 적용 버전(가장 큰 id)."""
    r = conn.execute("SELECT * FROM weight_sets ORDER BY id DESC LIMIT 1").fetchone()
    return _row(r)


def get(conn, set_id):
    r = conn.execute("SELECT * FROM weight_sets WHERE id=?", (set_id,)).fetchone()
    return _row(r) if r else None


def list_all(conn):
    return [_row(r) for r in conn.execute("SELECT * FROM weight_sets ORDER BY id DESC")]


def _number(raw, name):
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise WeightError(f"{name}: 숫자가 아닙니다")
    if not math.isfinite(v):
        raise WeightError(f"{name}: 유효한 숫자가 아닙니다")
    return v


def validate(importance, saturation):
    """importance/saturation: {key: 입력값}. 반환: (정규화된 부호 가중치, 포화 변동률). 잘못되면 WeightError."""
    keys = set(config.SCORE_DIRECTION)
    if set(importance) != keys or set(saturation) != keys:
        raise WeightError("지표 목록이 올바르지 않습니다")
    imp = {k: _number(importance[k], f"{k} 중요도") for k in keys}
    sat = {k: _number(saturation[k], f"{k} 포화 변동률") for k in keys}
    for k, v in imp.items():
        if not 0 <= v <= 100:
            raise WeightError(f"{k} 중요도는 0~100 이어야 합니다")
    for k, v in sat.items():
        if not SAT_MIN <= v <= SAT_MAX:
            raise WeightError(f"{k} 포화 변동률은 {SAT_MIN}~{SAT_MAX}% 이어야 합니다")
    total = sum(imp.values())
    if total <= 0:
        raise WeightError("중요도가 모두 0입니다. 하나 이상은 0보다 커야 합니다")
    weights = {k: round(config.SCORE_DIRECTION[k] * imp[k] / total, 6) for k in config.SCORE_DIRECTION}
    return weights, {k: round(v, 4) for k, v in sat.items()}


def create_version(conn, importance, saturation, note=""):
    weights, sat = validate(importance, saturation)
    nxt = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM weight_sets").fetchone()[0]
    label = f"v{nxt}" + (f" {note.strip()[:40]}" if note and note.strip() else "")
    cur = conn.execute(
        "INSERT INTO weight_sets (created_at,label,weights,saturation) VALUES (?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), label,
         json.dumps(weights), json.dumps(sat)))
    return cur.lastrowid
