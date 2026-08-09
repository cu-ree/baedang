#!/usr/bin/env python3
"""
step1_stocks.py — 종목 마스터 구축  (v4)

변경 이력
  v2  숫자만 남기는 파싱 -> 영숫자 코드(0001A0) 파손. 22건 오매칭
  v3  영숫자 코드 지원 + 이름 불일치 시 제외
      -> 현대차/KT&G/KCC 등 영문약어 21건이 오탐으로 제외됨
  v4  판정 로직 교체
        (a) 한글 음차를 영문으로 되돌려 비교  (케이티앤지 = KT&G)
        (b) 이름이 달라도, 그 이름이 corpCode 에서 '다른 종목코드'로
            등록되어 있을 때만 제외한다 (충돌의 실제 증거)

    python step1_stocks.py
"""

import io
import re
import ssl
import sys
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from common import Dart, db, log, section

SSL_CTX = ssl.create_default_context()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
      " (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

PREF_SUFFIX = re.compile(r"(\d*우[A-Z]?)$")
DROP_CORP = re.compile(
    r"(주식회사|㈜|\(주\)|corporation|corp\.?|co\.?,?\s*ltd\.?|inc\.?)", re.I)

# 한글 음차 -> 영문. 긴 것 우선.
ALPHA = [("더블유", "W"), ("에이치", "H"), ("에이", "A"), ("에스", "S"),
         ("에프", "F"), ("케이", "K"), ("제이", "J"), ("제트", "Z"),
         ("브이", "V"), ("아이", "I"), ("엑스", "X"), ("와이", "Y"),
         ("엘", "L"), ("엠", "M"), ("엔", "N"), ("디", "D"), ("티", "T"),
         ("피", "P"), ("비", "B"), ("씨", "C"), ("시", "C"), ("지", "G"),
         ("알", "R"), ("오", "O"), ("유", "U"), ("큐", "Q"), ("앤", "&"),
         ("이", "E")]

KIND_URL = ("https://kind.krx.co.kr/corpgeneral/corpList.do"
            "?method=download&searchType=13&marketType={}")
MARKETS = {"stockMkt": "KOSPI", "kosdaqMkt": "KOSDAQ"}


# ────────────────────────────────────────────────────────────
# 정규화
# ────────────────────────────────────────────────────────────

def norm_code(raw):
    """영문자를 제거하지 않는다. '0001A0' -> '0001A0'"""
    s = re.sub(r"[^0-9A-Za-z]", "", str(raw or "")).upper()
    if s.isdigit() and len(s) < 6:
        s = s.zfill(6)
    return s if len(s) == 6 else None


def _k2a(s):
    out, i = [], 0
    while i < len(s):
        for k, v in ALPHA:
            if s.startswith(k, i):
                out.append(v)
                i += len(k)
                break
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def canon(name):
    s = DROP_CORP.sub("", str(name or ""))
    s = re.sub(r"[\s\-\.,'’·()]", "", s)
    return _k2a(s).upper()


def name_match(a, b):
    a, b = canon(a), canon(b)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return (len(a) >= 3 and len(b) >= 3 and a[:3] == b[:3]
            and abs(len(a) - len(b)) <= 4)


# ────────────────────────────────────────────────────────────
# 상장 목록 소스
# ────────────────────────────────────────────────────────────

class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell, self._in = [], [], [], False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._in, self._cell = True, []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._in:
            self._row.append("".join(self._cell).strip())
            self._in = False
        elif tag == "tr" and self._row:
            self.rows.append(self._row)
            self._row = []

    def handle_data(self, data):
        if self._in:
            self._cell.append(data)


