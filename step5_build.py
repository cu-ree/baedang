#!/usr/bin/env python3
"""
step5_build.py — 정적 사이트 생성

    site/
      index.html
      style.css
      robots.txt
      sitemap.xml
      stock/{코드}-{종목명}/index.html     1,640개
      rank/{growth|quarterly|yield|streak}/index.html
      guide/index.html
      guide/{slug}/index.html              guides/*.md 가 있을 때만

1차 공개 정책
    AdSense 심사를 통과해야 하므로 처음부터 1,640개를 색인시키지 않는다.
    핵심 종목 + 랭킹 + 가이드만 색인 허용, 나머지는 noindex + 사이트맵 제외.
    승인 후 PUBLISH_ALL = True 로 바꾸고 다시 빌드하면 전체가 열린다.

의존성 없음.
    python step5_build.py
"""

import html
import json
import os
import re
import shutil
from datetime import datetime, timezone

from common import db, log, section

# ────────────────────────────────────────────────────────────
# 설정
# ────────────────────────────────────────────────────────────

SITE_NAME = "배당체크"
BASE_URL = "https://costcheck.kr"          # 끝에 / 없이
OUT = "docs"
GUIDE_SRC = "guides"                        # 직접 쓴 .md 를 넣는 폴더

PUBLISH_ALL = False                         # AdSense 승인 후 True
YIELD_CAP = 15.0
DATA_FLOOR = 2014

# 검색엔진 소유권 확인용 메타 태그.
# 파일 업로드 방식은 재빌드 때 docs/ 가 초기화되면서 사라지므로 쓰지 않는다.
# 각 도구에서 발급받은 content 값을 채우면 모든 페이지 <head> 에 들어간다.
VERIFY_META = {
    "naver-site-verification": "978a1427ab793680fd2672125cf432b7e54e2f1b",
    "google-site-verification": "",
}

# 1차 공개 기준
FIRST_WAVE_GROWTH_MIN = 4                   # 연속 증액 4년 이상
FIRST_WAVE_YIELD_TOP = 60                   # 배당수익률 상위 N

GUIDE_TOPICS = [    ("기준일-배당락일", "배당기준일과 배당락일, 언제까지 사야 배당을 받나"),
    ("배당소득세", "배당소득세 15.4%와 금융소득종합과세 2,000만원"),
    ("배당성향", "배당성향이 높으면 왜 위험 신호인가"),
    ("우선주-배당", "우선주 배당이 보통주보다 많은 이유"),
    ("분기배당", "분기배당 기업에 투자할 때 알아야 할 것"),
    ("고배당-함정", "배당수익률이 갑자기 높아졌을 때 의심할 것"),
]

FLAG_TEXT = {
    "payout_over_100": "배당성향 100% 초과 — 순이익보다 많이 배당했습니다",
    "payout_high": "배당성향 80% 이상 — 이익이 줄면 배당이 조정될 수 있습니다",
    "skipped_year": "최근 배당을 거른 해가 있습니다",
    "yield_spike": "배당수익률이 단기간에 급등했습니다 — 주가 하락 가능성",
    "yield_outlier": "공시된 배당수익률이 비정상적으로 높습니다 — 원문 확인 권장",
    "par_rate_suspect": "공시 수익률이 액면가 대비 배당률과 일치합니다 — 기재 기준 확인 필요",
    "short_history": "배당 이력이 3년 미만입니다",
    "history_truncated": f"{DATA_FLOOR}년 이전 자료는 제공 범위 밖입니다",
    "dps_decreased": "",   # 별도 문장으로 처리
}


# ────────────────────────────────────────────────────────────
# 유틸
# ────────────────────────────────────────────────────────────

def esc(s):
    return html.escape(str(s if s is not None else ""))


def slug(name):
    s = re.sub(r"[\s/\\?#\[\]@!$&'()*+,;=:\"<>|]+", "", str(name))
    return s or "x"


def won(v, unit="원"):
    if v is None:
        return "—"
    return f"{v:,.0f}{unit}" if abs(v - round(v)) < 0.005 else f"{v:,.2f}{unit}"


def pct(v):
    return "—" if v is None else f"{v:g}%"


# 영문자·숫자로 끝나는 종목명의 한글 발음 끝소리 (받침 유무 판정용)
_TAIL = {
    "A": "이", "B": "비", "C": "씨", "D": "디", "E": "이", "F": "프", "G": "지",
    "H": "치", "I": "이", "J": "이", "K": "이", "L": "엘", "M": "엠", "N": "엔",
    "O": "오", "P": "피", "Q": "큐", "R": "알", "S": "스", "T": "티", "U": "유",
    "V": "이", "W": "유", "X": "스", "Y": "이", "Z": "트",
    "0": "영", "1": "일", "2": "이", "3": "삼", "4": "사",
    "5": "오", "6": "육", "7": "칠", "8": "팔", "9": "구",
}


