#!/usr/bin/env python3
"""
step4_derive.py — 파생 지표 계산  (v3)

v3 변경
  1. stock_knd 정규화 강화
       - 공백 제거      : '우 선 주' -> 우선주
       - 별칭 흡수      : '의결권 있는 주식', '일반주', '기명식보통주' -> 보통주
       - 빈 표기('-')   : __BLANK__ 로 분리 후, 명시적 보통주 행이 없을 때만 흡수
       - '소액주주', '대주주', '결산배당' 등은 주식 종류가 아니므로 흡수하지 않음
         (차등배당 케이스에서 오염 위험)
  2. 무삭감(nocut)은 627종목 동률이라 변별력이 없음
       -> 주력 랭킹을 연속 증액(growth)으로 전환

API 호출 없음. 언제든 재실행 가능.

    python step4_derive.py
"""

import json
import os
import re
from collections import Counter
from datetime import datetime

from common import RAW_DIR, db, log, section

DATA_FLOOR = 2014          # 이보다 앞은 '무배당'이 아니라 '모름'
BLANK = "__BLANK__"

# 배당수익률 이상값 임계. 국내 최고 배당주도 12% 안팎이므로
# 이를 넘으면 공시 기재 방식이 다를 가능성이 높다.
# 값은 그대로 보존하고, 랭킹에서만 제외한다.
YIELD_CAP = 15.0


# ────────────────────────────────────────────────────────────
# stock_knd 정규화
# ────────────────────────────────────────────────────────────

def norm_knd(raw):
    """
    보통주 / 우선주 / __BLANK__ / None 으로 정규화.
    None 은 주식 종류가 아닌 행 (소액주주, 결산배당 등) — 사용하지 않는다.
    """
    s = re.sub(r"\s+", "", str(raw or ""))
    if not s or set(s) <= {"-", "–", "—"}:
        return BLANK
    if "우선" in s or "종류주" in s or "전환상환" in s:
        return "우선주"
    if "보통" in s or "의결권" in s or "일반주" in s or "기명식" in s:
        return "보통주"
    return None


def diagnose(conn):
    section("진단 1 — stock_knd 정규화 결과")
    buckets = Counter()
    unmapped = []
    for r in conn.execute(
        "SELECT stock_knd, COUNT(*) n FROM dividend_cumulative"
        " GROUP BY stock_knd ORDER BY n DESC"):
        k = norm_knd(r["stock_knd"])
        buckets[str(k)] += r["n"]
        if k is None:
            unmapped.append((str(r["stock_knd"]).replace("\n", " "), r["n"]))

    for k, n in buckets.most_common():
        log(f"  {k:<14} {n:>8,}")

    if unmapped:
        log()
        log("  미분류 (의도적으로 제외 — 주식 종류가 아님):")
        for s, n in unmapped[:20]:
            log(f"      {s:<24} {n:,}")
        if len(unmapped) > 20:
            log(f"      ... 외 {len(unmapped)-20}종")

    section("진단 2 — 우선주 매핑")
    n_tick = conn.execute(
        "SELECT COUNT(*) FROM stock WHERE stock_knd='우선주'").fetchone()[0]
    corps = [r[0] for r in conn.execute(
        "SELECT DISTINCT corp_code FROM stock WHERE stock_knd='우선주'")]

    have, multi = 0, []
    for corp in corps:
        kinds = {norm_knd(r[0]) for r in conn.execute(
            "SELECT DISTINCT stock_knd FROM dividend_cumulative WHERE corp_code=?",
            (corp,))}
        if "우선주" in kinds:
            have += 1
            n = conn.execute(
                "SELECT COUNT(*) FROM stock WHERE corp_code=? AND stock_knd='우선주'",
                (corp,)).fetchone()[0]
            if n > 1:
                multi.append((corp, n))

    log(f"  우선주 티커        : {n_tick:,}")
    log(f"  우선주 보유 법인   : {len(corps):,}")
    log(f"  우선주 배당 있음   : {have:,} 법인")
    log(f"  티커 2개 이상 법인 : {len(multi):,}  (구형/신형 구분 불가 -> 제외)")


# ────────────────────────────────────────────────────────────

