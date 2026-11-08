"""뉴스 수집기 (Google News RSS). 주제별로 ok/failed + fetched_at 을 기록한다.

헤드라인은 외부의 신뢰할 수 없는 텍스트다: 길이를 제한하고 링크는 http(s)만 허용한다.
"""
from datetime import datetime, timezone
from urllib.parse import quote

import config

RSS_URL = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
MAX_TITLE = 200


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def http_fetch(url):
    import httpx

    r = httpx.get(url, timeout=config.NEWS_TIMEOUT_SEC, follow_redirects=True,
                  headers={"User-Agent": "K-MacroSight/0.1"})
    r.raise_for_status()
    return r.content


def parse_items(content, limit):
    import feedparser

    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"RSS 파싱 실패: {feed.bozo_exception}")
    items = []
    for e in feed.entries[:limit]:
        title = " ".join((e.get("title") or "").split())[:MAX_TITLE]
        if not title:
            continue
        link = e.get("link") or ""
        if not link.startswith(("http://", "https://")):
            link = ""
        source = e.get("source", {}).get("title") if isinstance(e.get("source"), dict) else None
        items.append(dict(title=title, link=link, source=source,
                          published=e.get("published")))
    if not items:
        raise ValueError("헤드라인 없음")
    return items


def collect_one(topic, fetcher=http_fetch):
    _, query = config.NEWS_TOPICS[topic]
    fetched_at = now_iso()
    try:
        content = fetcher(RSS_URL.format(q=quote(query)))
        items = parse_items(content, config.NEWS_PER_TOPIC)
        return dict(topic=topic, status="ok", error=None, fetched_at=fetched_at, items=items)
    except Exception as e:
        return dict(topic=topic, status="failed", error=f"{type(e).__name__}: {e}"[:300],
                    fetched_at=fetched_at, items=[])


def collect_all(fetcher=http_fetch):
    return [collect_one(t, fetcher) for t in config.NEWS_TOPICS]


def save(conn, fetches):
    for f in fetches:
        cur = conn.execute(
            "INSERT INTO news_fetches (topic,status,error,fetched_at) VALUES (?,?,?,?)",
            (f["topic"], f["status"], f["error"], f["fetched_at"]))
        conn.executemany(
            "INSERT INTO news_items (fetch_id,title,link,source,published) VALUES (?,?,?,?,?)",
            [(cur.lastrowid, i["title"], i["link"], i["source"], i["published"])
             for i in f["items"]])


def latest(conn):
    """주제별 최신 시도 + 최신 성공 헤드라인. 실패해도 마지막 성공분은 stale 로 보여준다."""
    out = []
    for topic, (name, _) in config.NEWS_TOPICS.items():
        last = conn.execute(
            "SELECT * FROM news_fetches WHERE topic=? ORDER BY fetched_at DESC, id DESC LIMIT 1",
            (topic,)).fetchone()
        last_ok = conn.execute(
            "SELECT * FROM news_fetches WHERE topic=? AND status='ok' "
            "ORDER BY fetched_at DESC, id DESC LIMIT 1", (topic,)).fetchone()
        items = []
        if last_ok:
            items = [dict(r) for r in conn.execute(
                "SELECT title,link,source,published FROM news_items WHERE fetch_id=? ORDER BY id",
                (last_ok["id"],))]
        out.append(dict(
            topic=topic, name=name,
            status=last["status"] if last else "none",
            error=last["error"] if last else None,
            ok_at=last_ok["fetched_at"] if last_ok else None,
            headlines=items,
        ))
    return out
