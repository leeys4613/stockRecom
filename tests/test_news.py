import config
import db
from collectors import news
from tests.conftest import fake_rss


def test_collect_parses_and_sanitizes():
    f = news.collect_one("us_macro", fake_rss)
    assert f["status"] == "ok" and len(f["items"]) == 2
    assert f["items"][0]["title"] == "연준 금리 동결"  # 공백 정리
    assert f["items"][0]["link"] == "https://example.com/a"
    assert f["items"][0]["source"] == "매경"
    assert f["items"][1]["link"] == ""  # javascript: 링크 제거


def test_collect_failure_is_recorded_not_raised():
    def boom(url):
        raise TimeoutError("timeout")
    f = news.collect_one("bitcoin", boom)
    assert f["status"] == "failed" and "timeout" in f["error"] and f["items"] == []


def test_empty_feed_is_failure():
    f = news.collect_one("korea", lambda u: b'<rss version="2.0"><channel><title>t</title></channel></rss>')
    assert f["status"] == "failed"


def test_save_and_latest_keeps_last_success_on_failure():
    db.init_db()
    with db.session() as conn:
        news.save(conn, [news.collect_one("japan", fake_rss)])
        news.save(conn, [news.collect_one("japan", lambda u: (_ for _ in ()).throw(OSError("x")))])
        rows = {r["topic"]: r for r in news.latest(conn)}
    assert rows["japan"]["status"] == "failed"
    assert len(rows["japan"]["headlines"]) == 2 and rows["japan"]["ok_at"]
    assert rows["bitcoin"]["status"] == "none"
    assert len(rows) == len(config.NEWS_TOPICS)
