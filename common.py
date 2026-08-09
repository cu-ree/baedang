#!/usr/bin/env python3
"""
common.py — 공통 모듈

설계 원칙 (MapleFlow 규율 이식)
  1. 원본 보존 : API 응답 원문을 raw/ 에 저장. 파싱은 언제든 재실행 가능
  2. 값 생성 금지 : 데이터 없음은 NULL. 0과 절대 혼동하지 않음
  3. 재개 가능 : fetch_log 로 이미 받은 요청은 건너뜀
  4. 안전 중단 : 한도 초과(020) 감지 시 즉시 멈춤. 부분 결과는 보존
"""

import json
import os
import re
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_KEY = os.environ.get("DART_API_KEY", "a564796bfb2862e6616f55ff47104c090eefd6ad")
BASE = "https://opendart.fss.or.kr/api"

DB_PATH = "dividend.db"
RAW_DIR = "raw"

# 하루 한도 40,000. 안전 마진 남김
DAILY_BUDGET = 36000

REPRT_CODES = {
    "11013": ("1Q", 1),   # 1분기보고서 (누계 Q1)
    "11012": ("HY", 2),   # 반기보고서   (누계 Q1~Q2)
    "11014": ("3Q", 3),   # 3분기보고서 (누계 Q1~Q3)
    "11011": ("FY", 4),   # 사업보고서   (연간)
}

STATUS_MSG = {
    "000": "정상", "010": "미등록 키", "011": "사용불가 키",
    "013": "데이터 없음", "020": "일일 한도 초과", "100": "파라미터 오류",
    "800": "시스템 점검", "900": "정의되지 않은 오류", "901": "계정 오류",
}

SSL_CTX = ssl.create_default_context()


class RateLimitExceeded(Exception):
    pass


# ────────────────────────────────────────────────────────────
# DB
# ────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS stock (
    stock_code   TEXT PRIMARY KEY,
    corp_code    TEXT NOT NULL,
    corp_name    TEXT NOT NULL,
    market       TEXT,
    stock_knd    TEXT NOT NULL,          -- 보통주 / 우선주
    pref_of      TEXT,                   -- 우선주면 대응 보통주 코드
    match_method TEXT                    -- corpcode / name_suffix
);

-- 원본: 보고서별 누계 배당 (가공 금지)
CREATE TABLE IF NOT EXISTS dividend_cumulative (
    corp_code   TEXT    NOT NULL,
    bsns_year   INTEGER NOT NULL,
    reprt_code  TEXT    NOT NULL,
    stock_knd   TEXT    NOT NULL,
    dps         REAL,                    -- 주당 현금배당금. NULL = 데이터 없음
    yield_pct   REAL,
    rcept_no    TEXT,
    PRIMARY KEY (corp_code, bsns_year, reprt_code, stock_knd)
);

-- 원본: 연간 회사 단위 지표 (사업보고서 기준)
CREATE TABLE IF NOT EXISTS dividend_year (
    corp_code    TEXT    NOT NULL,
    bsns_year    INTEGER NOT NULL,
    payout_pct   REAL,                   -- 현금배당성향
    total_amt    REAL,                   -- 현금배당금총액(백만원)
    net_income   REAL,                   -- 당기순이익(백만원)
    eps          REAL,
    par_value    REAL,
    stlm_dt      TEXT,
    rcept_no     TEXT,
    src_basis    TEXT,                   -- 연결 / 별도
    PRIMARY KEY (corp_code, bsns_year)
);

-- 파생: 분기 분해 (언제든 재생성 가능)
CREATE TABLE IF NOT EXISTS dividend_quarter (
    corp_code   TEXT    NOT NULL,
    bsns_year   INTEGER NOT NULL,
    stock_knd   TEXT    NOT NULL,
    quarter     INTEGER NOT NULL,
    dps         REAL,
    PRIMARY KEY (corp_code, bsns_year, stock_knd, quarter)
);

-- 파생: 종목 지표
CREATE TABLE IF NOT EXISTS derived (
    stock_code     TEXT PRIMARY KEY,
    latest_year    INTEGER,
    latest_dps     REAL,
    latest_yield   REAL,
    latest_payout  REAL,
    streak_years   INTEGER,   -- 연속 배당 지급 연수
    nocut_years    INTEGER,   -- 연속 무삭감 연수
    growth_years   INTEGER,   -- 연속 증액 연수
    pay_freq       TEXT,      -- annual / semi / quarterly
    risk_flags     TEXT,      -- JSON 배열
    data_from      INTEGER,   -- 데이터 시작 연도 (과장 방지용)
    updated_at     TEXT
);

-- 요청 로그 (재개용)
CREATE TABLE IF NOT EXISTS fetch_log (
    corp_code   TEXT NOT NULL,
    bsns_year   INTEGER NOT NULL,
    reprt_code  TEXT NOT NULL,
    status      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (corp_code, bsns_year, reprt_code)
);