def build_series(conn, corp_code, knd):
    """
    {year: (dps, yield)} — 사업보고서 기준.
    빈 표기('-')는 단일 종류 회사가 비워둔 것으로 보고 보통주에만 흡수하되,
    명시적 보통주 행이 있으면 그쪽을 우선한다.
    """
    explicit, blank = {}, {}
    for r in conn.execute(
        "SELECT bsns_year, stock_knd, dps, yield_pct FROM dividend_cumulative"
        " WHERE corp_code=? AND reprt_code='11011' ORDER BY bsns_year",
        (corp_code,)):
        k = norm_knd(r["stock_knd"])
        v = (r["dps"], r["yield_pct"])
        y = r["bsns_year"]
        if k == knd:
            prev = explicit.get(y)
            if prev is None or (prev[0] is None and r["dps"] is not None):
                explicit[y] = v
        elif k == BLANK and knd == "보통주":
            prev = blank.get(y)
            if prev is None or (prev[0] is None and r["dps"] is not None):
                blank[y] = v

    for y, v in blank.items():
        explicit.setdefault(y, v)
    return explicit


def calc_streaks(series, latest):
    """
    streak    : 최신 연도부터 연속 배당 지급 연수
    nocut     : 최신 연도부터 전년 대비 감소가 없었던 연수
    growth    : 최신 연도부터 전년 대비 증가가 이어진 연수
    truncated : 데이터 시작 연도에 막혀 끊긴 경우 (실제 기록은 더 길 수 있음)

    데이터 없는 연도를 만나면 즉시 중단. 0으로 간주하지 않는다.
    """
    def dps(y):
        v = series.get(y)
        return v[0] if v else None

    streak, y = 0, latest
    while y >= DATA_FLOOR and dps(y) is not None and dps(y) > 0:
        streak += 1
        y -= 1
    truncated = streak > 0 and y < DATA_FLOOR

    def run(cmp):
        n, yy = (1 if streak else 0), latest
        while True:
            cur, prv = dps(yy), dps(yy - 1)
            if cur is None or prv is None or prv <= 0 or yy - 1 < DATA_FLOOR:
                break
            if not cmp(cur, prv):
                break
            n += 1
            yy -= 1
        return n

    return streak, run(lambda a, b: a >= b), run(lambda a, b: a > b), truncated


def build_flags(series, latest, payout, truncated):
    warn, info = [], []

    def dps(y):
        v = series.get(y)
        return v[0] if v else None

    if payout is not None:
        if payout >= 100:
            warn.append({"code": "payout_over_100", "value": round(payout, 1)})
        elif payout >= 80:
            warn.append({"code": "payout_high", "value": round(payout, 1)})

    # 배당 감소는 사실 전달. 특별배당 소멸일 수 있으므로 경고 아님.
    for y in range(latest, latest - 6, -1):
        cur, prv = dps(y), dps(y - 1)
        if cur is None or prv is None or prv <= 0:
            continue
        chg = (cur - prv) / prv * 100
        if chg <= -30:
            info.append({"code": "dps_decreased", "year": y, "pct": round(chg, 1),
                         "from": prv, "to": cur})
            break

    for y in range(max(DATA_FLOOR, latest - 5), latest + 1):
        v = series.get(y)
        if v is not None and (v[0] is None or v[0] <= 0):
            warn.append({"code": "skipped_year", "year": y})
            break

    cy = series.get(latest, (None, None))[1]
    py = series.get(latest - 1, (None, None))[1]
    if cy and cy > YIELD_CAP:
        # 값은 보존하되 랭킹에서 제외. 페이지에는 안내 문구와 함께 표시.
        warn.append({"code": "yield_outlier", "value": round(cy, 2)})
    elif cy and py and cy > py * 2 and cy > 8:
        warn.append({"code": "yield_spike", "value": round(cy, 2)})

    paid = [y for y in series if series[y][0] and series[y][0] > 0]
    if len(paid) < 3:
        info.append({"code": "short_history", "count": len(paid)})
    if truncated:
        info.append({"code": "history_truncated", "since": DATA_FLOOR})

    return {"warn": warn, "info": info}


def detect_freq(conn, corp_code, knd, year):
    """분기 레코드에서 실제 지급 횟수를 센다. 빈 표기는 보통주에만 흡수."""
    paid = 0
    for r in conn.execute(
        "SELECT stock_knd, dps FROM dividend_quarter"
        " WHERE corp_code=? AND bsns_year=?", (corp_code, year)):
        k = norm_knd(r["stock_knd"])
        if k == BLANK and knd == "보통주":
            k = "보통주"
        if k == knd and r["dps"] and r["dps"] > 0:
            paid += 1
    return ("quarterly" if paid >= 4 else "semi" if paid >= 2
            else "annual" if paid == 1 else "unknown")


