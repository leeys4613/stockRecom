"""한국 거래일 판별: 주말 + 공휴일(대체공휴일 포함) + KRX 휴장일(5/1, 12/31)."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import holidays

import config

_KR_HOLIDAYS = holidays.country_holidays("KR")


def today_kst():
    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    if d in _KR_HOLIDAYS:
        return False
    if (d.month, d.day) in ((5, 1), (12, 31)):  # KRX 휴장
        return False
    return True
