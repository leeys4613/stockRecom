import config
import db
from collectors import news
from tests.conftest import fake_rss, make_rss


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


def test_old_and_undated_articles_are_dropped():
    rss = make_rss([("오늘 기사 헤드라인입니다", "https://e.com/1", 3),
                    ("어제 기사 헤드라인입니다", "https://e.com/2", 30),
                    ("2주 전 기사 헤드라인입니다", "https://e.com/3", 24 * 14),
                    ("발행일 없는 기사입니다", "https://e.com/4", None)])
    f = news.collect_one("us_macro", lambda u: rss)           # 기본 3일
    assert f["status"] == "ok"
    assert [i["title"] for i in f["items"]] == ["오늘 기사 헤드라인입니다", "어제 기사 헤드라인입니다"]


def test_policy_topic_windows_and_limits():
    rss = make_rss([(f"정책 기사 {i}", f"https://e.com/{i}", 24 * i) for i in range(1, 10)])   # 1~9일 전
    kr = news.collect_one("kr_policy", lambda u: rss)           # 7일 창, 최대 8건
    us = news.collect_one("us_policy", lambda u: rss)           # 3일 창
    assert len(kr["items"]) == 6 and kr["items"][0]["title"] == "정책 기사 1"   # 1~6일 전(7일 경계는 제외)
    assert len(us["items"]) == 2


def test_items_sorted_newest_first_and_limited():
    rss = make_rss([(f"기사 제목 번호 {i}", f"https://e.com/{i}", h) for i, h in enumerate([40, 1, 20, 3, 10, 5, 7])])
    f = news.collect_one("us_macro", lambda u: rss)             # 3일(72h) 창, 상한 5건
    assert [i["title"] for i in f["items"]] == ["기사 제목 번호 1", "기사 제목 번호 3", "기사 제목 번호 5",
                                              "기사 제목 번호 6", "기사 제목 번호 4"]


def test_all_stale_is_ok_with_no_items_but_empty_feed_is_failure():
    stale = make_rss([("오래된 기사 헤드라인입니다", "https://e.com/1", 24 * 30)])
    f = news.collect_one("us_macro", lambda u: stale)
    assert f["status"] == "ok" and f["items"] == []              # 최근 기사 없음 ≠ 수집 실패
    empty = news.collect_one("us_macro", lambda u: b'<rss version="2.0"><channel><title>t</title></channel></rss>')
    assert empty["status"] == "failed"


def test_query_has_when_operator_and_policy_topics_exist():
    seen = []
    news.collect_one("kr_policy", lambda u: seen.append(u) or make_rss())
    news.collect_one("us_macro", lambda u: seen.append(u) or make_rss())
    assert "when%3A7d" in seen[0] and "when%3A3d" in seen[1]
    assert set(config.POLICY_TOPICS.values()) <= set(config.NEWS_TOPICS)
