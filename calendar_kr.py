"""한국 거래일 판별: 주말 + 공휴일(대체공휴일 포함) + KRX 휴장일(5/1, 12/31)."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import holidays

import config

_KR_HOLIDAYS = holidays.country_holidays("KR")


def today_kst():
    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


def last_completed_trading_day(now=None):
    """지금 시점에 일봉이 확정됐어야 하는 가장 최근 거래일. 장 마감 후(15:40 이후)의 거래일이면 오늘, 아니면 오늘 이전의 마지막 거래일."""
    from datetime import timedelta

    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    d = now.date()
    if not (is_trading_day(d) and (now.hour, now.minute) >= (15, 40)):
        d -= timedelta(days=1)
        while not is_trading_day(d):
            d -= timedelta(days=1)
    return d


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    if d in _KR_HOLIDAYS:
        return False
    if (d.month, d.day) in ((5, 1), (12, 31)):  # KRX 휴장
        return False
    return True
