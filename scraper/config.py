"""
config.py — 환경설정
- API 키는 env var YOUTUBE_API_KEY 또는 파일 .api_key에서
- 경로, 윈도우, batch 사이즈
"""
import os

BASE = os.path.dirname(os.path.abspath(__file__))

# DB는 로컬 디스크 (Dropbox 클라우드 동기화 영역 밖) — SQLite WAL 모드와 cloud sync 충돌 방지
DB_DIR = os.environ.get("YOUTUBE_DB_DIR", os.path.expanduser("~/youtube_daily_db"))
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "youtube_daily.db")

LOG_DIR = os.path.join(BASE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def get_api_key() -> str:
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if key:
        return key
    keyfile = os.path.join(BASE, ".api_key")
    if os.path.exists(keyfile):
        with open(keyfile) as f:
            return f.read().strip()
    raise RuntimeError("YOUTUBE_API_KEY env var 또는 scraper/.api_key 파일 필요")


# ── 스크래핑 모집단 ──────────────────────────────────────────────
# 매일 추적할 채널 — DB의 channels 테이블에서 필터링
# True면 YT∩PB 272개만, False면 channels 전체 (2006개)
ACTIVE_CHANNELS_ONLY_INTERSECTION = True

# ── 영상 추적 범위 ────────────────────────────────────────────────
# 영상 stats를 매일 업데이트할 윈도우 (game-day 기준)
# 너무 오래된 영상은 변화 거의 없으므로 quota 절약
ACTIVE_VIDEO_LOOKBACK_DAYS = 365      # 게시일 < N일 전 영상만 매일 갱신

# 임베드 재파싱은 더 좁게 — Shorts만, 최근 90일
EMBED_REPARSE_LOOKBACK_DAYS = 180

# ── API 호출 파라미터 ────────────────────────────────────────────
YT_API_BASE = "https://www.googleapis.com/youtube/v3"
BATCH_VIDEOS = 50                     # videos.list 한 콜 최대
BATCH_CHANNELS = 50                   # channels.list 한 콜 최대
MAX_RETRIES = 5
BACKOFF_BASE = 1.5

# ── HTML 스크래핑 (임베드 재파싱) ────────────────────────────────
HTML_TIMEOUT = 15
HTML_THROTTLE_SEC = 0.3               # 요청 사이 sleep

# ── Quota 가드 ──────────────────────────────────────────────────
DAILY_QUOTA_LIMIT = 10000             # YouTube API 기본
QUOTA_SAFETY_MARGIN = 500             # 이만큼 남으면 중단
