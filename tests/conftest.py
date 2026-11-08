import pytest

import config
import performance
from collectors import reports


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


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>  \xec\x97\xb0\xec\xa4\x80   \xea\xb8\x88\xeb\xa6\xac \xeb\x8f\x99\xea\xb2\xb0 </title>
<link>https://example.com/a</link><source url="https://x.com">\xeb\xa7\xa4\xea\xb2\xbd</source>
<pubDate>Mon, 02 Nov 2026 00:00:00 GMT</pubDate></item>
<item><title>bad link item</title><link>javascript:alert(1)</link></item>
</channel></rss>"""


def fake_rss(url):
    return RSS


def macro_ok(symbol):
    return 110.0, 100.0  # +10%


class FakeProvider:
    name = "fake"

    def __init__(self, payload=None, exc=None):
        self.payload, self.exc, self.calls = payload, exc, 0

    def complete(self, prompt):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.payload


GOOD = {"summary_3lines": ["a", "b", "c"], "adjustment": 4, "risk_comment": "리스크"}


def consensus(price=100.0, target=130.0, rec=1.8, n=12, **over):
    """reports.collect_one 용 가짜 fetcher 결과."""
    d = dict(price=price, target_mean=target, target_high=target * 1.3, target_low=target * 0.8,
             target_median=target, rec_mean=rec, rec_key="buy", n_analysts=n,
             strong_buy=4, buy=6, hold=2, sell=0, strong_sell=0)
    d.update(over)
    return d
