"""매크로 지표 차트 데이터. 지표를 눌렀을 때만 Yahoo 일봉(1년)을 받아 price_history 에 저장해 두고(12시간 캐시) 기간별로 잘라 돌려준다.

직접 지표(SOX, 환율 등)는 종가 그대로, 파생 지표(KOSPI 10거래일 변화, 10일 고점 대비, VIX 10일 평균)는 앱이 점수에 쓰는 것과 같은 식으로 계산한 값의 추이를 돌려준다.
AI·메모리·전력 바스켓 지표는 단일 가격 계열이 없어 차트를 제공하지 않는다.
"""
import threading
from datetime import datetime, timedelta, timezone

import config

CACHE_HOURS = 12
# 간격별 선택 가능한 기간: 분봉=최근 N 거래일(5분봉), 일봉=최근 N일, 주봉=최근 N일(달력일)
INTERVALS = {"5m": (1, 5), "1d": (30, 90, 365), "1wk": (365, 1095, 1825)}
RANGES = INTERVALS["1d"]
MEM_TTL = {"5m": timedelta(minutes=3), "1wk": timedelta(hours=6)}
_mem = {}
TREND = config.TREND_DAYS
_lock = threading.Lock()


class ChartError(ValueError):
    pass


def _kospi_change(rows):
    c = [r[1] for r in rows]
    return [(rows[i][0], (c[i] / c[i - TREND] - 1) * 100) for i in range(TREND, len(rows))]


def _kospi_dd(rows):
    c = [r[1] for r in rows]
    return [(rows[i][0], (c[i] / max(c[i - TREND:i + 1]) - 1) * 100) for i in range(TREND, len(rows))]


def _vix_avg(rows):
    c = [r[1] for r in rows]
    return [(rows[i][0], sum(c[i - TREND + 1:i + 1]) / TREND) for i in range(TREND - 1, len(rows))]


DERIVED = {  # key -> (원천 심볼, 변환, 단위, 설명)
    "KOSPI10D": (config.KOSPI_SYMBOL, _kospi_change, "%", "KOSPI 10거래일 변화율(%)의 추이"),
    "KOSPIDD": (config.KOSPI_SYMBOL, _kospi_dd, "%", "KOSPI 10일 고점 대비 낙폭(%)의 추이"),
    "VIXAVG": (config.MACRO_INDICATORS["VIX"][1], _vix_avg, "", "VIX 10일 평균의 추이"),
}


def spec(key):
    """key -> dict(symbol, transform, unit, note) 또는 None(차트 없음)."""
    if key in DERIVED:
        sym, fn, unit, note = DERIVED[key]
        return dict(symbol=sym, transform=fn, unit=unit, note=note)
    ind = config.MACRO_INDICATORS.get(key)
    if ind and ind[1]:
        return dict(symbol=ind[1], transform=None, unit="", note=None)
    return None


def chartable(key):
    return spec(key) is not None


def yf_series(symbol):
    import yfinance as yf

    s = yf.Ticker(symbol).history(period="1y", interval="1d")["Close"].dropna()
    return [(d.strftime("%Y-%m-%d"), float(c)) for d, c in zip(s.index, s)]


def _load(conn, symbol):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=430)).strftime("%Y-%m-%d")
    return [(r["date"], r["close"]) for r in conn.execute(
        "SELECT date, close FROM price_history WHERE symbol=? AND date>=? ORDER BY date", (symbol, cutoff))]


