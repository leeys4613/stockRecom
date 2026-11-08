"""구간별 주식 비중 규칙 관리. 불변 버전이며 저장하면 새 버전이 생기고 이후 분석부터 적용된다.

사용자는 구간 경계(점수 하한)와 구간별 주식 최소/최대 비중을 조정한다. 구간 개수와 라벨은 고정이다.
안전 규칙: 주식 최대는 config.EQUITY_CAP_PCT 를 넘을 수 없고, 점수가 낮은 구간일수록 비중이 늘어날 수 없다.
"""
import json
import math
from datetime import datetime, timezone

import config

LABELS = [b[1] for b in config.ALLOCATION_BANDS]  # 라벨/개수는 고정
N = len(LABELS)


class AllocationError(ValueError):
    pass


def _row(r):
    bands = json.loads(r["bands"])
    for b, name in zip(bands, LABELS):  # 구간 이름은 고정: 예전에 저장된 이름이 달라도 현재 이름으로 표시
        b[1] = name
    return dict(id=r["id"], created_at=r["created_at"], label=r["label"], bands=bands)


def get_active(conn):
    return _row(conn.execute("SELECT * FROM allocation_sets ORDER BY id DESC LIMIT 1").fetchone())


def get(conn, set_id):
    r = conn.execute("SELECT * FROM allocation_sets WHERE id=?", (set_id,)).fetchone()
    return _row(r) if r else None


def list_all(conn):
    return [_row(r) for r in conn.execute("SELECT * FROM allocation_sets ORDER BY id DESC")]


def _number(raw, name):
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise AllocationError(f"{name}: 숫자가 아닙니다")
    if not math.isfinite(v):
        raise AllocationError(f"{name}: 유효한 숫자가 아닙니다")
    return v


def validate(floors, emins, emaxs):
    """floors: 마지막 구간을 뺀 N-1개 점수 하한, emins/emaxs: N개. 반환: bands 리스트. 잘못되면 AllocationError."""
    if len(floors) != N - 1 or len(emins) != N or len(emaxs) != N:
        raise AllocationError("구간 입력 개수가 올바르지 않습니다")
    fl = [_number(v, f"{LABELS[i]} 점수 하한") for i, v in enumerate(floors)]
    lo = [_number(v, f"{LABELS[i]} 주식 최소") for i, v in enumerate(emins)]
    hi = [_number(v, f"{LABELS[i]} 주식 최대") for i, v in enumerate(emaxs)]
    for i, v in enumerate(fl):
        if v != int(v) or not 1 <= v <= 100:
            raise AllocationError(f"{LABELS[i]} 점수 하한은 1~100 사이 정수여야 합니다")
    fl = [int(v) for v in fl]
    if any(a <= b for a, b in zip(fl, fl[1:])):
        raise AllocationError("점수 하한은 위 구간일수록 커야 합니다 (경계가 겹치거나 뒤집힘)")
    cap = config.EQUITY_CAP_PCT
    for i in range(N):
        if not 0 <= lo[i] <= hi[i] <= cap:
            raise AllocationError(
                f"{LABELS[i]}: 0 ≤ 최소 ≤ 최대 ≤ {cap}(주식 상한) 이어야 합니다")
    for i in range(N - 1):
        if lo[i] < lo[i + 1] or hi[i] < hi[i + 1]:
            raise AllocationError(
                f"{LABELS[i + 1]} 구간의 비중이 {LABELS[i]}보다 클 수 없습니다 (점수가 낮을수록 주식 비중은 줄어야 함)")
    floors_full = fl + [0]
    return [[floors_full[i], LABELS[i], _clean(lo[i]), _clean(hi[i])] for i in range(N)]


def _clean(v):
    return int(v) if v == int(v) else round(v, 2)


def create_version(conn, floors, emins, emaxs, note=""):
    bands = validate(floors, emins, emaxs)
    nxt = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM allocation_sets").fetchone()[0]
    label = f"v{nxt}" + (f" {note.strip()[:40]}" if note and note.strip() else "")
    cur = conn.execute(
        "INSERT INTO allocation_sets (created_at,label,bands) VALUES (?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), label,
         json.dumps(bands, ensure_ascii=False)))
    return cur.lastrowid