def http_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as r:
        raw = r.read()
    for enc in ("cp949", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def source_fdr():
    import FinanceDataReader as fdr
    df = fdr.StockListing("KRX")
    cols = {c.lower(): c for c in df.columns}
    c_code, c_name, c_mkt = cols.get("code"), cols.get("name"), cols.get("market")
    if not (c_code and c_name):
        raise RuntimeError(f"컬럼 인식 실패: {list(df.columns)}")
    out = {}
    for _, r in df.iterrows():
        code = norm_code(r[c_code])
        name = str(r[c_name]).strip()
        mkt = str(r[c_mkt]).strip() if c_mkt else ""
        if code and name and mkt in ("KOSPI", "KOSDAQ"):
            out[code] = {"name": name, "market": mkt}
    log(f"    FDR: {len(out):,}")
    if not out:
        raise RuntimeError("결과 없음")
    return out, "FinanceDataReader"


def source_kind():
    out, alnum = {}, 0
    for param, mkt in MARKETS.items():
        html = http_text(KIND_URL.format(param))
        p = TableParser()
        p.feed(html)
        if not p.rows:
            raise RuntimeError(f"{mkt}: 표 없음")
        header = p.rows[0]
        i_name = next(i for i, h in enumerate(header) if "회사명" in h)
        i_code = next(i for i, h in enumerate(header) if "종목코드" in h)
        n = 0
        for row in p.rows[1:]:
            if len(row) <= max(i_name, i_code):
                continue
            code = norm_code(row[i_code])
            name = row[i_name].strip()
            if not code or not name:
                continue
            if not code.isdigit():
                alnum += 1
            out[code] = {"name": name, "market": mkt}
            n += 1
        log(f"    {mkt:<7}: {n:,}")
    log(f"    영숫자 코드: {alnum:,}")
    if not out:
        raise RuntimeError("결과 없음")
    return out, "KIND"


def fetch_listed():
    section("1-2  현재 상장 종목 목록")
    for fn, label in ((source_fdr, "FinanceDataReader"), (source_kind, "KIND")):
        log(f"  [시도] {label}")
        try:
            data, used = fn()
            log(f"  [성공] {used} — {len(data):,}종목")
            return data, used
        except ImportError:
            log("    미설치. 건너뜀.")
        except Exception as e:
            log(f"    실패: {type(e).__name__}: {str(e)[:120]}")
    sys.exit("[중단] 모든 소스 실패.")


def fetch_corp_code(dart):
    section("1-1  corpCode.xml")
    body = dart.get("corpCode.xml", {}, raw=True)
    if not isinstance(body, bytes) or body[:2] != b"PK":
        sys.exit(f"[중단] ZIP 아님: {str(body)[:200]}")
    zf = zipfile.ZipFile(io.BytesIO(body))
    root = ET.fromstring(zf.read(zf.namelist()[0]))
    rows, total = [], 0
    for el in root.iter("list"):
        total += 1
        sc = norm_code(el.findtext("stock_code"))
        if sc:
            rows.append({
                "stock_code": sc,
                "corp_code": (el.findtext("corp_code") or "").strip(),
                "corp_name": (el.findtext("corp_name") or "").strip(),
            })
    log(f"  전체 법인      : {total:,}")
    log(f"  종목코드 보유  : {len(rows):,}  (폐지 포함)")
    return rows


# ────────────────────────────────────────────────────────────

def main():
    conn = db()
    dart = Dart(conn)

    corp_rows = fetch_corp_code(dart)
    listed, source = fetch_listed()

    by_code = {r["stock_code"]: r for r in corp_rows}

    # 정규화된 이름 -> 해당 이름으로 등록된 종목코드들
    name_index = {}
    for r in corp_rows:
        name_index.setdefault(canon(r["corp_name"]), set()).add(r["stock_code"])

    section("1-3  매핑 + 충돌 판정")

    records, matched = [], set()
    exact, alias, conflict = 0, [], []

    for code, info in listed.items():
        c = by_code.get(code)
        if not c:
            continue

        if name_match(info["name"], c["corp_name"]):
            exact += 1
        else:
            # 이름이 다르다 -> 이 이름이 다른 코드로 등록되어 있는가?
            others = name_index.get(canon(info["name"]), set())
            others = {o for o in others if o != code}
            if others:
                conflict.append((code, info["name"], c["corp_name"], sorted(others)))
                continue
            alias.append((code, info["name"], c["corp_name"]))

        records.append((code, c["corp_code"], info["name"],
                        info["market"], "보통주", None, "corpcode"))
        matched.add(code)

    log(f"  이름 완전 일치   : {exact:,}")
    log(f"  별칭 추정(채택)  : {len(alias):,}")
    log(f"  충돌 확인(제외)  : {len(conflict):,}")

    # 우선주
    by_name_first = {}
    for r in corp_rows:
        by_name_first.setdefault(r["corp_name"], r)

    prefs = []
    for code, info in listed.items():
        if code in matched:
            continue
        name = info["name"] or ""
        m = PREF_SUFFIX.search(name)
        if not m:
            continue
        base = name[: m.start()].strip()
        c = by_name_first.get(base)
        if not c:
            for r in corp_rows:
                if name_match(base, r["corp_name"]):
                    c = r
                    break
        if c:
            prefs.append((code, c["corp_code"], name, info["market"],
                          "우선주", c["stock_code"], "name_suffix"))
    records.extend(prefs)
    log(f"  우선주 확정      : {len(prefs):,}")

    if alias:
        log()
        log("  별칭으로 판단해 채택한 항목 (충돌 증거 없음):")
        for code, kn, dn in alias[:20]:
            log(f"      {code}  '{kn}'  =  '{dn}'")
        if len(alias) > 20:
            log(f"      ... 외 {len(alias)-20}건")

    if conflict:
        log()
        log("  [!] 충돌로 제외:")
        for code, kn, dn, others in conflict:
            log(f"      {code}  목록='{kn}' (실제 {','.join(others)})"
                f"  DART='{dn}'")
        with open("conflicts.txt", "w", encoding="utf-8") as f:
            for code, kn, dn, others in conflict:
                f.write(f"{code}\t{kn}\t{dn}\t{','.join(others)}\n")
        log("      -> conflicts.txt 저장")

    conn.execute("DELETE FROM stock")
    conn.executemany(
        "INSERT INTO stock (stock_code, corp_code, corp_name, market,"
        " stock_knd, pref_of, match_method) VALUES (?,?,?,?,?,?,?)", records)
    conn.commit()

    section("스팟체크")
    for code in ("005930", "000100", "000080", "005380", "033780",
                 "030200", "002380", "005935"):
        r = conn.execute(
            "SELECT corp_name, stock_knd, corp_code FROM stock WHERE stock_code=?",
            (code,)).fetchone()
        log(f"  {code}  {(r['corp_name'] if r else '<없음>'):<16}"
            f"{r['stock_knd'] if r else ''}")

    section("결과")
    n_corp = conn.execute(
        "SELECT COUNT(DISTINCT corp_code) FROM stock").fetchone()[0]
    n_pref = conn.execute(
        "SELECT COUNT(*) FROM stock WHERE stock_knd='우선주'").fetchone()[0]
    log(f"  소스           : {source}")
    log(f"  저장 종목      : {len(records):,}  (우선주 {n_pref:,})")
    log(f"  고유 corp_code : {n_corp:,}   <- API 호출 대상")
    log()
    log(f"  step2 예상 호출: 약 {n_corp*4:,}회")
    log(f"  {dart.report()}")
    log()
    log("  -> python step2_annual.py")


if __name__ == "__main__":
    main()
