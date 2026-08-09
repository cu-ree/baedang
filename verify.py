#!/usr/bin/env python3
"""
verify.py — 배포 전 최종 점검

자동 배포는 사람이 안 보는 사이에 돌아간다.
API 장애나 파싱 오류로 빈 사이트가 만들어졌을 때
그대로 배포되면 몇 주 동안 모를 수 있다.

기준 미달이면 종료 코드 1 을 반환해 배포를 막는다.

    python verify.py
"""

import html
import os
import re
import sys

SITE = "docs"

MIN_STOCK_PAGES = 1200      # 정상값 약 1,640
MIN_SITEMAP_URLS = 150      # 정상값 약 235
MIN_GUIDES = 4              # AdSense 승인 최소선
MIN_HTML_BYTES = 900        # 이보다 작으면 껍데기

# 파일별 최소 크기. robots.txt 는 네 줄짜리라 원래 작다.
MIN_SIZE = {
    "index.html": 1000,
    "style.css": 1000,
    "sitemap.xml": 200,
    "robots.txt": 30,
}


def fail(msg):
    print(f"  [실패] {msg}")
    return 1


def main():
    print("배포 전 점검")
    errors = 0

    # 1. 핵심 파일
    for f, minimum in MIN_SIZE.items():
        p = os.path.join(SITE, f)
        if not os.path.exists(p):
            errors += fail(f"{f} 없음")
        elif os.path.getsize(p) < minimum:
            errors += fail(f"{f} 크기 이상 ({os.path.getsize(p)}B < {minimum}B)")
    print("  핵심 파일 확인")

    # 2. 종목 페이지 수
    stock_dir = os.path.join(SITE, "stock")
    n_stock = len(os.listdir(stock_dir)) if os.path.isdir(stock_dir) else 0
    if n_stock < MIN_STOCK_PAGES:
        errors += fail(f"종목 페이지 {n_stock} < {MIN_STOCK_PAGES}")
    else:
        print(f"  종목 페이지 {n_stock:,}")

    # 3. 랭킹
    for r in ("growth", "quarterly", "yield", "streak"):
        if not os.path.exists(os.path.join(SITE, "rank", r, "index.html")):
            errors += fail(f"랭킹 페이지 없음: {r}")
    print("  랭킹 페이지 4")

    # 4. 가이드
    gdir = os.path.join(SITE, "guide")
    guides = [d for d in (os.listdir(gdir) if os.path.isdir(gdir) else [])
              if os.path.isdir(os.path.join(gdir, d))]
    if len(guides) < MIN_GUIDES:
        errors += fail(f"가이드 {len(guides)} < {MIN_GUIDES}")
    else:
        print(f"  가이드 {len(guides)}")

    # 5. 사이트맵
    sm = os.path.join(SITE, "sitemap.xml")
    if os.path.exists(sm):
        text = open(sm, encoding="utf-8").read()
        urls = re.findall(r"<loc>(.*?)</loc>", text)
        if len(urls) < MIN_SITEMAP_URLS:
            errors += fail(f"사이트맵 URL {len(urls)} < {MIN_SITEMAP_URLS}")
        else:
            print(f"  사이트맵 URL {len(urls):,}")
        bad = [u for u in urls if not u.startswith("https://")
               or "localhost" in u or u.startswith("https:///")]
        if bad:
            errors += fail(f"잘못된 URL {len(bad)}개 — 예: {bad[0]}")

    # 6. 빈 껍데기 페이지
    empties = []
    for root, _, files in os.walk(SITE):
        for f in files:
            if f.endswith(".html"):
                p = os.path.join(root, f)
                if os.path.getsize(p) < MIN_HTML_BYTES:
                    empties.append(p)
    if empties:
        errors += fail(f"내용 없는 페이지 {len(empties)}개 — 예: {empties[0]}")
    else:
        print("  빈 페이지 없음")

    # 7. 대표 종목 스팟체크
    spot = {"005930": "삼성전자", "000100": "유한양행",
            "033780": "KT&G", "005380": "현대차"}
    found = 0
    listing = os.listdir(stock_dir) if os.path.isdir(stock_dir) else []
    for code, name in spot.items():
        hit = [d for d in listing if d.startswith(code + "-")]
        if hit:
            raw = open(os.path.join(stock_dir, hit[0], "index.html"),
                       encoding="utf-8").read()
            # KT&G -> KT&amp;G 로 저장되므로 엔티티를 되돌린 뒤 비교
            text = html.unescape(raw)
            if name in text and "원" in text:
                found += 1
                continue
            errors += fail(f"스팟체크 내용 불일치: {code} {name} ({hit[0]})")
        else:
            errors += fail(f"스팟체크 페이지 없음: {code} {name}")
    if found == len(spot):
        print(f"  스팟체크 {found}/{len(spot)}")

    print()
    if errors:
        print(f"점검 실패 — {errors}건. 배포를 중단합니다.")
        return 1
    print("점검 통과. 배포 가능.")
    return 0


if __name__ == "__main__":
    sys.exit(main())