def label_nocut(n, truncated):
    if not n:
        return "-"
    return f"{DATA_FLOOR}년 이후 무삭감" if truncated else f"{n}년 연속 무삭감"


# ────────────────────────────────────────────────────────────
# 이상값 원인 자동 진단
# ────────────────────────────────────────────────────────────

def inspect_outliers(conn, limit=12):
    """
    수익률 이상값의 원인을 원본 응답으로 추정한다.

    가장 유력한 가설: 일부 회사가 '현금배당수익률' 칸에
    시가 대비가 아니라 '액면가 대비 배당률'을 기재.
        주당배당금 / 액면가 × 100 == 수익률  이면 확정에 가깝다.
    """
    section(f"이상값 점검 — 배당수익률 {YIELD_CAP}% 초과")

    rows = conn.execute(
        "SELECT s.stock_code, s.corp_name, s.corp_code, d.latest_year,"
        " d.latest_dps, d.latest_yield FROM derived d"
        " JOIN stock s USING(stock_code) WHERE d.latest_yield > ?"
        " ORDER BY d.latest_yield DESC", (YIELD_CAP,)).fetchall()

    log(f"  대상 {len(rows):,}종목  (랭킹에서 제외됨. 페이지에는 안내와 함께 표시)")
    if not rows:
        return

    verdict = Counter()
    log()
    log(f"  {'종목':<16}{'수익률':>8}{'주당배당':>10}{'액면가':>8}"
        f"{'배당/액면':>10}   판정")
    log("  " + "-" * 68)

    for r in rows[:limit]:
        par = conn.execute(
            "SELECT par_value FROM dividend_year WHERE corp_code=? AND bsns_year=?",
            (r["corp_code"], r["latest_year"])).fetchone()
        par = par["par_value"] if par else None

        par_rate = None
        if par and par > 0 and r["latest_dps"] is not None:
            par_rate = r["latest_dps"] / par * 100

        y = r["latest_yield"]
        if par_rate is not None and abs(par_rate - y) <= max(0.5, y * 0.02):
            note = "액면가 대비 배당률로 기재"
            verdict["par_rate"] += 1
        elif par_rate is not None:
            note = "원인 미상"
            verdict["unknown"] += 1
        else:
            note = "액면가 없음 — 판정 불가"
            verdict["no_par"] += 1

        log(f"  {r['corp_name'][:15]:<16}{y:>7.1f}%"
            f"{(r['latest_dps'] or 0):>10,.0f}"
            f"{(par or 0):>8,.0f}"
            f"{(f'{par_rate:.1f}%' if par_rate is not None else '-'):>10}   {note}")

    if len(rows) > limit:
        log(f"  ... 외 {len(rows)-limit}종목")

    log()
    log(f"  판정 요약: {dict(verdict)}")

    # 원본 1건 확인
    top = rows[0]
    path = os.path.join(
        RAW_DIR, f"{top['corp_code']}_{top['latest_year']}_11011.json")
    log()
    log(f"  원본 확인 — {top['corp_name']}  ({path})")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for it in data.get("list", []):
            se = str(it.get("se", ""))
            if any(k in se.replace(" ", "")
                   for k in ("현금배당수익률", "주당현금배당금", "주당액면가액")):
                log(f"      [{it.get('stock_knd') or '-':<6}] {se:<24}"
                    f" 당기={it.get('thstrm')}")
    except FileNotFoundError:
        log("      원본 파일 없음 (raw/ 폴더 확인)")
    except Exception as e:
        log(f"      읽기 실패: {e}")

    log()
    log("  대응")
    log("   - par_rate 가 다수면 DART 기재 방식 차이. 우리가 고칠 수 없음.")
    log("     -> 랭킹 제외 + 페이지에 '공시 기재 기준이 달라 보입니다' 안내")
    log("   - unknown 이 다수면 파싱을 다시 봐야 함.")

    scan_par_rate_contamination(conn)


