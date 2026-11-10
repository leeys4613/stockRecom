import io

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import db
import main
import stockmaster as sm
from collectors import reports
from tests.conftest import consensus


def init_with(rows=None, source="upload"):
    db.init_db()
    if rows is not None:
        with db.session() as conn:
            conn.execute("DELETE FROM stock_master")  # 기본 내장 목록을 비우고 테스트 데이터만 사용
            sm._insert(conn, rows, source)


def names(conn, q, limit=8):
    return [r["name"] for r in sm.search(conn, q, limit)]


# ---------- 기본 함수 ----------
def test_choseong_and_norm():
    assert sm.choseong("삼성전자") == "ㅅㅅㅈㅈ"
    assert sm.choseong("SK 하이닉스") == "skㅎㅇㄴㅅ"
    assert sm.norm(" Sk 하이닉스 ") == "sk하이닉스"


def test_seed_loaded_once_and_survives_reinit():
    db.init_db()
    db.init_db()
    with db.session() as conn:
        i = sm.info(conn)
    assert i["count"] == 40 and i["source"] == "seed"


# ---------- 검색 ----------
def test_search_ranking_and_variants():
    db.init_db()
    with db.session() as conn:
        assert names(conn, "삼성전자")[0] == "삼성전자"
        assert names(conn, "005930") == ["삼성전자"]
        assert names(conn, "0059")[0] == "삼성전자"                      # 코드 접두
        assert names(conn, "ㅅㅅㅈㅈ") == ["삼성전자"]                    # 초성
        assert names(conn, "삼성 전자") == ["삼성전자"]                   # 공백 무시
        assert names(conn, "sk하이닉스") == ["SK하이닉스"]                # 대소문자·공백 무시
        assert names(conn, "hlb") == ["HLB"]
        assert set(names(conn, "삼성")) >= {"삼성전자", "삼성SDI", "삼성물산", "삼성생명", "삼성전기"}
        assert names(conn, "하이닉") == ["SK하이닉스"]                    # 부분 일치
        assert names(conn, "zzzz없음") == [] and names(conn, "") == [] and names(conn, "   ") == []


def test_search_exact_name_outranks_longer_names():
    init_with([dict(code="000001", name="SK", market="KS"), dict(code="000002", name="SK하이닉스", market="KS"),
               dict(code="000003", name="에스케이SK", market="KS")], "upload")
    with db.session() as conn:
        assert names(conn, "sk") == ["SK", "SK하이닉스", "에스케이SK"]   # 일치 > 접두 > 포함


def test_search_limit_and_result_shape():
    db.init_db()
    with db.session() as conn:
        r = sm.search(conn, "삼성", limit=2)
    assert len(r) == 2 and set(r[0]) == {"code", "name", "market"}


@pytest.mark.parametrize("q", ["%", "_", "\\", "'; DROP TABLE stock_master;--", "삼성%", "\x00"])
def test_search_special_characters_are_literal_and_safe(q):
    db.init_db()
    with db.session() as conn:
        assert sm.search(conn, q) == []              # 와일드카드로 해석되어 전체가 나오면 안 됨
        assert sm.info(conn)["count"] == 40          # 테이블 무사


# ---------- 업로드 파싱 ----------
def kind_rows(n=150):
    rows = [(f"테스트종목{i}", "유가증권" if i % 2 == 0 else "코스닥", f"{100000 + i:06d}", "업종") for i in range(n)]
    rows += [("코넥스종목", "코넥스", "900001", "업종")]
    return rows


def kind_html(rows, enc="cp949"):
    body = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td><td>{d}</td></tr>" for a, b, c, d in rows)
    html = f"<html><head><meta charset='{enc}'></head><body><table><thead><tr><th>회사명</th><th>시장구분</th><th>종목코드</th><th>업종</th></tr></thead><tbody>{body}</tbody></table></body></html>"
    return html.encode(enc)


def test_parse_kind_style_html_xls_cp949():
    rows = sm.parse_file("상장법인목록.xls", kind_html(kind_rows()))
    by = {r["code"]: r for r in rows}
    assert len(rows) == 150                                  # 코넥스 제외
    assert by["100000"]["market"] == "KS" and by["100001"]["market"] == "KQ"
    assert by["100000"]["name"] == "테스트종목0"


def test_parse_numeric_codes_get_zero_padded():
    rows = [("삼성전자", "유가증권", 5930, "x")] + kind_rows(120)
    out = sm.parse_file("a.xls", kind_html(rows))
    assert any(r["code"] == "005930" and r["name"] == "삼성전자" for r in out)


def test_parse_csv_utf8_bom_and_cp949_and_alt_headers():
    df = pd.DataFrame({"단축코드": [f"{200000 + i:06d}" for i in range(130)],
                       "한글 종목약명": [f"종목{i}" for i in range(130)],
                       "시장구분": ["KOSPI" if i % 2 else "KOSDAQ" for i in range(130)]})
    for enc in ("utf-8-sig", "cp949"):
        raw = df.to_csv(index=False).encode(enc)
        out = sm.parse_file("list.csv", raw)
        assert len(out) == 130 and {r["market"] for r in out} == {"KS", "KQ"}


