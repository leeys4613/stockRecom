"""관심종목 추천 점수 알림 (웹 푸시).

종목별로 알림 on/off 와 기준 점수를 둔다. 점수가 기준 이상으로 "올라설 때" 한 번만 보낸다
(계속 기준 이상이어도 반복 발송하지 않고, 기준보다 HYSTERESIS 이상 내려가면 다시 대기 상태가 된다).
VAPID 키는 서버가 처음 필요할 때 만들어 DB 에 저장한다 (저장소에 올라가지 않는다).
알림 내용은 종목명과 점수뿐이며 매수 권유 문구는 쓰지 않는다.
"""
import base64
import json
import logging

import config

log = logging.getLogger("alerts")
HYSTERESIS = 5.0
VAPID_SUB = "mailto:noreply@k-macrosight.local"


class AlertError(ValueError):
    pass


# ---- VAPID 키 ----
def _kv_get(conn, k):
    r = conn.execute("SELECT v FROM app_kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


def vapid_keys(conn):
    """(private_pem, public_b64url). 없으면 생성해 저장."""
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid

    pem = _kv_get(conn, "vapid_private")
    if pem:
        v = Vapid.from_pem(pem.encode())
    else:
        v = Vapid()
        v.generate_keys()
        pem = v.private_pem().decode()
        conn.execute("INSERT OR REPLACE INTO app_kv (k,v) VALUES ('vapid_private',?)", (pem,))
    pub = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return pem, base64.urlsafe_b64encode(pub).rstrip(b"=").decode()


# ---- 구독 ----
def subscribe(conn, sub):
    try:
        endpoint, keys = sub["endpoint"], sub["keys"]
        p256dh, auth = keys["p256dh"], keys["auth"]
    except (KeyError, TypeError):
        raise AlertError("구독 정보가 올바르지 않습니다")
    if not (isinstance(endpoint, str) and endpoint.startswith("https://") and len(endpoint) < 600
            and isinstance(p256dh, str) and isinstance(auth, str)):
        raise AlertError("구독 정보가 올바르지 않습니다")
    conn.execute("INSERT OR REPLACE INTO push_subscriptions (endpoint,p256dh,auth,created_at) "
                 "VALUES (?,?,?,datetime('now'))", (endpoint, p256dh, auth))


def unsubscribe(conn, endpoint):
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))


def subscription_count(conn):
    return conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0]


# ---- 종목별 설정 ----
def set_alert(conn, code, on, min_score):
    try:
        m = float(min_score)
    except (TypeError, ValueError):
        raise AlertError("기준 점수는 숫자여야 합니다")
    if not (m == m) or not 1 <= m <= 99:
        raise AlertError("기준 점수는 1~99 사이여야 합니다")
    cur = conn.execute("UPDATE watchlist SET alert_on=?, alert_min=?, alert_fired=0 WHERE code=?",
                       (1 if on else 0, round(m), (code or "").strip().upper()))
    if not cur.rowcount:
        raise AlertError("관심종목에 없는 종목입니다")


# ---- 발송 ----
def send_push(sub, payload, private_pem):
    """실제 발송(네트워크). 테스트에서는 교체한다. 구독이 만료됐으면 False."""
    from pywebpush import WebPushException, webpush

    try:
        webpush(subscription_info=sub, data=json.dumps(payload, ensure_ascii=False), vapid_private_key=private_pem,
                vapid_claims={"sub": VAPID_SUB}, ttl=6 * 3600)
        return True
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (404, 410):
            return False
        raise


def notify_all(conn, payload, sender=None):
    """모든 구독 기기로 발송. 만료된 구독은 지운다. 반환: 성공 수."""
    sender = sender or send_push
    pem, _ = vapid_keys(conn)
    ok = 0
    for r in conn.execute("SELECT * FROM push_subscriptions").fetchall():
        sub = dict(endpoint=r["endpoint"], keys=dict(p256dh=r["p256dh"], auth=r["auth"]))
        try:
            if sender(sub, payload, pem):
                ok += 1
            else:
                unsubscribe(conn, r["endpoint"])
        except Exception as e:
            log.warning("push failed: %s", type(e).__name__)
    return ok


def check_and_notify(conn, sender=None):
    """최신 AI 판정 점수가 기준 이상으로 올라선 종목에 알림. 반환: 알림을 낸 종목 코드 목록."""
    fired = []
    rows = conn.execute("SELECT code,name,alert_min,alert_fired FROM watchlist WHERE alert_on=1").fetchall()
    for w in rows:
        v = conn.execute("SELECT score FROM stock_views WHERE code=? AND status='ok' ORDER BY id DESC LIMIT 1",
                         (w["code"],)).fetchone()
        if v is None or v["score"] is None:
            continue
        score, thr = v["score"], w["alert_min"] or config.ALERT_DEFAULT_MIN
        if score >= thr and not w["alert_fired"]:
            payload = dict(title=f"{w['name']} 추천 점수 {score:.0f}",
                           body=f"기준 {thr:.0f}점 이상으로 올라섰습니다. 현재 분석 기준 적합도이며 매수 추천이 아닙니다.",
                           url="/watchlist", tag=f"watch-{w['code']}")
            if notify_all(conn, payload, sender):
                conn.execute("UPDATE watchlist SET alert_fired=1 WHERE code=?", (w["code"],))
                fired.append(w["code"])
        elif score < thr - HYSTERESIS and w["alert_fired"]:
            conn.execute("UPDATE watchlist SET alert_fired=0 WHERE code=?", (w["code"],))
    return fired