CREATE INDEX IF NOT EXISTS idx_cum_corp ON dividend_cumulative(corp_code);
CREATE INDEX IF NOT EXISTS idx_year_corp ON dividend_year(corp_code);
CREATE INDEX IF NOT EXISTS idx_stock_corp ON stock(corp_code);
"""


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ────────────────────────────────────────────────────────────
# DART 클라이언트
# ────────────────────────────────────────────────────────────

class Dart:
    def __init__(self, conn):
        if not API_KEY:
            sys.exit("[중단] DART_API_KEY 환경변수가 없습니다.")
        self.conn = conn
        self.calls = 0
        self.counts = {}

    def _bump(self, status):
        self.counts[status] = self.counts.get(status, 0) + 1

    def get(self, path, params, raw=False, retries=3):
        if self.calls >= DAILY_BUDGET:
            raise RateLimitExceeded(f"자체 예산 {DAILY_BUDGET} 도달")

        p = dict(params)
        p["crtfc_key"] = API_KEY
        url = f"{BASE}/{path}?" + urllib.parse.urlencode(p)
        req = urllib.request.Request(url, headers={"User-Agent": "dividend/1.0"})

        for attempt in range(retries):
            self.calls += 1
            try:
                with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
                    body = r.read()
                    return body if raw else body.decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                b = e.read()
                return b if raw else b.decode("utf-8", "replace")
            except Exception as e:
                if attempt == retries - 1:
                    return None
                time.sleep(1.5 * (attempt + 1))
        return None

    def alot_matter(self, corp_code, bsns_year, reprt_code):
        """(status, list) 반환. 원본은 raw/ 에 저장."""
        body = self.get("alotMatter.json", {
            "corp_code": corp_code,
            "bsns_year": str(bsns_year),
            "reprt_code": reprt_code,
        })
        if body is None:
            self._bump("NET")
            return "NET", []

        try:
            d = json.loads(body)
        except Exception:
            self._bump("PARSE")
            return "PARSE", []

        status = d.get("status", "?")
        self._bump(status)

        if status == "020":
            raise RateLimitExceeded("DART 응답 020 — 일일 한도 초과")

        if status == "000":
            save_raw(f"{corp_code}_{bsns_year}_{reprt_code}.json", body)
            return status, d.get("list", [])
        return status, []

    def report(self):
        return f"호출 {self.calls}회 / 상태 {self.counts}"


def save_raw(name, content):
    os.makedirs(RAW_DIR, exist_ok=True)
    with open(os.path.join(RAW_DIR, name), "w", encoding="utf-8") as f:
        f.write(content)


# ────────────────────────────────────────────────────────────
# 파싱 유틸
# ────────────────────────────────────────────────────────────

def norm(s):
    """se 항목명 정규화. 공백/전각공백 제거."""
    return re.sub(r"[\s\u00a0]+", "", str(s or ""))


def num(v):
    """
    문자열 -> float. 값 생성 금지 원칙:
      '-', '', None  ->  None  (데이터 없음)
      '0'            ->  0.0   (실제 0)
    """
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("−", "-")
    if s in ("", "-", "–", "—", "N/A", "해당사항없음"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        f = float(s)
    except ValueError:
        return None
    return -f if neg else f


# thstrm=당기, frmtrm=전기, lwfr=전전기
YEAR_OFFSET = {"thstrm": 0, "frmtrm": -1, "lwfr": -2}


def parse_alot(items, bsns_year):
    """
    alotMatter list -> {year: {...}} 구조로 변환.
    1회 응답에 3개 연도가 들어있음.
    """
    out = {}
    for off_key, off in YEAR_OFFSET.items():
        out[bsns_year + off] = {
            "dps": {}, "yield": {},
            "payout": None, "total_amt": None, "net_income": None,
            "eps": None, "par_value": None, "src_basis": None,
        }

    rcept = items[0].get("rcept_no") if items else None
    stlm = items[0].get("stlm_dt") if items else None

    for it in items:
        se = norm(it.get("se"))
        knd = (it.get("stock_knd") or "").strip() or None

        for off_key, off in YEAR_OFFSET.items():
            y = bsns_year + off
            v = num(it.get(off_key))
            rec = out[y]

            if "주당현금배당금" in se and knd:
                rec["dps"][knd] = v
            elif "현금배당수익률" in se and knd:
                rec["yield"][knd] = v
            elif "현금배당성향" in se:
                # 연결 우선. 별도밖에 없으면 그것 사용
                if "연결" in se or rec["payout"] is None:
                    if rec["src_basis"] != "연결" or "연결" in se:
                        rec["payout"] = v
                        rec["src_basis"] = "연결" if "연결" in se else "별도"
            elif "현금배당금총액" in se:
                rec["total_amt"] = v
            elif "당기순이익" in se:
                if "연결" in se or rec["net_income"] is None:
                    rec["net_income"] = v
            elif "주당순이익" in se:
                if "연결" in se or rec["eps"] is None:
                    rec["eps"] = v
            elif "주당액면가액" in se:
                rec["par_value"] = v

    for y in out:
        out[y]["rcept_no"] = rcept
        out[y]["stlm_dt"] = stlm
    return out


def log(m=""):
    print(m, flush=True)


def section(t):
    log()
    log("=" * 64)
    log(f"  {t}")
    log("=" * 64)
