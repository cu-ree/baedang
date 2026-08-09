#!/usr/bin/env python3
"""
step3_quarter.py — 분기 배당 발견 및 분해

핵심 발견 (삼성전자 2024):
    1분기(11013)   361
    반기 (11012)   722   = 361 + 361
    3분기(11014) 1,083   = 361 + 361 + 361
    사업 (11011) 1,446   = 361 + 361 + 361 + 363

  -> 누계값이므로 차분하면 분기별 배당금이 나온다.

전략
  Phase 1  배당 지급 법인 전체 × 최근 2년 × 분기보고서 3종 스캔
           -> 분기/반기 배당 기업 발견
  Phase 2  발견된 기업만 과거로 확장

    DART_API_KEY=xxx python3 step3_quarter.py
"""

import time
from datetime import datetime

from common import (Dart, RateLimitExceeded, db, log, num, norm,
                    parse_alot, section)

SCAN_YEARS = [2025, 2024]            # Phase 1
EXPAND_YEARS = [2023, 2022, 2021]    # Phase 2
QUARTER_REPRTS = ["11013", "11012", "11014"]
SLEEP = 0.08


def already(conn, corp, year, reprt):
    return conn.execute(
        "SELECT 1 FROM fetch_log WHERE corp_code=? AND bsns_year=? AND reprt_code=?",
        (corp, year, reprt)).fetchone() is not None


def mark(conn, corp, year, reprt, status):
    conn.execute("INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?)",
                 (corp, year, reprt, status,
                  datetime.now().isoformat(timespec="seconds")))


def store_cumulative(conn, corp, year, reprt, items):
    """분기보고서는 당기 값만 사용 (전기/전전기는 연간과 혼동 위험)."""
    rcept = items[0].get("rcept_no") if items else None
    for it in items:
        se = norm(it.get("se"))
        knd = (it.get("stock_knd") or "").strip()
        if not knd or "주당현금배당금" not in se:
            continue
        dps = num(it.get("thstrm"))
        if dps is None:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO dividend_cumulative"
            " (corp_code, bsns_year, reprt_code, stock_knd, dps, yield_pct, rcept_no)"
            " VALUES (?,?,?,?,?,NULL,?)",
            (corp, year, reprt, knd, dps, rcept))


def collect(conn, dart, corps, years, phase):
    total = len(corps) * len(years) * len(QUARTER_REPRTS)
    section(f"{phase}  {len(corps):,}법인 × {len(years)}년 × 3종 = 최대 {total:,}회")

    done = skipped = 0
    t0 = time.time()
    for i, corp in enumerate(corps, 1):
        for year in years:
            for reprt in QUARTER_REPRTS:
                if already(conn, corp, year, reprt):
                    skipped += 1
                    continue
                status, items = dart.alot_matter(corp, year, reprt)
                mark(conn, corp, year, reprt, status)
                if status == "000" and items:
                    store_cumulative(conn, corp, year, reprt, items)
                done += 1
                time.sleep(SLEEP)

        if i % 200 == 0:
            conn.commit()
            el = time.time() - t0
            rate = done / el if el else 0
            log(f"  {i:>5}/{len(corps)}  |  호출 {dart.calls:>6}  |  {rate:.1f}/s")

    conn.commit()
    log(f"  완료: 호출 {done:,} / 건너뜀 {skipped:,}")