def josa(word, with_batchim, without):
    """
    받침 유무에 따라 조사를 고른다.
        josa("삼성전자", "은", "는") -> "는"
        josa("유한양행", "은", "는") -> "은"
        josa("KT", "은", "는")      -> "는"   (티 -> 받침 없음)
    """
    w = str(word or "").strip()
    if not w:
        return without
    ch = w[-1]
    if "가" <= ch <= "힣":
        return with_batchim if (ord(ch) - 0xAC00) % 28 else without
    t = _TAIL.get(ch.upper())
    if t:
        return with_batchim if (ord(t) - 0xAC00) % 28 else without
    return without


def nm(word, a, b):
    """종목명 + 조사를 붙여 반환."""
    return f"{word}{josa(word, a, b)}"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ────────────────────────────────────────────────────────────
# 최소 마크다운
# ────────────────────────────────────────────────────────────

def md(text):
    """
    최소 마크다운. 지원: 제목, 문단, 목록, 표, 굵게, 링크, 인라인 코드.
    선두 H1 은 페이지 템플릿의 제목과 중복되므로 제거한다.
    """
    text = text.replace("\u200b", "").replace("\ufeff", "")
    lines = text.splitlines()

    # 선두 H1 제거
    for i, l in enumerate(lines):
        if l.strip():
            if re.match(r"^#\s+", l):
                lines = lines[i + 1:]
            break

    out, buf, in_ul = [], [], False

    def flush():
        nonlocal buf
        if buf:
            out.append("<p>" + " ".join(buf) + "</p>")
            buf = []

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    def inline(s):
        s = esc(s)
        s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', s)
        return s

    def is_row(l):
        return l.strip().startswith("|") and l.strip().endswith("|")

    def cells(l):
        return [c.strip() for c in l.strip().strip("|").split("|")]

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()

        if not line.strip():
            flush()
            close_ul()
            i += 1
            continue

        # 표
        if (is_row(line) and i + 1 < len(lines)
                and re.match(r"^\|[\s:\-|]+\|$", lines[i + 1].strip())):
            flush()
            close_ul()
            head = cells(line)
            align = ["right" if c.strip().endswith(":") else "left"
                     for c in cells(lines[i + 1])]
            i += 2
            body = []
            while i < len(lines) and is_row(lines[i]):
                body.append(cells(lines[i]))
                i += 1
            th = "".join(
                f'<th style="text-align:{align[j] if j < len(align) else "left"}">'
                f"{inline(c)}</th>" for j, c in enumerate(head))
            tr = "".join(
                "<tr>" + "".join(
                    f'<td style="text-align:{align[j] if j < len(align) else "left"}">'
                    f"{inline(c)}</td>" for j, c in enumerate(r)) + "</tr>"
                for r in body)
            out.append(f"<table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table>")
            continue

        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            flush()
            close_ul()
            lv = min(len(m.group(1)) + 1, 5)
            out.append(f"<h{lv}>{inline(m.group(2))}</h{lv}>")
            i += 1
            continue

        if re.match(r"^[-*]\s+", line):
            flush()
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{inline(re.sub(r'^[-*]  *', '', line))}</li>")
            i += 1
            continue

        buf.append(inline(line.strip()))
        i += 1

    flush()
    close_ul()
    return "\n".join(out)


# ────────────────────────────────────────────────────────────
# 레이아웃
# ────────────────────────────────────────────────────────────

def page(title, desc, body, path_depth, indexed=True, canonical=""):
    up = "../" * path_depth
    robots = ("index,follow" if (indexed or PUBLISH_ALL) else "noindex,follow")
    canon = f'<link rel="canonical" href="{BASE_URL}{canonical}">' if canonical else ""
    verify = "\n".join(f'<meta name="{k}" content="{v}">'
                       for k, v in VERIFY_META.items() if v)
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<meta name="robots" content="{robots}">
{verify}
{canon}
<link rel="preconnect" href="https://cdn.jsdelivr.net">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<link rel="stylesheet" href="{up}style.css">
</head>
<body>
<header class="top">
  <a class="wordmark" href="{up}index.html">배당<span>체크</span></a>
  <nav>
    <a href="{up}rank/growth/index.html">연속 증액</a>
    <a href="{up}rank/quarterly/index.html">분기배당</a>
    <a href="{up}rank/yield/index.html">수익률</a>
    <a href="{up}guide/index.html">가이드</a>
  </nav>
</header>
<main>
{body}
</main>
<footer>
  <p class="src">자료 출처 · 금융감독원 전자공시시스템(DART) 사업보고서 「배당에 관한 사항」</p>
  <p class="src">제공 범위 {DATA_FLOOR}년~ · 공시 원문의 값을 가공 없이 표시합니다</p>
  <p class="disc">본 정보는 투자 참고용입니다. 정확성과 완전성을 보장하지 않으며,
  투자 판단과 그 결과에 대한 책임은 이용자 본인에게 있습니다.</p>
