# GitHub Actions + Backblaze B2 셋업 가이드

매일 KST 10시 (UTC 01시) 자동 스크래핑. 노트북 의존성 0.

---

## 1. Backblaze B2 셋업 (~10분)

### 1-1. 계정 생성
- https://www.backblaze.com/sign-up/cloud-storage
- 이메일만으로 가입 가능 (카드 X)

### 1-2. 버킷 생성
- B2 Console → **Buckets** → **Create a Bucket**
- 이름: `yt-daily-jh` (또는 원하는 unique 이름, 소문자/하이픈만)
- Files in Bucket are: **Private**
- Default Encryption: **Enable**
- Object Lock: **Disable**
- Create

### 1-3. Application Key 생성
- **App Keys** → **Add a New Application Key**
- Name of Key: `github-actions`
- Allow access to Bucket(s): **선택해서 `yt-daily-jh`만** (안전)
- Type of Access: **Read and Write**
- Allow List All Bucket Names: 체크 안 함
- File name prefix: 비움
- Duration: 비움 (영구)
- **Create New Key**

⚠️ **즉시 복사**: `keyID`와 `applicationKey` 둘 다. **`applicationKey`는 다시 못 봄.**

---

## 2. 로컬 DB를 B2에 처음 업로드 (~5분)

```bash
# b2 CLI 설치
pip3 install b2

# 인증 (1회만, 위에서 받은 키)
b2 account authorize "<keyID>" "<applicationKey>"

# 로컬 DB → B2 (현재 ~440MB, 3-5분 소요)
b2 file upload yt-daily-jh ~/youtube_daily_db/youtube_daily.db youtube_daily.db

# 확인
b2 ls b2://yt-daily-jh/
```

---

## 3. GitHub repo 생성 (~5분)

### 3-1. 새 public repo
- https://github.com/new
- 이름: `youtube-daily-scraping` (또는 원하는 이름)
- Visibility: **Public** (Actions 무제한 사용 위해)
- README, .gitignore 추가 안 함 (이미 있음)
- Create

### 3-2. 로컬 코드 push

```bash
cd "/Users/jhyunlee/Library/CloudStorage/Dropbox/바탕화면/Gradschool/year5/youtube_daily_scraping"

git init
git branch -m main
git add scraper/ .github/ CLOUD_SETUP.md STATUS.md
git status   # ★ youtube_daily.db, .api_key가 staged되지 않은지 반드시 확인

git commit -m "Initial: scraper + workflow + setup guide"
git remote add origin https://github.com/<your-username>/<repo-name>.git
git push -u origin main
```

⚠️ **commit 전에 `git status`로 DB·.api_key가 빠졌는지 꼭 확인.** `.gitignore`에 있긴 하지만 한 번 더.

---

## 4. GitHub Secrets 등록 (~3분)

repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

다음 4개 등록:

| Name | Value |
|---|---|
| `YOUTUBE_API_KEY` | (YouTube Data API v3 키) |
| `B2_KEY_ID` | Backblaze에서 받은 keyID |
| `B2_APPLICATION_KEY` | Backblaze applicationKey |
| `B2_BUCKET_NAME` | `yt-daily-jh` (위에서 만든 버킷 이름) |

---

## 5. 첫 수동 실행 (~5-10분)

- repo → **Actions** 탭
- 왼쪽 **Daily YouTube Scrape** 클릭
- 오른쪽 **Run workflow** → **Run workflow** 버튼

진행 로그를 실시간으로 볼 수 있음. 첫 실행에서:
1. 코드 체크아웃 (10초)
2. Python 셋업 + 의존성 설치 (30초)
3. B2에서 DB 다운로드 (1-2분, 440MB)
4. **스크랩 실행** (1-3시간)
5. B2에 DB 업로드 (1-2분)

성공하면 매일 KST 10시 자동 실행 시작.

---

## 6. 모니터링

### 실행 결과 보기
- repo → Actions 탭 → 가장 위 run 클릭 → 단계별 로그

### 실패 시 이메일 알림
- GitHub 기본값: workflow 실패 시 자동 이메일
- 끄고 싶으면: Settings → Notifications → Actions

### 로컬에서 최신 DB 받아보기
```bash
b2 file download "b2://yt-daily-jh/youtube_daily.db" ~/youtube_daily_db/youtube_daily.db
```

---

## 7. 비용

| 항목 | 사용량 | 비용 |
|---|---|---|
| GitHub Actions (public) | 일 3시간 × 30일 = 90시간 | $0 (무제한) |
| B2 storage | DB 440MB + 스냅샷 (선택) | $0 (10GB 무료티어 안) |
| B2 download | 매일 440MB × 30 = 13GB/월 | $0 (1GB/일 무료) ⚠️ 약간 초과 가능 |
| B2 API calls | 무시 가능 | $0 |

⚠️ **B2 download 무료티어**: 매일 1GB. 우리는 매일 440MB downalod (workflow run에서) + 사용자가 가끔 받기. 무료티어 안쪽에서 안정적.

만약 DB가 2GB 넘어가면 (1~2년 후 가능) B2 download 비용 약간 발생 ($0.01/GB). 미미.

---

## 8. 로컬 launchd 정리 (선택)

GitHub Actions로 옮긴 후 로컬은 더 이상 필요 없음:

```bash
launchctl unload ~/Library/LaunchAgents/com.jhyun.youtube_daily.plist
rm ~/Library/LaunchAgents/com.jhyun.youtube_daily.plist
```

(원하면 로컬도 병행해서 데이터 검증용 redundancy로 둬도 됨. API quota는 같은 키 공유.)

---

## 트러블슈팅

**"No existing DB on B2"**: 2번 단계 (초기 업로드) 안 했음. b2 CLI로 수동 업로드.

**"Insufficient permissions"**: Application Key가 read-only이거나 다른 버킷에만 권한. 새로 만들기.

**Workflow 안 돌아감**: repo → Actions 탭에서 "I understand my workflows, go ahead and enable them" 한 번 눌러야 함 (처음 한 번).

**Cron이 정시에 안 돌아감**: GitHub Actions cron은 ±15분 지연 정상 (트래픽 따라). 정확한 시간 보장 X.

**API quota 초과**: scrape_log에 `status='partial'`. 다음 날 자동 재개됨.