def test_parse_xlsx():
    df = pd.DataFrame(kind_rows(140), columns=["회사명", "시장구분", "종목코드", "업종"])
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    out = sm.parse_file("상장법인목록.xlsx", buf.getvalue())
    assert len(out) == 140 and out[0]["code"].isalnum()


def test_parse_skips_bad_rows_and_duplicates():
    rows = kind_rows(120) + [("중복", "유가증권", "100000", "x"), ("", "유가증권", "777777", "x"),
                             ("코드이상", "유가증권", "ABC", "x"), ("시장없음", "ETF", "888888", "x")]
    out = sm.parse_file("a.xls", kind_html(rows))
    codes = [r["code"] for r in out]
    assert len(out) == 120 and codes.count("100000") == 1
    assert "777777" not in codes and "888888" not in codes


@pytest.mark.parametrize("name,raw,msg", [
    ("a.csv", "이름,값\n가,1\n".encode("utf-8"), "필요한 컬럼이 없습니다"),
    ("a.xlsx", b"PK\x03\x04garbage", "읽지 못했습니다"),
    ("a.csv", b"\xff\xfe\x00\x01\x80\x81", "인코딩|읽지 못했습니다|필요한 컬럼"),
    ("a.xls", b"<html><body>no table</body></html>", "읽지 못했습니다|표를 찾지"),
    ("a.csv", b"x" * (sm.MAX_UPLOAD_BYTES + 1), "너무 큽니다"),
])
def test_parse_rejects_bad_files(name, raw, msg):
    with pytest.raises(sm.StockMasterError, match=msg):
        sm.parse_file(name, raw)


def test_replace_all_requires_enough_rows_and_keeps_old_data_on_failure():
    db.init_db()
    with db.session() as conn:
        with pytest.raises(sm.StockMasterError, match="최소"):
            sm.replace_all(conn, [dict(code="000001", name="a", market="KS")] * 5)
        assert sm.info(conn)["count"] == 40          # 실패해도 기존 목록 유지
        n = sm.replace_all(conn, sm.parse_file("a.xls", kind_html(kind_rows())))
        assert n == 150 and sm.info(conn)["source"] == "upload"
        assert names(conn, "삼성전자") == []          # 업로드가 기본 목록을 대체
        assert names(conn, "테스트종목7")[0] == "테스트종목7"


# ---------- 통합 검색어 해석 ----------
def test_resolve_cases():
    db.init_db()
    with db.session() as conn:
        assert reports.resolve(conn, "삼성전자") == ("005930", "KS", "삼성전자")
        assert reports.resolve(conn, "005930") == ("005930", "KS", "삼성전자")
        assert reports.resolve(conn, "하이닉") == ("000660", "KS", "SK하이닉스")       # 유일 후보
        assert reports.resolve(conn, "에코프로비엠")[0] == "247540"
        assert reports.resolve(conn, "x", "005930", "KS", "삼성전자") == ("005930", "KS", "삼성전자")  # 자동완성 선택
        with pytest.raises(reports.WatchlistError, match="여러 종목"):
            reports.resolve(conn, "삼성")
        with pytest.raises(reports.WatchlistError, match="검색 결과가 없습니다"):
            reports.resolve(conn, "없는종목이름")
        with pytest.raises(reports.WatchlistError, match="입력하세요"):
            reports.resolve(conn, "  ")
        assert reports.resolve(conn, "123456") == ("123456", None, None)               # 목록에 없는 코드는 시장 미정


def test_ambiguous_message_lists_candidates():
    db.init_db()
    with db.session() as conn:
        with pytest.raises(reports.WatchlistError) as e:
            reports.resolve(conn, "삼성")
    assert "삼성전자(005930)" in str(e.value) and "삼성SDI(006400)" in str(e.value)


def test_add_stock_without_market_tries_kospi_then_kosdaq():
    db.init_db()
    seen = []

    def fetch(sym):
        seen.append(sym)
        if sym.endswith(".KS"):
            raise ValueError("조회 결과 없음")
        return consensus()
    with db.session() as conn:
        reports.add_stock(conn, "123456", None, "신규", fetch)
        assert [w["market"] for w in reports.watchlist(conn)] == ["KQ"]
    assert seen == ["123456.KS", "123456.KQ"]


def test_add_stock_without_market_fails_if_both_fail():
    db.init_db()
    with db.session() as conn:
        with pytest.raises(reports.WatchlistError, match="조회 실패"):
            reports.add_stock(conn, "123456", None, "신규", lambda s: (_ for _ in ()).throw(ValueError("x")))
        assert reports.watchlist(conn) == []


# ---------- 화면/라우트 ----------
def client():
    return TestClient(main.app)