def scan_par_rate_contamination(conn):
    """
    액면배당률 오기재는 15% 초과에만 있는 게 아니다.
    액면가 500원 · 주당 25원이면 액면배당률 5%로, 정상 수익률과 구분되지 않는다.
    전 종목에서 (주당배당 / 액면가 × 100) 과 공시 수익률이 일치하는 비율을 센다.

    주의: 우연 일치가 있을 수 있으므로 소수점까지 근접한 경우만 의심으로 본다.
    """
    section("전수 스캔 — 액면배당률 오기재 의심")

    rows = conn.execute(
        "SELECT s.stock_code, s.corp_name, d.latest_year, d.latest_dps,"
        " d.latest_yield FROM derived d JOIN stock s USING(stock_code)"
        " WHERE d.latest_yield IS NOT NULL AND d.latest_yield > 0"
        " AND d.latest_dps IS NOT NULL").fetchall()

    suspect, checked = [], 0
    for r in rows:
        par = conn.execute(
            "SELECT par_value FROM dividend_year d JOIN stock s"
            " ON s.corp_code=d.corp_code WHERE s.stock_code=? AND d.bsns_year=?",
            (r["stock_code"], r["latest_year"])).fetchone()
        par = par["par_value"] if par else None
        if not par or par <= 0:
            continue
        checked += 1
        pr = r["latest_dps"] / par * 100
        # 소수점 둘째 자리까지 사실상 동일할 때만 의심
        if abs(pr - r["latest_yield"]) < 0.01:
            suspect.append((r["corp_name"], r["latest_yield"], r["latest_dps"], par))

    ratio = len(suspect) / checked * 100 if checked else 0
    log(f"  검사 가능 종목 : {checked:,}  (액면가 확보분)")
    log(f"  의심 종목      : {len(suspect):,}  ({ratio:.2f}%)")

    if suspect:
        log()
        log(f"  {'종목':<18}{'공시수익률':>10}{'주당배당':>10}{'액면가':>10}")
        log("  " + "-" * 50)
        for nm, y, dps, par in sorted(suspect, key=lambda x: -x[1])[:20]:
            log(f"  {nm[:17]:<18}{y:>9.2f}%{dps:>10,.0f}{par:>10,.0f}")
        if len(suspect) > 20:
            log(f"  ... 외 {len(suspect)-20}종목")

    log()
    if ratio < 1:
        log("  >>> 1% 미만. 배당수익률을 랭킹 축으로 사용해도 무방.")
    elif ratio < 5:
        log("  >>> 1~5%. 사용 가능하나 의심 종목에 안내 문구를 다는 것을 권장.")
    else:
        log("  >>> 5% 이상. 오염이 넓다. 배당수익률 랭킹을 보조 지표로 강등하고")
        log("      연속 증액 / 분기배당을 주력으로 쓸 것.")


# ────────────────────────────────────────────────────────────

