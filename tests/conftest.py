import pytest

import config
import performance
from collectors import ai_tracker, macro, news, reports


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """모든 테스트: 임시 DB, 스케줄러 off, 실제 LLM/네트워크 호출 금지."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(config, "LLM_PROVIDER", "none-in-tests")

    def no_network():
        raise RuntimeError("no network in tests")
    monkeypatch.setattr(performance, "kospi_closes", no_network)

    def no_symbol_network(sym):
        raise RuntimeError("no network in tests")
    monkeypatch.setattr(reports, "yf_fetch", no_symbol_network)
    monkeypatch.setattr(config, "REPORTS_DELAY_SEC", 0)

    def no_capex(ticker):
        raise RuntimeError("no network in tests")
    monkeypatch.setattr(ai_tracker, "yf_capex", no_capex)

    def no_http(url):
        raise RuntimeError("no network in tests")
    monkeypatch.setattr(news, "http_fetch", no_http)                 # 종목 헤드라인 등 기본 조회 차단
    import alerts

    def no_push(sub, payload, pem):
        raise RuntimeError("no network in tests")
    monkeypatch.setattr(alerts, "send_push", no_push)                # 실제 푸시 발송 차단
    import screener

    def no_screen(*a, **k):
        raise RuntimeError("no network in tests")
    for fn in ("yf_chunk", "yf_index", "yf_mcap"):
        monkeypatch.setattr(screener, fn, no_screen)                 # 스크리너 네트워크 차단

    def no_ohlcv(symbol, period=None):
        raise RuntimeError("no network in tests")
    monkeypatch.setattr(macro, "yf_ohlcv", no_ohlcv)                 # 일봉 조회 차단 (지지·저항은 테스트에서 직접 주입)
    monkeypatch.setattr(reports, "yf_industry", lambda sym: None)    # Yahoo 업종 조회 차단
    monkeypatch.setattr(macro, "yf_closes", lambda symbol, period="2mo": flat_closes(start=20.0 if symbol == "^VIX" else 5000.0))


def make_rss(items=None):
    """items: [(title, link, hours_ago 또는 None(발행일 없음))]. 기본은 최근 기사 2건."""
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    items = items if items is not None else [
        ("  연준   금리 동결 ", "https://example.com/a", 2), ("bad link item", "javascript:alert(1)", 5)]
    now = datetime.now(timezone.utc)
    body = ""
    for title, link, hours in items:
        pub = f"<pubDate>{format_datetime(now - timedelta(hours=hours))}</pubDate>" if hours is not None else ""
        src = '<source url="https://x.com">매경</source>' if link.startswith("https") else ""
        body += f"<item><title>{title}</title><link>{link}</link>{src}{pub}</item>"
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'.encode("utf-8")


def fake_rss(url):
    return make_rss()


def macro_ok(symbol):
    return 110.0, 100.0  # +10%


class FakeProvider:
    name = "fake"

    def __init__(self, payload=None, exc=None):
        self.payload, self.exc, self.calls = payload, exc, 0

    def complete(self, prompt, system=None, schema=None):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.payload


GOOD = {"summary_lines": ["a", "b", "c", "d"], "adjustment": 4, "risk_comment": "리스크"}


def consensus(price=100.0, target=130.0, rec=1.8, n=12, **over):
    """reports.collect_one 용 가짜 fetcher 결과."""
    d = dict(price=price, target_mean=target, target_high=target * 1.3, target_low=target * 0.8,
             target_median=target, rec_mean=rec, rec_key="buy", n_analysts=n,
             strong_buy=4, buy=6, hold=2, sell=0, strong_sell=0)
    d.update(over)
    return d


def flat_closes(n=30, start=100.0, step=0.0):
    """추세 지표용 합성 종가: 기본은 평탄(KOSPI 변화 0, 낙폭 0, VIX 평균=종가)."""
    return [start + step * i for i in range(n)]


def history_down(symbol, period="2mo"):
    raise RuntimeError("history down")