def test_search_endpoint_json_shape_and_headers():
    with client() as c:
        r = c.get("/watchlist/search", params={"q": "삼성"})
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        data = r.json()
        assert 1 <= len(data) <= 8 and {"code", "name", "market", "market_name"} <= set(data[0])
        assert c.get("/watchlist/search", params={"q": ""}).json() == []
        assert c.get("/watchlist/search").json() == []
        assert c.get("/watchlist/search", params={"q": "ㅅㅅㅈㅈ"}).json()[0]["code"] == "005930"
        assert c.get("/watchlist/search", params={"q": "가" * 500}).status_code == 200   # 긴 입력도 안전


def test_add_by_single_query_name_code_and_choseong(monkeypatch):
    monkeypatch.setattr(reports, "yf_fetch", lambda sym: consensus())
    with client() as c:
        for q, code in (("삼성전자", "005930"), ("000660", "000660"), ("ㅋㅋㅇ게임", None), ("에코프로비엠", "247540")):
            r = c.post("/watchlist/add", data={"q": q}, follow_redirects=True)
            if code:
                assert r.status_code == 200 and f"{code} 추가 완료" in r.text, q
        with db.session() as conn:
            got = {w["code"]: (w["name"], w["market"]) for w in reports.watchlist(conn)}
        assert got["005930"] == ("삼성전자", "KS") and got["000660"] == ("SK하이닉스", "KS")
        assert got["247540"] == ("에코프로비엠", "KQ")


def test_add_ambiguous_or_unknown_query_returns_400_with_guidance(monkeypatch):
    monkeypatch.setattr(reports, "yf_fetch", lambda sym: consensus())
    with client() as c:
        r = c.post("/watchlist/add", data={"q": "삼성"})
        assert r.status_code == 400 and "여러 종목" in r.text and "삼성전자(005930)" in r.text
        r = c.post("/watchlist/add", data={"q": "없는종목이름"})
        assert r.status_code == 400 and "검색 결과가 없습니다" in r.text
        assert c.post("/watchlist/add", data={"q": ""}).status_code == 400
        with db.session() as conn:
            assert reports.watchlist(conn) == []


def test_add_selected_suggestion_posts_code_market_name(monkeypatch):
    monkeypatch.setattr(reports, "yf_fetch", lambda sym: consensus())
    with client() as c:
        r = c.post("/watchlist/add", data={"q": "삼성전자 (005930)", "code": "005930", "market": "KS", "name": "삼성전자"},
                   follow_redirects=True)
        assert "005930 추가 완료" in r.text


def test_unknown_code_falls_back_to_market_probe(monkeypatch):
    seen = []

    def fetch(sym):
        seen.append(sym)
        if sym.endswith(".KS"):
            raise ValueError("조회 결과 없음")
        return consensus()
    monkeypatch.setattr(reports, "yf_fetch", fetch)
    with client() as c:
        r = c.post("/watchlist/add", data={"q": "123456"}, follow_redirects=True)
        assert r.status_code == 200 and "123456 추가 완료" in r.text
    assert seen == ["123456.KS", "123456.KQ"]


def test_watchlist_page_has_autocomplete_and_master_info():
    with client() as c:
        html = c.get("/watchlist").text
    assert 'id="q"' in html and 'role="combobox"' in html and "/watchlist/search?q=" in html
    assert "종목 목록 40개 (기본 내장)" in html and 'type="file"' in html and "자동으로 접속하지 않습니다" in html
    assert "textContent" in html and "innerHTML = ''" in html and "innerHTML =" not in html.replace("innerHTML = ''", "")  # 응답은 textContent 로만 삽입


def test_upload_route_success_and_errors():
    with client() as c:
        good = kind_html(kind_rows())
        r = c.post("/watchlist/master", files={"file": ("상장법인목록.xls", good)}, follow_redirects=True)
        assert r.status_code == 200 and "150개로 갱신했습니다" in r.text and "종목 목록 150개 (업로드)" in r.text
        assert c.get("/watchlist/search", params={"q": "테스트종목9"}).json()[0]["code"] == "100009"

        r = c.post("/watchlist/master", files={"file": ("bad.csv", "a,b\n1,2\n".encode())})
        assert r.status_code == 400 and "업로드 실패" in r.text and "필요한 컬럼" in r.text
        r = c.post("/watchlist/master", files={"file": ("few.xls", kind_html(kind_rows(5)))})
        assert r.status_code == 400 and "최소" in r.text
        r = c.post("/watchlist/master", data={"file": "not a file"})
        assert r.status_code == 400 and "파일을 선택" in r.text
        assert "종목 목록 150개" in c.get("/watchlist").text          # 실패해도 직전 목록 유지


def test_service_worker_bypasses_search_endpoint():
    with client() as c:
        sw = c.get("/sw.js").text
    assert "/watchlist/search" in sw and "kms-v4" in sw
