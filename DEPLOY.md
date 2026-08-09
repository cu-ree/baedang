# 배포 절차 — baedang.co.kr

순서대로 진행하세요. 총 1~2시간.

---

## 0. BASE_URL 수정

`step5_build.py` 상단:

```python
BASE_URL = "https://baedang.co.kr"     # 끝에 / 없이
```

수정 후 다시 빌드하고 점검합니다.

```powershell
python step5_build.py
python verify.py
```

`점검 통과` 가 나와야 다음으로 갑니다.

---

## 1. 파일 배치

작업 폴더가 아래 구조가 되도록 정리합니다.

```
D:\python\TEST\
  .github\workflows\build.yml      ← build.yml 을 이 경로로
  guides\*.md                       가이드 6편
  common.py
  step1_stocks.py
  step2_annual.py
  step3_quarter.py
  step4_derive.py
  step5_build.py
  verify.py
  requirements.txt
  .gitignore
  dividend.db                       커밋함 (재수집 방지)
  raw\                              커밋 안 함
  site\                             커밋 안 함
```

`.github\workflows\` 폴더는 직접 만드셔야 합니다.

---

## 2. GitHub 저장소 (Private)

```powershell
git init
git add .
git commit -m "init: 배당 데이터 사이트"
```

github.com 에서 **New repository** → 이름 `baedang` → **Private** 선택 → 생성.

```powershell
git remote add origin https://github.com/{계정}/baedang.git
git branch -M main
git push -u origin main
```

> **확인:** 저장소에 `dividend.db` 는 있고 `raw/`, `site/` 는 없어야 합니다.
> API 키가 코드에 들어가 있지 않은지도 한 번 보세요.

---

## 3. Cloudflare Pages 프로젝트

1. dash.cloudflare.com 가입 · 로그인
2. 좌측 **Workers & Pages** → **Create** → **Pages** → **Upload assets**
3. 프로젝트 이름 **`baedang`** (워크플로의 `--project-name` 과 반드시 일치)
4. 로컬 `site` 폴더를 통째로 드래그해서 업로드 → **Deploy**

몇 분 뒤 `baedang.pages.dev` 로 사이트가 뜹니다. **여기서 한 번 눈으로 확인하세요.**

---

## 4. 도메인 연결

**4-1. Cloudflare 에 도메인 추가**

좌측 **Websites** → **Add a site** → `baedang.co.kr` → Free 플랜 →
안내에 나오는 **네임서버 2개**를 메모합니다. (`xxx.ns.cloudflare.com` 형태)

**4-2. 등록업체에서 네임서버 변경**

도메인 구매처(가비아·후이즈 등) 관리 페이지 →
**네임서버 설정** → 위에서 받은 2개로 교체 → 저장.

> 전파에 보통 1~6시간, 최대 하루. 조급해하지 마세요.

**4-3. Pages 에 커스텀 도메인 연결**

Cloudflare 에서 도메인이 **Active** 로 바뀐 뒤:

**Workers & Pages** → `baedang` → **Custom domains** → **Set up a domain**
→ `baedang.co.kr` 입력 → 연결.

`www` 도 쓰실 거면 같은 방법으로 `www.baedang.co.kr` 을 추가합니다.

SSL 인증서는 자동 발급됩니다.

---

## 5. 시크릿 등록

**5-1. Cloudflare API 토큰**

dash.cloudflare.com → 우측 상단 프로필 → **API Tokens** → **Create Token**
→ **Custom token**

```
Permissions:  Account  →  Cloudflare Pages  →  Edit
Account Resources:  본인 계정 선택
```

생성된 토큰을 복사합니다. **이 화면을 벗어나면 다시 볼 수 없습니다.**

**Account ID** 는 Cloudflare 대시보드 우측 하단 또는 URL 의
`dash.cloudflare.com/{여기가 Account ID}` 에서 확인합니다.

**5-2. GitHub 에 등록**

저장소 → **Settings** → **Secrets and variables** → **Actions** →
**New repository secret** 로 3개 등록:

| 이름 | 값 |
|---|---|
| `DART_API_KEY` | OpenDART 인증키 |
| `CLOUDFLARE_API_TOKEN` | 위에서 만든 토큰 |
| `CLOUDFLARE_ACCOUNT_ID` | Account ID |

---

## 6. 자동화 시험

저장소 → **Actions** 탭 → `build-and-deploy` →
**Run workflow** → **Run workflow** 클릭.

전체 수집이 돌아가므로 **60~120분** 걸립니다.
초록색 체크가 뜨면 자동화가 완성된 것입니다.

> 실패하면 로그에서 어느 단계인지 확인하세요.
> 대부분 시크릿 이름 오타이거나 `--project-name` 불일치입니다.

이후로는 **매주 월요일 18시(KST)에 자동 실행**됩니다.

---

## 7. 검색엔진 등록

**Google Search Console**

search.google.com/search-console → **속성 추가** → **URL 접두어** →
`https://baedang.co.kr` → HTML 태그 또는 DNS 로 소유권 확인
(Cloudflare 를 쓰므로 DNS 방식이 편합니다) →
**Sitemaps** → `sitemap.xml` 제출

**네이버 서치어드바이저**

searchadvisor.naver.com → **웹마스터 도구** → 사이트 등록 →
소유 확인 → **요청 → 사이트맵 제출** → `sitemap.xml`

> costcheck.kr 실적을 보면 **네이버가 구글보다 먼저 반응할 가능성이 높습니다.**
> 초기 지표는 이쪽을 보세요.

**다음(Daum)**

register.search.daum.net 에서 사이트 등록.

---

## 8. 그리고 손을 떼세요

```
지금        배포 완료
2~4주 뒤     AdSense 검토 요청 (5분)
6개월 뒤     트래픽 확인 → 월 1만 PV 미만이면 종료, 넘으면 유지
```

**그 사이에는 아무것도 하지 않습니다.**
매주 트래픽을 확인하는 것이 가장 큰 시간 낭비입니다.

AdSense 승인 후에는 `step5_build.py` 의

```python
PUBLISH_ALL = True
```

로 바꾸고 push 하면 1,640 페이지가 전부 색인 허용으로 열립니다.

---

## 문제가 생기면

| 증상 | 확인할 곳 |
|---|---|
| Actions 가 step1 에서 실패 | `DART_API_KEY` 시크릿 이름·값 |
| 배포 단계에서 실패 | `--project-name` 이 `baedang` 인지, 토큰 권한이 Pages Edit 인지 |
| `verify.py` 에서 중단 | 데이터 수집이 덜 된 것. 로그의 실패 건수 확인 |
| 도메인이 안 열림 | 네임서버 전파 대기 (최대 24시간) |
| 사이트맵 오류 | `BASE_URL` 이 실제 도메인과 같은지 |
