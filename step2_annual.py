#!/usr/bin/env python3
"""
step2_annual.py — 연간 배당 수집 (사업보고서 기준)

1회 호출 = 3개 연도(당기/전기/전전기).
따라서 2025, 2022, 2019, 2016 네 번 호출하면 2014~2025 커버.

재개 가능: fetch_log 에 기록된 요청은 건너뜀.
중단해도 안전하며, 다시 실행하면 이어서 진행한다.

    DART_API_KEY=xxx python3 step2_annual.py
"""

import time
from datetime import datetime

from common import (Dart, RateLimitExceeded, db, log, parse_alot, section)

# 1회 = 해당연도 + 전기 + 전전기
ANCHOR_YEARS = [2025, 2022, 2019, 2016]
REPRT = "11011"   # 사업보고서
SLEEP = 0.08


def already(conn, corp_code, year):
    r = conn.execute(
        "SELECT 1 FROM fetch_log WHERE corp_code=? AND bsns_year=? AND reprt_code=?",
        (corp_code, year, REPRT)).fetchone()
    return r is not None


def mark(conn, corp_code, year, status):
    conn.execute(
        "INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?)",
        (corp_code, year, REPRT, status, datetime.now().isoformat(timespec="seconds")))


def store(conn, corp_code, parsed):
    for year, rec in parsed.items():
        if year < 2010 or year > datetime.now().year:
            continue

        has_any = (rec["payout"] is not None or rec["total_amt"] is not None
                   or any(v is not None for v in rec["dps"].values()))
        if has_any:
            conn.execute(
                "INSERT OR REPLACE INTO dividend_year"
                " (corp_code, bsns_year, payout_pct, total_amt, net_income,"
                "  eps, par_value, stlm_dt, rcept_no, src_basis)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (corp_code, year, rec["payout"], rec["total_amt"],
                 rec["net_income"], rec["eps"], rec["par_value"],
                 rec["stlm_dt"], rec["rcept_no"], rec["src_basis"]))

        for knd, dps in rec["dps"].items():
            yld = rec["yield"].get(knd)
            if dps is None and yld is None:
                continue          # 값 생성 금지
            conn.execute(
                "INSERT OR REPLACE INTO dividend_cumulative"
                " (corp_code, bsns_year, reprt_code, stock_knd, dps, yield_pct, rcept_no)"
                " VALUES (?,?,?,?,?,?,?)",
                (corp_code, year, REPRT, knd, dps, yld, rec["rcept_no"]))


def main():
    conn = db()
    dart = Dart(conn)

    corps = [r["corp_code"] for r in conn.execute(
        "SELECT DISTINCT corp_code FROM stock ORDER BY corp_code")]
    if not corps:
        return log("[중단] stock 테이블이 비어 있습니다. step1_stocks.py 먼저 실행.")

    total = len(corps) * len(ANCHOR_YEARS)
    section(f"연간 배당 수집  ({len(corps):,}개 법인 × {len(ANCHOR_YEARS)}회 = {total:,})")

    done = skipped = 0
    t0 = time.time()

    try:
        for i, corp in enumerate(corps, 1):
            for year in ANCHOR_YEARS:
                if already(conn, corp, year):
                    skipped += 1
                    continue

                status, items = dart.alot_matter(corp, year, REPRT)
                mark(conn, corp, year, status)

                if status == "000" and items:
                    store(conn, corp, parse_alot(items, year))
                done += 1
                time.sleep(SLEEP)

            if i % 200 == 0:
                conn.commit()
                el = time.time() - t0
                rate = done / el if el else 0
                left = (total - skipped - done) / rate if rate else 0
                log(f"  {i:>5}/{len(corps)}  법인  |  호출 {dart.calls:>6}  "
                    f"|  {rate:.1f}/s  |  남은 시간 약 {left/60:.0f}분")

    except RateLimitExceeded as e:
        conn.commit()
        log()
        log(f"[중단] {e}")
        log("  진행 상황은 저장되었습니다. 내일 다시 실행하면 이어서 진행합니다.")
    except KeyboardInterrupt:
        conn.commit()
        log("\n[사용자 중단] 진행 상황 저장 완료.")

    conn.commit()

    section("결과")
    n_year = conn.execute("SELECT COUNT(*) FROM dividend_year").fetchone()[0]
    n_cum = conn.execute("SELECT COUNT(*) FROM dividend_cumulative").fetchone()[0]
    n_corp = conn.execute(
        "SELECT COUNT(DISTINCT corp_code) FROM dividend_cumulative WHERE dps > 0"
    ).fetchone()[0]
    yr = conn.execute(
        "SELECT MIN(bsns_year), MAX(bsns_year) FROM dividend_year").fetchone()

    log(f"  이번 실행 호출 : {done:,}   (건너뜀 {skipped:,})")
    log(f"  dividend_year       : {n_year:,} 행")
    log(f"  dividend_cumulative : {n_cum:,} 행")
    log(f"  배당 지급 법인      : {n_corp:,}")
    log(f"  연도 범위           : {yr[0]} ~ {yr[1]}")
    log(f"  {dart.report()}")
    log()
    if n_corp:
        log("  -> python3 step3_quarter.py")


if __name__ == "__main__":
    main()
