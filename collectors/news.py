"""뉴스 수집기 (Google News RSS). 주제별로 ok/failed + fetched_at 을 기록한다.

헤드라인은 외부의 신뢰할 수 없는 텍스트다: 길이를 제한하고 링크는 http(s)만 허용한다.
"""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import config

RSS_URL = "https://news.google.com/rss/search?q={q}&{locale}"
DEFAULT_LOCALE = "hl=ko&gl=KR&ceid=KR:ko"
MAX_TITLE = 200


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def http_fetch(url):
    import httpx

    r = httpx.get(url, timeout=config.NEWS_TIMEOUT_SEC, follow_redirects=True,
                  headers={"User-Agent": "K-MacroSight/0.1"})
    r.raise_for_status()
    return r.content


def _published_dt(entry):
    raw = entry.get("published")
    try:
        d = parsedate_to_datetime(raw) if raw else None
    except (TypeError, ValueError):
        return None
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def parse_items(content, limit, max_age_days=None, now=None):
    """RSS -> 헤드라인. max_age_days 가 있으면 발행일이 그 기간 안인 기사만(발행일 없는 기사는 제외), 최신순.

    피드 자체가 비었으면 실패(예외), 기간 필터로 모두 걸러지면 빈 목록(정상: 최근 기사 없음).
    """
    import feedparser

    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"RSS 파싱 실패: {feed.bozo_exception}")
    if not feed.entries:
        raise ValueError("헤드라인 없음")
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days) if max_age_days else None
    items = []
    for e in feed.entries:
        title = " ".join((e.get("title") or "").split())[:MAX_TITLE]
        if not title:
            continue
        dt = _published_dt(e)
        if cutoff is not None and (dt is None or dt < cutoff or dt > now + timedelta(hours=6)):
            continue
        link = e.get("link") or ""
        if not link.startswith(("http://", "https://")):
            link = ""
        source = e.get("source", {}).get("title") if isinstance(e.get("source"), dict) else None
        items.append((dt or now, dict(title=title, link=link, source=source, published=e.get("published"))))
    items.sort(key=lambda t: t[0], reverse=True)
    return [i for _, i in items[:limit]]


def collect_one(topic, fetcher=http_fetch):
    _, query = config.NEWS_TOPICS[topic]
    max_age = config.NEWS_MAX_AGE_DAYS.get(topic, config.NEWS_DEFAULT_MAX_AGE_DAYS)
    limit = config.NEWS_PER_TOPIC_OVERRIDE.get(topic, config.NEWS_PER_TOPIC)
    fetched_at = now_iso()
    try:
        content = fetcher(RSS_URL.format(q=quote(f"{query} when:{max_age}d"),
                                          locale=config.NEWS_LOCALE.get(topic, DEFAULT_LOCALE)))
        items = parse_items(content, limit, max_age)
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


def stock_headlines(name, fetcher=None):
    """종목명으로 최근 뉴스 헤드라인(제목 목록). 실패하면 예외 — 호출자가 '기사 없음'으로 처리한다."""
    q = f'"{name}" 주가 OR 주식 OR 전망 OR 실적 when:{config.STOCK_NEWS_AGE_DAYS}d'
    content = (fetcher or http_fetch)(RSS_URL.format(q=quote(q), locale=DEFAULT_LOCALE))
    return [i["title"] for i in parse_items(content, config.STOCK_HEADLINES, config.STOCK_NEWS_AGE_DAYS)]