def main():
    conn = db()
    diagnose(conn)

    section("파생 지표 계산")
    stocks = conn.execute(
        "SELECT stock_code, corp_code, corp_name, stock_knd FROM stock").fetchall()
    conn.execute("DELETE FROM derived")

    now = datetime.now().isoformat(timespec="seconds")
    made = no_data = skipped_multi = 0
    freq_stat, knd_stat = Counter(), Counter()

    multi = {r[0] for r in conn.execute(
        "SELECT corp_code FROM stock WHERE stock_knd='우선주'"
        " GROUP BY corp_code HAVING COUNT(*) > 1")}

    for s in stocks:
        if s["stock_knd"] == "우선주" and s["corp_code"] in multi:
            skipped_multi += 1
            continue

        series = build_series(conn, s["corp_code"], s["stock_knd"])
        paid = [y for y, (d, _) in series.items() if d and d > 0]
        if not paid:
            no_data += 1
            continue

        latest = max(paid)
        dps, yld = series[latest]
        yr = conn.execute(
            "SELECT payout_pct FROM dividend_year WHERE corp_code=? AND bsns_year=?",
            (s["corp_code"], latest)).fetchone()
        payout = yr["payout_pct"] if yr else None

        streak, nocut, growth, truncated = calc_streaks(series, latest)
        flags = build_flags(series, latest, payout, truncated)
        freq = detect_freq(conn, s["corp_code"], s["stock_knd"], latest)

        freq_stat[freq] += 1
        knd_stat[s["stock_knd"]] += 1

        conn.execute(
            "INSERT OR REPLACE INTO derived VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (s["stock_code"], latest, dps, yld, payout, streak, nocut, growth,
             freq, json.dumps(flags, ensure_ascii=False), min(series), now))
        made += 1

    conn.commit()

    log(f"  전체 종목          : {len(stocks):,}")
    log(f"  배당 이력 없음     : {no_data:,}")
    log(f"  우선주 다종류 제외 : {skipped_multi:,}")
    log(f"  페이지 생성 대상   : {made:,}")
    log()
    log(f"  주식 종류별 : {dict(knd_stat)}")
    log(f"  배당 주기별 : {dict(freq_stat)}")

    section("검증 — 삼성전자")
    for code in ("005930", "005935"):
        r = conn.execute(
            "SELECT d.*, s.corp_name, s.stock_knd FROM derived d"
            " JOIN stock s USING(stock_code) WHERE stock_code=?", (code,)).fetchone()
        if not r:
            log(f"  {code}  <derived 없음>")
            continue
        fl = json.loads(r["risk_flags"])
        tr = any(f["code"] == "history_truncated" for f in fl["info"])
        log(f"  {r['corp_name']} ({r['stock_knd']})")
        log(f"    {r['latest_year']}년  주당 {r['latest_dps']:,.0f}원"
            f"  수익률 {r['latest_yield']}%  성향 {r['latest_payout']}%")
        log(f"    연속배당 {r['streak_years']}년 | {label_nocut(r['nocut_years'], tr)}"
            f" | 증액 {r['growth_years']}년 | {r['pay_freq']}")
        log(f"    warn: {fl['warn']}")
        log(f"    info: {fl['info']}")
        log()

    # ── 랭킹 ─────────────────────────────────────────────
    section("랭킹 — 연속 증액 상위 15  (간판 지표)")
    for r in conn.execute(
        "SELECT s.corp_name, s.stock_code, d.growth_years, d.latest_dps,"
        " d.latest_yield FROM derived d JOIN stock s USING(stock_code)"
        " WHERE s.stock_knd='보통주' AND d.growth_years >= 2"
        " ORDER BY d.growth_years DESC, d.latest_yield DESC LIMIT 15"):
        log(f"    {r['corp_name']:<18} {r['growth_years']}년 연속 증액   "
            f"{r['latest_dps']:,.0f}원   {r['latest_yield'] or 0}%")

    dist = Counter(r[0] for r in conn.execute(
        "SELECT growth_years FROM derived d JOIN stock s USING(stock_code)"
        " WHERE s.stock_knd='보통주'"))
    log()
    log(f"    증액 연수 분포: {dict(sorted(dist.items(), reverse=True))}")

    section("랭킹 — 분기배당 종목  (최대 차별점)")
    for r in conn.execute(
        "SELECT s.corp_name, d.latest_dps, d.latest_yield FROM derived d"
        " JOIN stock s USING(stock_code) WHERE d.pay_freq='quarterly'"
        " AND s.stock_knd='보통주' ORDER BY d.latest_yield DESC"):
        log(f"    {r['corp_name']:<18} {r['latest_dps']:,.0f}원   "
            f"{r['latest_yield'] or 0}%")

    section(f"랭킹 — 배당수익률 상위 15  ({YIELD_CAP}% 초과 제외)")
    for r in conn.execute(
        "SELECT s.corp_name, s.stock_knd, d.latest_yield, d.latest_payout"
        " FROM derived d JOIN stock s USING(stock_code)"
        " WHERE d.latest_yield IS NOT NULL AND d.latest_yield > 0"
        " AND d.latest_yield <= ? ORDER BY d.latest_yield DESC LIMIT 15",
        (YIELD_CAP,)):
        log(f"    {r['corp_name']:<18} ({r['stock_knd']}) {r['latest_yield']}%"
            f"   성향 {r['latest_payout'] or '-'}%")

    inspect_outliers(conn)

    section("신호 분포")
    cnt = Counter()
    for r in conn.execute("SELECT risk_flags FROM derived"):
        fl = json.loads(r["risk_flags"])
        for f in fl["warn"]:
            cnt["warn:" + f["code"]] += 1
        for f in fl["info"]:
            cnt["info:" + f["code"]] += 1
    for k, v in cnt.most_common():
        log(f"    {k:<30} {v:,}")

    log()
    log("  -> 2주차 완료. 다음은 3주차 사이트 생성.")


if __name__ == "__main__":
    main()