def _ensure(conn_factory, symbol, fetcher):
    """캐시가 신선하면 그대로, 아니면 새로 받는다. 받기 실패 시 (캐시 행 목록, 오류 문자열)."""
    with conn_factory() as conn:
        f = conn.execute("SELECT fetched_at FROM chart_fetch WHERE symbol=?", (symbol,)).fetchone()
        fresh = f and datetime.fromisoformat(f["fetched_at"]) > datetime.now(timezone.utc) - timedelta(hours=CACHE_HOURS)
        rows = _load(conn, symbol)
    if fresh and rows:
        return rows, None
    try:
        got = (fetcher or yf_series)(symbol)
        if not got:
            raise ValueError("데이터 없음")
        with conn_factory() as conn:
            conn.executemany("INSERT OR REPLACE INTO price_history (symbol,date,close) VALUES (?,?,?)", [(symbol, d, c) for d, c in got])
            conn.execute("INSERT OR REPLACE INTO chart_fetch (symbol,fetched_at) VALUES (?,?)",
                         (symbol, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            rows = _load(conn, symbol)
        return rows, None
    except Exception as e:
        return rows, f"{type(e).__name__}: {e}"[:160]


def yf_bars(symbol, interval):
    """분봉(5m)·주봉(1wk). 분봉 라벨은 한국 시간 'MM-DD HH:MM', 주봉은 'YYYY-MM-DD'."""
    import yfinance as yf
    from zoneinfo import ZoneInfo

    period, fmt = ("5d", "%m-%d %H:%M") if interval == "5m" else ("5y", "%Y-%m-%d")
    s = yf.Ticker(symbol).history(period=period, interval=interval)["Close"].dropna()
    tz = ZoneInfo(config.TIMEZONE)
    return [((d.astimezone(tz) if interval == "5m" else d).strftime(fmt), float(c)) for d, c in zip(s.index, s)]


def _alt_rows(symbol, interval, fetcher):
    """분봉/주봉은 DB 에 쌓지 않고 짧게 메모리에 캐시한다. 반환 (행, 오류 문자열 또는 None)."""
    k = (symbol, interval)
    now = datetime.now(timezone.utc)
    hit = _mem.get(k)
    if hit and now - hit[0] < MEM_TTL[interval]:
        return hit[1], None
    try:
        rows = (fetcher or yf_bars)(symbol, interval)
        if not rows:
            raise ValueError("데이터 없음")
        _mem[k] = (now, rows)
        return rows, None
    except Exception as e:
        return (hit[1] if hit else []), f"{type(e).__name__}: {e}"[:160]


def _points(conn_factory, symbol, transform, days, interval, fetcher):
    """한 심볼의 (라벨, 값) 목록과 오류 문자열. 일봉은 DB 캐시, 분봉/주봉은 메모리 캐시."""
    if interval == "1d":
        with _lock:      # 같은 심볼을 동시에 여러 번 눌러도 한 번만 받는다
            rows, err = _ensure(conn_factory, symbol, fetcher)
        if len(rows) < TREND + 2:
            raise ChartError(err or "차트를 그릴 데이터가 부족합니다")
        pts = transform(rows) if transform else rows
        cut = (datetime.strptime(pts[-1][0], "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
        return [p for p in pts if p[0] >= cut], err
    with _lock:
        rows, err = _alt_rows(symbol, interval, fetcher)
    if len(rows) < 3:
        raise ChartError(err or "차트를 그릴 데이터가 부족합니다")
    if interval == "5m":     # 최근 days 거래일(라벨 앞 'MM-DD'가 다른 날 수)만
        keep = set(list(dict.fromkeys(p[0][:5] for p in rows))[-days:])
        return [p for p in rows if p[0][:5] in keep], err
    cut = (datetime.strptime(rows[-1][0], "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
    return [p for p in rows if p[0] >= cut], err


def _check(interval, days):
    if interval not in INTERVALS:
        raise ChartError("간격은 분봉(5m)·일봉(1d)·주봉(1wk) 중 하나입니다")
    if days not in INTERVALS[interval]:
        raise ChartError("선택할 수 없는 기간입니다")


def _pack(pts, name, unit, note, interval, days, err, **extra):
    vals = [p[1] for p in pts]
    first, last = vals[0], vals[-1]
    return dict(name=name, unit=unit, note=note, interval=interval, days=days, points=[[d, round(v, 4)] for d, v in pts],
                first=first, last=last, high=max(vals), low=min(vals),
                chg=None if unit == "%" or first == 0 else (last / first - 1) * 100,   # 비율 지표는 변화율 대신 값 차이를 본다
                diff=last - first, stale=err is not None, error=err, as_of=pts[-1][0], **extra)


def series(conn_factory, key, days=90, interval="1d", fetcher=None):
    """매크로 지표 차트 데이터. interval: 5m(분봉) / 1d(일봉) / 1wk(주봉). 파생 지표는 일봉만.

    fetcher 는 일봉이면 f(symbol), 그 외엔 f(symbol, interval).
    """
    sp = spec(key)
    if sp is None:
        raise ChartError("이 지표는 차트가 없습니다(여러 종목을 묶은 지표)")
    _check(interval, days)
    if interval != "1d" and sp["transform"]:
        raise ChartError("이 지표는 일봉만 제공합니다(10거래일 기준 지표)")
    pts, err = _points(conn_factory, sp["symbol"], sp["transform"], days, interval, fetcher)
    return _pack(pts, config.MACRO_INDICATORS[key][0], sp["unit"], sp["note"], interval, days, err, key=key)


def stock_series(conn_factory, code, days=90, interval="1d", compare=False, levels=False, fetcher=None):
    """종목 차트 데이터. 대상은 종목 목록(stock_master)에 있는 종목만. compare=True 면 소속 지수를 시작점(첫 값)에 맞춰 겹쳐 그릴 수 있게 함께 돌려준다.

    levels=True 면(일봉만) 관심종목의 최근 지지·저항 계산값을 함께 돌려준다.
    """
    _check(interval, days)
    with conn_factory() as conn:
        m = conn.execute("SELECT code,name,market FROM stock_master WHERE code=?", ((code or "").strip().upper(),)).fetchone()
        lv = None
        if levels and interval == "1d" and m:
            r = conn.execute("SELECT metrics FROM stock_views WHERE code=? AND status='ok' ORDER BY id DESC LIMIT 1", (m["code"],)).fetchone()
            if r and r["metrics"]:
                import json
                got = (json.loads(r["metrics"]) or {}).get("levels")
                if got:
                    lv = dict(supports=[x["price"] for x in got.get("supports", [])], resistances=[x["price"] for x in got.get("resistances", [])])
    if not m:
        raise ChartError("종목 목록에 없는 종목입니다")
    sym = f"{m['code']}.{m['market']}"
    pts, err = _points(conn_factory, sym, None, days, interval, fetcher)
    extra = dict(code=m["code"], market=m["market"], levels=lv)
    if compare:
        idx_sym = config.SCREEN_INDEX[m["market"]]
        try:
            ipts, _ = _points(conn_factory, idx_sym, None, days, interval, fetcher)
            imap = dict(ipts)
            common = [(d, v) for d, v in pts if d in imap]
            if len(common) >= 3 and imap[common[0][0]]:
                base_s, base_i = common[0][1], imap[common[0][0]]
                extra["index"] = dict(
                    name="KOSPI" if m["market"] == "KS" else "KOSDAQ",
                    points=[[d, round(imap[d] / base_i * base_s, 4)] for d, _ in common],   # 시작점을 종목 가격에 맞춰 겹쳐 보게 한다
                    chg=(imap[common[-1][0]] / base_i - 1) * 100, stock_chg=(common[-1][1] / base_s - 1) * 100)
        except Exception:
            pass       # 지수 선을 못 그려도 종목 차트는 보여준다
    return _pack(pts, m["name"], "", None, interval, days, err, **extra)