def derive_quarters(conn):
    """누계 -> 분기 차분. 값 생성 금지: 누계가 없으면 해당 분기는 만들지 않음."""
    section("분기 차분")

    conn.execute("DELETE FROM dividend_quarter")

    rows = conn.execute(
        "SELECT corp_code, bsns_year, stock_knd, reprt_code, dps"
        " FROM dividend_cumulative WHERE dps IS NOT NULL").fetchall()

    cum = {}
    for r in rows:
        cum.setdefault((r["corp_code"], r["bsns_year"], r["stock_knd"]),
                       {})[r["reprt_code"]] = r["dps"]

    made = 0
    freq_counter = {"quarterly": 0, "semi": 0, "annual": 0, "unknown": 0}

    for (corp, year, knd), v in cum.items():
        q1 = v.get("11013")
        hy = v.get("11012")
        q3 = v.get("11014")
        fy = v.get("11011")

        # 분기 보고서 값이 전혀 없으면 연 1회 배당으로 간주
        if q1 is None and hy is None and q3 is None:
            if fy is not None:
                conn.execute("INSERT OR REPLACE INTO dividend_quarter"
                             " VALUES (?,?,?,?,?)", (corp, year, knd, 4, fy))
                made += 1
                freq_counter["annual"] += 1
            else:
                freq_counter["unknown"] += 1
            continue

        # 누계 단조성 검증 — 깨지면 차분하지 않음 (안전)
        seq = [x for x in (q1, hy, q3, fy) if x is not None]
        if any(b < a for a, b in zip(seq, seq[1:])):
            freq_counter["unknown"] += 1
            continue

        quarters = {}
        if q1 is not None:
            quarters[1] = q1
        if hy is not None:
            quarters[2] = hy - (q1 or 0) if q1 is not None else None
        if q3 is not None and hy is not None:
            quarters[3] = q3 - hy
        if fy is not None:
            base = q3 if q3 is not None else (hy if hy is not None else q1)
            if base is not None:
                quarters[4] = fy - base

        for q, dps in quarters.items():
            if dps is None:
                continue
            conn.execute("INSERT OR REPLACE INTO dividend_quarter"
                         " VALUES (?,?,?,?,?)", (corp, year, knd, q, dps))
            made += 1

        paid = sum(1 for d in quarters.values() if d and d > 0)
        if paid >= 4:
            freq_counter["quarterly"] += 1
        elif paid >= 2:
            freq_counter["semi"] += 1
        elif paid == 1:
            freq_counter["annual"] += 1
        else:
            freq_counter["unknown"] += 1

    conn.commit()
    log(f"  분기 레코드 생성 : {made:,}")
    log(f"  (법인·연도·주식종류) 분류: {freq_counter}")


def main():
    conn = db()
    dart = Dart(conn)

    corps = [r[0] for r in conn.execute(
        "SELECT DISTINCT corp_code FROM dividend_cumulative"
        " WHERE dps IS NOT NULL AND dps > 0 ORDER BY corp_code")]
    if not corps:
        return log("[중단] 배당 데이터가 없습니다. step2_annual.py 먼저 실행.")

    try:
        collect(conn, dart, corps, SCAN_YEARS, "Phase 1  전체 스캔")

        # Phase 2 대상: 분기/반기 배당이 확인된 법인
        found = [r[0] for r in conn.execute(
            "SELECT DISTINCT corp_code FROM dividend_cumulative"
            " WHERE reprt_code IN ('11013','11012','11014') AND dps > 0")]
        log()
        log(f"  분기/반기 배당 법인 발견: {len(found):,}개")

        if found:
            collect(conn, dart, found, EXPAND_YEARS, "Phase 2  과거 확장")

    except RateLimitExceeded as e:
        conn.commit()
        log(f"\n[중단] {e}  — 진행 상황 저장됨. 재실행하면 이어서 진행.")
    except KeyboardInterrupt:
        conn.commit()
        log("\n[사용자 중단] 저장 완료.")

    derive_quarters(conn)

    section("결과")
    q = conn.execute(
        "SELECT COUNT(DISTINCT corp_code) FROM dividend_quarter"
        " WHERE quarter < 4 AND dps > 0").fetchone()[0]
    log(f"  분기/반기 배당 법인 : {q:,}")
    log(f"  {dart.report()}")
    log()
    log("  -> python3 step4_derive.py")


if __name__ == "__main__":
    main()
