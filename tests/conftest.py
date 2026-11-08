import pytest

import config
import performance


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """모든 테스트: 임시 DB, 스케줄러 off, 실제 LLM/네트워크 호출 금지."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(config, "LLM_PROVIDER", "none-in-tests")

    def no_network():
        raise RuntimeError("no network in tests")
    monkeypatch.setattr(performance, "kospi_closes", no_network)


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