</footer>
</body>
</html>
"""


CSS = """
:root{
  --paper:#FAFAF7; --ink:#16191D; --mute:#5C6470;
  --brass:#9C6B2F; --brass-soft:#EFE2CE;
  --rule:#E4E3DC; --warn:#B4442E; --warn-soft:#F7E9E6;
  --pad:20px;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--paper); color:var(--ink);
  font-family:"Pretendard Variable",Pretendard,-apple-system,system-ui,sans-serif;
  font-size:16px; line-height:1.7; letter-spacing:-0.01em;
}
.num{font-family:"IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
a{color:inherit}
main{max-width:760px;margin:0 auto;padding:0 var(--pad) 72px}

/* header */
.top{
  max-width:760px;margin:0 auto;padding:18px var(--pad) 14px;
  display:flex;align-items:baseline;gap:20px;flex-wrap:wrap;
  border-bottom:1px solid var(--rule);
}
.wordmark{
  font-weight:800;font-size:19px;letter-spacing:-0.04em;text-decoration:none;
}
.wordmark span{color:var(--brass)}
.top nav{display:flex;gap:16px;font-size:13.5px;color:var(--mute)}
.top nav a{text-decoration:none}
.top nav a:hover{color:var(--brass)}

/* stock head */
.head{padding:30px 0 10px;border-bottom:1px solid var(--rule)}
.eyebrow{font-size:12.5px;color:var(--mute);letter-spacing:0.06em}
h1{font-size:30px;font-weight:800;letter-spacing:-0.045em;margin:6px 0 2px;line-height:1.25}
h1 .code{font-size:16px;font-weight:500;color:var(--mute);margin-left:8px}

.figs{display:flex;gap:30px;flex-wrap:wrap;margin:24px 0 26px}
.fig .v{font-size:34px;font-weight:600;letter-spacing:-0.03em;line-height:1.1}
.fig .l{font-size:12.5px;color:var(--mute);margin-top:4px}

/* signature: 배당 리듬 */
.rhythm{border:1px solid var(--rule);border-radius:2px;padding:16px 18px;margin:26px 0}
.rhythm .cap{font-size:12.5px;color:var(--mute);margin-bottom:12px}
.strip{display:grid;grid-template-columns:repeat(4,1fr);gap:5px}
.q{height:34px;border-radius:1px;background:#EDECE6;position:relative}
.q.on{background:var(--brass)}
.q .qk{
  position:absolute;bottom:-19px;left:0;right:0;text-align:center;
  font-size:11px;color:var(--mute)
}
.q.on .amt{
  position:absolute;top:8px;left:0;right:0;text-align:center;
  font-size:11.5px;color:#fff
}
.rhythm .note{margin-top:28px;font-size:13.5px;color:var(--mute)}

/* summary */
.summary{font-size:16.5px;margin:26px 0 8px}
.summary p{margin:0 0 12px}

/* badges */
.badges{display:flex;flex-direction:column;gap:7px;margin:20px 0}
.badge{font-size:13.5px;padding:9px 12px;border-radius:2px;border-left:3px solid}
.badge.w{background:var(--warn-soft);border-color:var(--warn)}
.badge.i{background:#F2F1EB;border-color:#C9C7BD;color:#3E434B}

/* history */
h2{font-size:17px;font-weight:700;letter-spacing:-0.03em;margin:40px 0 14px}
table{width:100%;border-collapse:collapse;font-size:14.5px}
th,td{padding:9px 6px;text-align:right;border-bottom:1px solid var(--rule)}
th{font-size:12px;color:var(--mute);font-weight:500;text-align:right}
th:first-child,td:first-child{text-align:left}
tbody tr:hover{background:#F3F2EC}
.bar{display:block;height:5px;background:var(--brass-soft);border-radius:1px}
.bar i{display:block;height:100%;background:var(--brass);border-radius:1px}

/* lists */
.rows a{display:flex;justify-content:space-between;gap:14px;align-items:baseline;
  padding:12px 2px;border-bottom:1px solid var(--rule);text-decoration:none}
.rows a:hover{background:#F3F2EC}
.rows .nm{font-weight:500}
.rows .sub{font-size:12.5px;color:var(--mute);margin-left:8px;font-weight:400}
.rows .rt{color:var(--mute);font-size:14px;white-space:nowrap}

.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin:26px 0}
.card{border:1px solid var(--rule);border-radius:2px;padding:16px 16px 18px;text-decoration:none;display:block}
.card:hover{border-color:var(--brass)}
.card b{display:block;font-size:15.5px;letter-spacing:-0.02em}
.card span{display:block;font-size:13px;color:var(--mute);margin-top:5px}

.lede{font-size:18px;line-height:1.65;margin:26px 0 6px;letter-spacing:-0.02em}
.prose h3{font-size:19px;margin:38px 0 12px;letter-spacing:-0.035em}
.prose h4{font-size:16px;margin:26px 0 8px}
.prose p{margin:0 0 14px}
.prose ul{margin:0 0 16px;padding-left:20px}
.prose li{margin-bottom:5px}
.prose table{margin:18px 0 22px}
.prose code{font-family:"IBM Plex Mono",monospace;font-size:13.5px;
  background:#F1F0EA;padding:1px 5px;border-radius:2px}
.prose a{color:var(--brass);text-decoration:underline;text-underline-offset:2px}
.prose strong{font-weight:700}

footer{border-top:1px solid var(--rule);margin-top:60px;padding:22px var(--pad) 60px;
  max-width:760px;margin-left:auto;margin-right:auto}
footer p{margin:0 0 6px;font-size:12.5px;color:var(--mute);line-height:1.6}
footer .disc{margin-top:12px}

@media(max-width:560px){
  h1{font-size:25px}
  .fig .v{font-size:28px}
  .figs{gap:22px}
  main{padding-bottom:48px}
}
@media(prefers-reduced-motion:no-preference){
  .card,.rows a{transition:background .12s ease,border-color .12s ease}
}
"""


# ────────────────────────────────────────────────────────────
# 요약문 — 조건 분기로 문장 구조가 달라진다
# ────────────────────────────────────────────────────────────

FREQ_KO = {"quarterly": "분기마다", "semi": "반기마다", "annual": "연 1회"}


def summarize(s, d, series, quarters, flags):
    name, knd = s["name"], s["knd"]
    y, dps = d["latest_year"], d["latest_dps"]
    p1 = []

    freq = FREQ_KO.get(d["pay_freq"])
    if freq == "분기마다" and quarters:
        vals = [quarters.get(q) for q in (1, 2, 3, 4)]
        total = sum(v for v in vals if v)
        # 분기 합과 연간 공시값이 어긋나면 분해 결과를 문장에 쓰지 않는다
        consistent = (all(v for v in vals) and dps
                      and abs(total - dps) <= max(1.0, dps * 0.01))
        if consistent:
            if len({round(v) for v in vals[:3]}) == 1:
                p1.append(f"{nm(name, '은', '는')} 분기마다 배당하며, {y}년에는 "
                          f"1~3분기 각 {won(vals[0])}, 4분기 {won(vals[3])}을 지급해 "
                          f"연간 {won(dps)}이 됩니다.")
            else:
                p1.append(f"{nm(name, '은', '는')} 분기마다 배당하며, {y}년 분기별 "
                          "지급액은 " + ", ".join(won(v) for v in vals)
                          + f"으로 연간 {won(dps)}입니다.")
        else:
            p1.append(f"{nm(name, '은', '는')} 분기 배당을 실시하며, {y}년 연간 "
                      f"배당금은 {won(dps)}입니다.")
    elif freq == "반기마다":
        p1.append(f"{nm(name, '은', '는')} 반기마다 배당하며 {y}년 합계 "
                  f"{won(dps)}을 지급했습니다.")
    else:
        p1.append(f"{nm(name, '의', '의')} {y}년 배당금은 주당 {won(dps)}입니다.")

    if knd == "우선주":
        p1.append("우선주는 의결권이 없는 대신 보통주보다 배당이 많은 경우가 일반적입니다.")

    p2 = []
    g, streak = d["growth_years"], d["streak_years"]
    trunc = any(f["code"] == "history_truncated" for f in flags["info"])
    if g >= 5:
        p2.append(f"{g}년 연속으로 배당을 늘려 왔습니다.")
    elif g >= 2:
        p2.append(f"최근 {g}년 동안 배당이 매년 늘었습니다.")
    elif streak >= 5 and trunc:
        p2.append(f"{DATA_FLOOR}년 이후 한 해도 거르지 않고 배당했습니다.")
    elif streak >= 2:
        p2.append(f"{streak}년 연속 배당을 이어오고 있습니다.")
    else:
        p2.append("배당 이력이 길지 않아 추세를 판단하기 이릅니다.")

    dec = next((f for f in flags["info"] if f["code"] == "dps_decreased"), None)
    if dec:
        p2.append(f"다만 {dec['year']}년에는 배당금이 {won(dec['from'])}에서 "
                  f"{won(dec['to'])}으로 {abs(dec['pct']):g}% 줄었습니다. "
                  "특별배당이 있었던 해의 기저효과일 수 있어 원문 확인이 필요합니다.")

    p3 = []
    po, yl = d["latest_payout"], d["latest_yield"]
    if po is not None:
        if po >= 100:
            p3.append(f"배당성향은 {pct(po)}로, 그해 순이익보다 많은 금액을 배당했습니다.")
        elif po >= 80:
            p3.append(f"배당성향 {pct(po)}는 높은 편이라 이익이 줄면 배당도 조정될 수 있습니다.")
        elif po > 0:
            p3.append(f"배당성향은 {pct(po)}입니다.")
    if yl is not None and yl <= YIELD_CAP:
        p3.append(f"공시 기준 배당수익률은 {pct(yl)}입니다.")

    paras = [" ".join(p1), " ".join(p2)]
    if p3:
        paras.append(" ".join(p3))
    return "\n".join(f"<p>{esc(t)}</p>" for t in paras)


# ────────────────────────────────────────────────────────────
# 데이터 로드
# ────────────────────────────────────────────────────────────

def load(conn):
    rows = conn.execute(
        "SELECT s.stock_code, s.corp_code, s.corp_name, s.market, s.stock_knd,"
        " s.pref_of, d.* FROM derived d JOIN stock s USING(stock_code)").fetchall()

    items = []
    for r in rows:
        flags = json.loads(r["risk_flags"])

        series = {}
        for x in conn.execute(
            "SELECT bsns_year, stock_knd, dps, yield_pct FROM dividend_cumulative"
            " WHERE corp_code=? AND reprt_code='11011' ORDER BY bsns_year",
            (r["corp_code"],)):
            k = re.sub(r"\s+", "", str(x["stock_knd"] or ""))
            want = r["stock_knd"]
            ok = (("우선" in k or "종류주" in k) if want == "우선주"
                  else ("우선" not in k and "종류주" not in k))
            if ok and x["dps"] is not None:
                series[x["bsns_year"]] = (x["dps"], x["yield_pct"])

        quarters = {}
        for x in conn.execute(
            "SELECT quarter, stock_knd, dps FROM dividend_quarter"
            " WHERE corp_code=? AND bsns_year=?", (r["corp_code"], r["latest_year"])):
            k = re.sub(r"\s+", "", str(x["stock_knd"] or ""))
            want = r["stock_knd"]
            ok = (("우선" in k or "종류주" in k) if want == "우선주"
                  else ("우선" not in k and "종류주" not in k))
            if ok and x["dps"] is not None:
                quarters[x["quarter"]] = x["dps"]

        # 액면배당률 의심 판정
        par = conn.execute(
            "SELECT par_value FROM dividend_year WHERE corp_code=? AND bsns_year=?",
            (r["corp_code"], r["latest_year"])).fetchone()
        par = par["par_value"] if par else None
        if (par and par > 0 and r["latest_dps"] and r["latest_yield"]
                and abs(r["latest_dps"] / par * 100 - r["latest_yield"]) < 0.01):
            flags["warn"].append({"code": "par_rate_suspect"})

        items.append({
            "code": r["stock_code"], "name": r["corp_name"],
            "market": r["market"], "knd": r["stock_knd"], "pref_of": r["pref_of"],
            "row": r, "flags": flags, "series": series, "quarters": quarters,
            "slug": f"{r['stock_code']}-{slug(r['corp_name'])}",
        })
    return items


def pick_first_wave(items):
    """1차 색인 대상 선정."""
    sel = set()
    for it in items:
        if it["row"]["pay_freq"] in ("quarterly", "semi"):
            sel.add(it["code"])
        if (it["row"]["growth_years"] or 0) >= FIRST_WAVE_GROWTH_MIN:
            sel.add(it["code"])
    ranked = sorted(
        (i for i in items
         if i["row"]["latest_yield"] and i["row"]["latest_yield"] <= YIELD_CAP),
        key=lambda i: -i["row"]["latest_yield"])
    for it in ranked[:FIRST_WAVE_YIELD_TOP]:
        sel.add(it["code"])
    return sel


# ────────────────────────────────────────────────────────────
# 페이지
# ────────────────────────────────────────────────────────────

def rhythm_block(it):
    q = it["quarters"]
    freq = it["row"]["pay_freq"]
    cells = []
    for i in (1, 2, 3, 4):
        v = q.get(i)
        on = v is not None and v > 0
        amt = f'<span class="amt num">{v:,.0f}</span>' if on else ""
        cells.append(f'<div class="q{" on" if on else ""}">{amt}'
                     f'<span class="qk">{i}분기</span></div>')
    label = {"quarterly": "분기마다 지급", "semi": "반기마다 지급",
             "annual": "연 1회 지급", "unknown": "지급 시기 확인 불가"}.get(freq, "")
    note = ("분기별 금액은 각 분기보고서의 누계 배당금을 차감해 산출했습니다."
            if freq in ("quarterly", "semi")
            else "분기보고서에 별도 배당 기재가 없어 결산 배당으로 표시했습니다.")
    return f"""<section class="rhythm">
  <div class="cap">{it['row']['latest_year']}년 배당 지급 시기 · {label}</div>
  <div class="strip">{''.join(cells)}</div>
  <p class="note">{note}</p>
</section>"""


def history_table(it):
    ser = it["series"]
    if not ser:
        return ""
    mx = max((v[0] or 0) for v in ser.values()) or 1
    rows = []
    for y in sorted(ser, reverse=True):
        dps, yld = ser[y]
        w = (dps or 0) / mx * 100
        rows.append(
            f"<tr><td>{y}</td>"
            f'<td class="num">{won(dps)}</td>'
            f'<td class="num">{pct(yld)}</td>'
            f'<td style="width:34%"><span class="bar">'
            f'<i style="width:{w:.1f}%"></i></span></td></tr>')
    return f"""<h2>연도별 배당 이력</h2>
<table><thead><tr><th>사업연도</th><th>주당 배당금</th>
<th>배당수익률</th><th></th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>"""


def related_block(it, items, by_code):
    out = []
    if it["knd"] == "우선주" and it["pref_of"] and it["pref_of"] in by_code:
        out.append((by_code[it["pref_of"]], "같은 회사 보통주"))
    else:
        for o in items:
            if o["pref_of"] == it["code"]:
                out.append((o, "같은 회사 우선주"))
                break

    y = it["row"]["latest_yield"]
    if y and y <= YIELD_CAP:
        near = sorted(
            (o for o in items
             if o["code"] != it["code"] and o["row"]["latest_yield"]
             and o["row"]["latest_yield"] <= YIELD_CAP
             and o["row"]["pay_freq"] == it["row"]["pay_freq"]),
            key=lambda o: abs(o["row"]["latest_yield"] - y))[:4]
        for o in near:
            out.append((o, f"배당수익률 {pct(o['row']['latest_yield'])}"))

    if not out:
        return ""
    rows = "".join(
        f'<a href="../../stock/{o["slug"]}/index.html">'
        f'<span class="nm">{esc(o["name"])}'
        f'<span class="sub">{o["code"]}</span></span>'
        f'<span class="rt">{esc(tag)}</span></a>'
        for o, tag in out[:5])
    return f'<h2>함께 보면 좋은 종목</h2><div class="rows">{rows}</div>'


def build_stock(it, items, by_code, indexed):
    r, f = it["row"], it["flags"]
    badges = []
    for x in f["warn"]:
        t = FLAG_TEXT.get(x["code"], "")
        if t:
            badges.append(f'<div class="badge w">{esc(t)}</div>')
    for x in f["info"]:
        t = FLAG_TEXT.get(x["code"], "")
        if t:
            badges.append(f'<div class="badge i">{esc(t)}</div>')

    title = f"{it['name']} {it['code']} 배당금 · 배당수익률 | {SITE_NAME}"
    desc = (f"{it['name']}({it['code']}) {r['latest_year']}년 주당 배당금 "
            f"{won(r['latest_dps'])}, 배당수익률 {pct(r['latest_yield'])}, "
            f"배당성향 {pct(r['latest_payout'])}. DART 공시 기준 연도별 이력.")

    body = f"""<section class="head">
  <div class="eyebrow">{esc(it['market'])} · {esc(it['knd'])}</div>
  <h1>{esc(it['name'])}<span class="code num">{it['code']}</span></h1>
</section>

<div class="figs">
  <div class="fig"><div class="v num">{won(r['latest_dps'])}</div>
    <div class="l">{r['latest_year']}년 주당 배당금</div></div>
  <div class="fig"><div class="v num">{pct(r['latest_yield'])}</div>
    <div class="l">배당수익률</div></div>
  <div class="fig"><div class="v num">{pct(r['latest_payout'])}</div>
    <div class="l">배당성향</div></div>
</div>

{rhythm_block(it)}

<div class="summary">{summarize(it, r, it['series'], it['quarters'], f)}</div>

<div class="badges">{''.join(badges)}</div>

{history_table(it)}

{related_block(it, items, by_code)}
"""
    return page(title, desc, body, 2, indexed,
                f"/stock/{it['slug']}/")


def build_rank(key, title, lede, rows, note=""):
    items_html = "".join(
        f'<a href="../../stock/{o["slug"]}/index.html">'
        f'<span class="nm">{esc(o["name"])}'
        f'<span class="sub">{o["code"]} · {esc(o["knd"])}</span></span>'
        f'<span class="rt num">{esc(v)}</span></a>' for o, v in rows)
    body = f"""<section class="head"><h1>{esc(title)}</h1></section>
<p class="lede">{esc(lede)}</p>
<div class="rows">{items_html}</div>
{f'<p class="note" style="color:var(--mute);font-size:13.5px;margin-top:20px">{esc(note)}</p>' if note else ''}
"""
    return page(f"{title} | {SITE_NAME}", lede, body, 2, True, f"/rank/{key}/")


def build_home(items, first_wave):
    tot = len(items)
    q = [i for i in items if i["row"]["pay_freq"] == "quarterly"]
    top_g = sorted(items, key=lambda i: -(i["row"]["growth_years"] or 0))[:6]

    cards = "".join(
        f'<a class="card" href="rank/{k}/index.html"><b>{t}</b><span>{s}</span></a>'
        for k, t, s in [
            ("growth", "연속 증액", "배당을 매년 늘려온 기업"),
            ("quarterly", "분기배당", f"분기마다 지급하는 {len(q)}개 종목"),
            ("yield", "배당수익률", "공시 기준 수익률 순위"),
            ("streak", "무삭감", "배당을 줄이지 않은 기업"),
        ])

    rows = "".join(
        f'<a href="stock/{o["slug"]}/index.html">'
        f'<span class="nm">{esc(o["name"])}<span class="sub">{o["code"]}</span></span>'
        f'<span class="rt num">{o["row"]["growth_years"]}년 연속 증액</span></a>'
        for o in top_g)

    body = f"""<section class="head">
  <h1>배당은 언제, 얼마나 들어오나</h1>
</section>
<p class="lede">국내 상장사 {tot:,}개 종목의 배당 이력을 DART 공시 원문에서 모았습니다.
연간 금액뿐 아니라 <strong>분기별로 나눠 지급한 금액</strong>까지 분해해 보여줍니다.</p>

<div class="cards">{cards}</div>

<h2>배당을 가장 오래 늘려온 기업</h2>
<div class="rows">{rows}</div>

<h2>배당 투자 가이드</h2>
<div class="rows">{''.join(
    f'<a href="guide/{s}/index.html"><span class="nm">{esc(t)}</span></a>'
    for s, t in GUIDE_TOPICS)}</div>
"""
    return page(f"{SITE_NAME} · 국내 주식 배당금 조회", 
                f"국내 상장사 {tot:,}개 종목의 배당금, 배당수익률, 배당성향, "
                "분기별 배당 지급액을 DART 공시 기준으로 조회합니다.",
                body, 0, True, "/")


def guide_stats(items):
    """
    가이드 본문에서 {{키}} 형태로 쓰면 빌드 시점의 실제 수치로 치환된다.
    글에 적은 숫자가 데이터 갱신 후에도 어긋나지 않게 하기 위한 장치.
    """
    def n(f):
        return sum(1 for i in items if f(i))

    return {
        "quarterly_count": f"{n(lambda i: i['row']['pay_freq'] == 'quarterly'):,}",
        "quarterly_common": f"{n(lambda i: i['row']['pay_freq'] == 'quarterly' and i['knd'] == '보통주'):,}",
        "semi_count": f"{n(lambda i: i['row']['pay_freq'] == 'semi'):,}",
        "annual_count": f"{n(lambda i: i['row']['pay_freq'] == 'annual'):,}",
        "total_stocks": f"{len(items):,}",
        "payout_over_100": f"{n(lambda i: any(x['code'] == 'payout_over_100' for x in i['flags']['warn'])):,}",
        "par_rate_suspect": f"{n(lambda i: any(x['code'] == 'par_rate_suspect' for x in i['flags']['warn'])):,}",
        "yield_outlier": f"{n(lambda i: any(x['code'] == 'yield_outlier' for x in i['flags']['warn'])):,}",
        "data_from": str(DATA_FLOOR),
        "latest_year": str(max((i["row"]["latest_year"] for i in items), default="")),
    }


def build_guides(items):
    made = []
    os.makedirs(GUIDE_SRC, exist_ok=True)
    stats = guide_stats(items)
    used_vars = set()

    for s, t in GUIDE_TOPICS:
        src = os.path.join(GUIDE_SRC, f"{s}.md")
        if not os.path.exists(src):
            continue
        text = open(src, encoding="utf-8").read().strip()
        if len(text) < 400:
            log(f"    [건너뜀] {s}.md — 400자 미만 ({len(text)}자)")
            continue

        # {{키}} 치환
        for k, v in stats.items():
            if "{{" + k + "}}" in text:
                used_vars.add(k)
                text = text.replace("{{" + k + "}}", v)
        left = re.findall(r"\{\{(\w+)\}\}", text)
        if left:
            log(f"    [경고] {s}.md — 알 수 없는 변수 {sorted(set(left))}")

        # 원고 첫 줄의 H1 을 제목으로 사용 (없으면 GUIDE_TOPICS 값)
        h1 = next((re.sub(r"^#\s+", "", l).strip() for l in text.splitlines()
                   if l.strip().startswith("# ")), None)
        title = h1 or t

        first = next((l.strip() for l in text.splitlines()
                      if l.strip() and not l.startswith("#")), title)
        desc = re.sub(r"[*`\[\]]|\(/[^)]*\)", "", first)[:150]
        body = (f'<section class="head"><h1>{esc(title)}</h1></section>'
                f'<div class="prose">{md(text)}</div>')
        write(os.path.join(OUT, "guide", s, "index.html"),
              page(f"{title} | {SITE_NAME}", desc, body, 2, True, f"/guide/{s}/"))
        made.append((s, title))

    if used_vars:
        log(f"    치환된 변수: {sorted(used_vars)}")

    rows = "".join(
        f'<a href="{s}/index.html"><span class="nm">{esc(t)}</span></a>'
        for s, t in made)
    body = (f'<section class="head"><h1>배당 투자 가이드</h1></section>'
            f'<p class="lede">배당 데이터를 읽을 때 알아야 할 것들을 정리했습니다.</p>'
            f'<div class="rows">{rows}</div>')
    write(os.path.join(OUT, "guide", "index.html"),
          page(f"배당 투자 가이드 | {SITE_NAME}",
               "배당기준일, 배당소득세, 배당성향 등 배당 투자에 필요한 기본 개념 정리.",
               body, 1, True, "/guide/"))
    return made


# ────────────────────────────────────────────────────────────

def main():
    conn = db()
    section("사이트 생성")

    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)
    write(os.path.join(OUT, "style.css"), CSS.strip())

    items = load(conn)
    by_code = {i["code"]: i for i in items}
    first_wave = pick_first_wave(items)
    log(f"  전체 종목   : {len(items):,}")
    log(f"  1차 색인    : {len(first_wave):,}   (나머지는 noindex)")

    urls = ["/"]

    for it in items:
        idx = it["code"] in first_wave
        write(os.path.join(OUT, "stock", it["slug"], "index.html"),
              build_stock(it, items, by_code, idx))
        if idx or PUBLISH_ALL:
            urls.append(f"/stock/{it['slug']}/")
    log(f"  종목 페이지 : {len(items):,}")

    # 랭킹
    ok = [i for i in items
          if i["row"]["latest_yield"] is None or i["row"]["latest_yield"] <= YIELD_CAP]

    g = sorted((i for i in items if (i["row"]["growth_years"] or 0) >= 2),
               key=lambda i: (-(i["row"]["growth_years"] or 0),
                              -(i["row"]["latest_yield"] or 0)))[:100]
    write(os.path.join(OUT, "rank", "growth", "index.html"),
          build_rank("growth", "연속 증액 배당주",
                     "배당금을 전년보다 늘린 해가 연속으로 이어진 기업입니다. "
                     "금액이 유지만 된 해는 연속에서 끊깁니다.",
                     [(o, f"{o['row']['growth_years']}년") for o in g]))

    q = sorted((i for i in items if i["row"]["pay_freq"] == "quarterly"),
               key=lambda i: -(i["row"]["latest_yield"] or 0))
    write(os.path.join(OUT, "rank", "quarterly", "index.html"),
          build_rank("quarterly", "분기배당 종목",
                     "1년에 네 번 배당하는 기업입니다. 분기보고서의 누계 배당금을 "
                     "차감해 분기별 지급액까지 분해했습니다.",
                     [(o, pct(o["row"]["latest_yield"])) for o in q]))

    y = sorted((i for i in ok if i["row"]["latest_yield"]),
               key=lambda i: -i["row"]["latest_yield"])[:100]
    write(os.path.join(OUT, "rank", "yield", "index.html"),
          build_rank("yield", "배당수익률 순위",
                     "사업보고서에 공시된 현금배당수익률 기준입니다. "
                     "현재 주가 기준이 아니라 공시 시점 기준값입니다.",
                     [(o, pct(o["row"]["latest_yield"])) for o in y],
                     f"공시값이 {YIELD_CAP:g}%를 넘는 종목은 기재 기준이 다를 수 있어 "
                     "순위에서 제외했습니다."))

    st = sorted(items, key=lambda i: (-(i["row"]["streak_years"] or 0),
                                      -(i["row"]["latest_yield"] or 0)))[:100]
    write(os.path.join(OUT, "rank", "streak", "index.html"),
          build_rank("streak", "배당 무삭감 기업",
                     f"{DATA_FLOOR}년 이후 배당을 줄이지 않고 이어온 기업입니다. "
                     "그 이전 기록은 제공 범위 밖이라 실제 기간은 더 길 수 있습니다.",
                     [(o, f"{o['row']['streak_years']}년") for o in st]))

    urls += [f"/rank/{k}/" for k in ("growth", "quarterly", "yield", "streak")]
    log("  랭킹 페이지 : 4")

    guides = build_guides(items)
    urls.append("/guide/")
    urls += [f"/guide/{s}/" for s, _ in guides]
    log(f"  가이드      : {len(guides)} / {len(GUIDE_TOPICS)}"
        + ("   [!] 부족 — AdSense 승인에 불리합니다" if len(guides) < 4 else ""))

    write(os.path.join(OUT, "index.html"), build_home(items, first_wave))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    write(os.path.join(OUT, "sitemap.xml"),
          '<?xml version="1.0" encoding="UTF-8"?>\n'
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
          + "".join(f"<url><loc>{BASE_URL}{u}</loc>"
                    f"<lastmod>{today}</lastmod></url>\n" for u in urls)
          + "</urlset>\n")

    write(os.path.join(OUT, "robots.txt"),
          f"User-agent: *\nAllow: /\n\nSitemap: {BASE_URL}/sitemap.xml\n")

    section("완료")
    log(f"  출력       : ./{OUT}/")
    log(f"  사이트맵   : {len(urls):,} URL")
    log(f"  PUBLISH_ALL: {PUBLISH_ALL}")
    log()
    log("  로컬 확인:  python -m http.server 8000 --directory site")
    log("             http://localhost:8000")


if __name__ == "__main__":
    main()