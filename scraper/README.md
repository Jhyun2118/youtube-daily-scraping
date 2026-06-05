# YouTube 일별 스크래퍼

매일 1회 자동 실행으로 272개 채널의 영상-day 패널을 누적 구축.

## 구조

```
scraper/
├── config.py            # 경로·quota·윈도우 설정
├── schema.sql           # SQLite 테이블 정의
├── seed_from_csv.py     # 1회 실행: 기존 CSV → DB
├── scrape_daily.py      # 매일 실행 본체
├── README.md            # this
├── .api_key             # YouTube API 키 (gitignored)
└── logs/                # 일별 실행 로그
```

## DB 위치

**중요**: DB는 `~/youtube_daily_db/youtube_daily.db` (로컬 디스크). Dropbox 동기화 영역에 두면 SQLite WAL 모드가 충돌해서 corruption 발생.

다른 위치 쓰려면 환경변수:
```bash
export YOUTUBE_DB_DIR=/path/to/local/dir
```

## 초기 셋업 (1회)

```bash
cd scraper
# API 키 파일 생성 (또는 export YOUTUBE_API_KEY=...)
echo "AIza...your_key..." > .api_key
chmod 600 .api_key

# DB 초기 시드 (기존 CSV → SQLite, ~3분)
python3 seed_from_csv.py
```

결과:
- 2,006 channels
- 365,375 videos
- 1,141,112 channel-day rows (Playboard 2년치)
- 365,375 video-day rows (스냅샷)
- 88,319 embed edges

## 일별 실행

```bash
python3 scrape_daily.py                   # 전체 (272채널)
python3 scrape_daily.py --limit 5         # 5채널만 (테스트)
python3 scrape_daily.py --dry-run         # API 콜 안 함
python3 scrape_daily.py --skip-embeds     # HTML 임베드 파싱 스킵
```

### 무엇이 일어나는가
1. **Stage 1** 채널 stats + 신규 업로드 발견
   - 272 채널 batch로 channels.list
   - 각 채널의 uploads playlist 1페이지(50개) 확인
   - DB에 없는 video_id = 신규 → videos.list로 메타 가져옴
   - `channels_daily`, `videos_daily`, `videos`에 upsert
2. **Stage 2** 활성 영상 stats 갱신
   - 게시일 < 365일인 영상만 (config에서 조정 가능)
   - 50개씩 videos.list 배치
   - `videos_daily`에 upsert
3. **Stage 3** Shorts 임베드 재파싱
   - is_shorts=1 AND 게시일<180일인 Shorts에 대해 HTML GET
   - `reelMultiFormatLinkViewModel` 블록에서 creator-set 링크카드 추출
   - 변경 감지 → `embeds`(현재 상태) + `embeds_daily`(매일 스냅샷) 갱신

### Quota 예상
- 272 채널 전체 실행: 약 1,500-2,500 units (YouTube API 일일 한도 10,000)

## launchd로 매일 자동 실행 (macOS)

`~/Library/LaunchAgents/com.jhyun.youtube_daily.plist` 생성:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.jhyun.youtube_daily</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/python3</string>
        <string>/Users/jhyunlee/Library/CloudStorage/Dropbox/바탕화면/Gradschool/year5/youtube_daily_scraping/scraper/scrape_daily.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/jhyunlee/Library/CloudStorage/Dropbox/바탕화면/Gradschool/year5/youtube_daily_scraping/scraper</string>

    <!-- 매일 새벽 4시 (UTC 19시) 실행 -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>4</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>

    <!-- 슬립 중에 시간이 지나갔으면 깨어났을 때 실행 -->
    <key>RunAtLoad</key>
    <false/>

    <key>StandardOutPath</key>
    <string>/Users/jhyunlee/Library/CloudStorage/Dropbox/바탕화면/Gradschool/year5/youtube_daily_scraping/scraper/logs/launchd_out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/jhyunlee/Library/CloudStorage/Dropbox/바탕화면/Gradschool/year5/youtube_daily_scraping/scraper/logs/launchd_err.log</string>

    <!-- 환경변수: API 키 -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>YOUTUBE_API_KEY</key>
        <string>YOUR_API_KEY_HERE</string>
    </dict>
</dict>
</plist>
```

### 등록
```bash
# python3 경로 확인
which python3
# 결과를 plist의 ProgramArguments 첫 줄에 반영

# plist 로드 (재부팅 후에도 살아있음)
launchctl load ~/Library/LaunchAgents/com.jhyun.youtube_daily.plist

# 즉시 한 번 실행해서 테스트
launchctl start com.jhyun.youtube_daily

# 로그 확인
tail -f scraper/logs/launchd_out.log
```

### 해제
```bash
launchctl unload ~/Library/LaunchAgents/com.jhyun.youtube_daily.plist
```

### 트러블슈팅
- **실행 안 됨**: `launchctl list | grep youtube` 으로 등록 여부 확인
- **권한 오류**: System Settings → Privacy & Security → Full Disk Access에 Terminal 추가
- **Python 모듈 없음**: plist에 `PATH` 환경변수 추가하거나 venv의 절대경로 사용
- **Mac 슬립 중**: StartCalendarInterval은 슬립 중 트리거를 놓치지만 깨어나면 자동 catch-up 안 함. 매일 같은 시각에 깨워두거나 KeepAlive + 별도 스케줄러 (cron, anacron) 검토.

### cron 대안 (단순)
```bash
crontab -e
# 추가:
0 4 * * * cd /Users/.../scraper && YOUTUBE_API_KEY=... /usr/local/bin/python3 scrape_daily.py >> logs/cron.log 2>&1
```

## 모니터링

```bash
# 최근 실행 로그
sqlite3 ~/youtube_daily_db/youtube_daily.db \
  "SELECT scrape_date, started_at_utc, ended_at_utc, status, n_channels_done, n_videos_updated, n_new_videos, n_embed_changes, quota_units_used FROM scrape_log ORDER BY run_id DESC LIMIT 10"

# 일별 누적량
sqlite3 ~/youtube_daily_db/youtube_daily.db \
  "SELECT scrape_date, COUNT(*) FROM videos_daily GROUP BY scrape_date ORDER BY scrape_date DESC LIMIT 30"
```

## 알려진 한계

1. **임베드 변경 감지 정확도**: HTML 파싱이 YouTube 페이지 구조 변경에 취약. 월 1회는 검증 필요.
2. **Quota 차단 시**: 일별 10k unit 한도 도달 시 partial로 종료. 다음 날 자동 재개.
3. **삭제된 영상**: videos.list 응답에 없으면 미감지. 별도 검증 로직 필요.
4. **PB 채널 확장**: 현재는 YT 카테고리 라벨 있는 272개만. 전체 2,006개로 확장하려면 `config.ACTIVE_CHANNELS_ONLY_INTERSECTION = False`.
