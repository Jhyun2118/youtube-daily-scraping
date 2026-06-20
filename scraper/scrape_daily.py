"""
scrape_daily.py — 매일 1회 실행
1. 활성 채널 리스트 가져옴
2. 채널별 channels.list → channels_daily 갱신, 신규 업로드 발견
3. 활성 영상 → videos.list 배치 → videos_daily 갱신
4. 활성 Shorts → HTML 재파싱 → embeds_daily / embeds 갱신
5. scrape_log에 결과 기록

사용:
    python3 scrape_daily.py                    # 전체
    python3 scrape_daily.py --limit 5          # 5채널만
    python3 scrape_daily.py --dry-run          # API 콜 안 함
    python3 scrape_daily.py --skip-embeds      # HTML 파싱 스킵
"""
import os
import re
import sys
import time
import sqlite3
import argparse
import datetime as dt
import logging
import requests
from typing import List, Dict, Iterable

import config

# ──────────── logging setup ────────────
log_file = os.path.join(config.LOG_DIR, f"scrape_{dt.datetime.utcnow().strftime('%Y%m%d')}.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
)
log = logging.getLogger("scrape")


# ──────────── helpers ────────────
def now_utc_iso() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def today_utc() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%d")


def iso8601_duration_to_seconds(s: str):
    if not isinstance(s, str) or not s.startswith("P"):
        return None
    h = m = sec = 0
    if "T" in s:
        t = s.split("T", 1)[1]
        num = ""
        for ch in t:
            if ch.isdigit():
                num += ch
            else:
                if ch == "H": h = int(num or 0)
                elif ch == "M": m = int(num or 0)
                elif ch == "S": sec = int(num or 0)
                num = ""
    return h * 3600 + m * 60 + sec


def batched(it: Iterable, n: int):
    chunk = []
    for x in it:
        chunk.append(x)
        if len(chunk) >= n:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def safe_int(x):
    try:
        return int(x)
    except (ValueError, TypeError):
        return None


# ──────────── API quota tracking ────────────
class QuotaTracker:
    def __init__(self, limit: int, margin: int):
        self.limit = limit
        self.margin = margin
        self.used = 0

    def charge(self, units: int):
        self.used += units
        if self.used + self.margin >= self.limit:
            raise QuotaExhausted(f"Quota near limit: used={self.used}/{self.limit}")


class QuotaExhausted(Exception):
    pass


# ──────────── YouTube API client ────────────
class YT:
    def __init__(self, api_key: str, quota: QuotaTracker):
        self.api_key = api_key
        self.quota = quota
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "yt-research-scraper/1.0"})

    def _get(self, endpoint: str, params: Dict, cost: int = 1) -> Dict:
        self.quota.charge(cost)
        params = {**params, "key": self.api_key}
        url = f"{config.YT_API_BASE}/{endpoint}"
        for attempt in range(1, config.MAX_RETRIES + 1):
            r = self.session.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 403 and "quotaExceeded" in r.text:
                raise QuotaExhausted("API responded quotaExceeded")
            # 4xx (except 408/429) = permanent error, no retry
            if 400 <= r.status_code < 500 and r.status_code not in (408, 429):
                log.warning(f"  HTTP {r.status_code} on {endpoint} [no retry]: {r.text[:150]}")
                r.raise_for_status()
            wait = (config.BACKOFF_BASE ** attempt) + 0.1 * attempt
            log.warning(f"  HTTP {r.status_code} on {endpoint}, retry in {wait:.1f}s ({attempt}/{config.MAX_RETRIES})")
            time.sleep(wait)
        r.raise_for_status()

    def channels(self, ids: List[str]) -> List[Dict]:
        out = []
        for chunk in batched(ids, config.BATCH_CHANNELS):
            data = self._get("channels", {
                "part": "snippet,contentDetails,statistics",
                "id": ",".join(chunk),
                "maxResults": len(chunk),
            })
            out.extend(data.get("items", []))
        return out

    def uploads_recent(self, uploads_playlist_id: str, max_pages: int = 2) -> List[str]:
        """최근 업로드 video_id 리스트 (최대 100개)"""
        out, token = [], None
        for _ in range(max_pages):
            params = {"part": "contentDetails", "playlistId": uploads_playlist_id, "maxResults": 50}
            if token:
                params["pageToken"] = token
            data = self._get("playlistItems", params)
            for it in data.get("items", []):
                vid = (it.get("contentDetails") or {}).get("videoId")
                if vid:
                    out.append(vid)
            token = data.get("nextPageToken")
            if not token:
                break
        return out

    def videos(self, ids: List[str]) -> List[Dict]:
        out = []
        for chunk in batched(ids, config.BATCH_VIDEOS):
            data = self._get("videos", {
                "part": "snippet,contentDetails,statistics",
                "id": ",".join(chunk),
            })
            out.extend(data.get("items", []))
            time.sleep(0.05)
        return out


# ──────────── Shorts HTML embed parser ────────────
# 현재 YouTube는 ytInitialData를 JS escape (\xNN) 형식으로 인코딩.
# 디코드 후 원본 regex 적용.
SHORTS_SESSION = requests.Session()
SHORTS_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})

# Escape decode: \xNN → 해당 ASCII 문자
ESC_PAT = re.compile(r'\\x([0-9a-fA-F]{2})')

def _decode_js_escapes(s: str) -> str:
    return ESC_PAT.sub(lambda m: chr(int(m.group(1), 16)), s)

# 디코드 후 매칭. MFLV 블록 안에서만 reelWatchEndpoint 추출 (= 크리에이터 설정 링크카드).
# 일반 reelWatchEndpoint (다음 영상 추천)는 무시.
MFLV_VID   = re.compile(
    r'reelMultiFormatLinkViewModel":\{(.{0,3000}?)reelWatchEndpoint":\{"videoId":"([A-Za-z0-9_-]+)"',
    re.DOTALL,
)
MFLV_TITLE = re.compile(
    r'reelMultiFormatLinkViewModel":\{.*?"title":\{"content":"([^"]+)"',
    re.DOTALL,
)
POPUP_FLAG = re.compile(r'compactProductListRenderer')
URL_PAT    = re.compile(r'"urlEndpoint":\{"url":"([^"]+?)"')
NAME_PAT   = re.compile(r'"accessibilityTitle":"([^"]+?)"')
# 음원 attribution: "오리지널 사운드" (original) / "Song - Artist" (library) / "@channel" (creator)
SOUND_PAT  = re.compile(r'soundAttributionTitle":\{"content":"([^"]+)"')
# Heuristic: 'original sound' string set (다국어)
ORIGINAL_SOUND_TOKENS = {
    "original sound", "오리지널 사운드", "原创音频", "オリジナル音源",
    "audio original", "son original", "originalton", "som original",
}


def parse_shorts_embed(video_id: str) -> Dict:
    """Return: has_embed, embed_target_id, embed_title, ads_present, shopping_link, product_name,
       sound_attribution_title, sound_is_original, http_status, video_unavailable"""
    base = {"has_embed": 0, "embed_target_id": None, "embed_title": None,
            "ads_present": 0, "shopping_link": None, "product_name": None,
            "sound_attribution_title": None, "sound_is_original": None,
            "http_status": None, "video_unavailable": None,
            "synthetic_disclosure": None}
    try:
        r = SHORTS_SESSION.get(f"https://www.youtube.com/shorts/{video_id}",
                               timeout=config.HTML_TIMEOUT)
        base["http_status"] = r.status_code
        if r.status_code != 200:
            # 404 = 삭제, 403 = 비공개/지역제한, 기타 = 알 수 없음
            return base
        raw = r.text
    except Exception as e:
        log.warning(f"  HTML fail {video_id}: {e}")
        base["http_status"] = -1  # network error
        return base

    # HTML 안에 'video unavailable' 마커 검사 (200 OK인데 삭제/비공개 상태)
    if ('"reason":{"simpleText":"' in raw and
        ('Video unavailable' in raw or '비공개' in raw or 'unavailable' in raw[:50000].lower())):
        base["video_unavailable"] = 1
    elif '"playabilityStatus":{"status":"ERROR"' in raw or '"playabilityStatus":{"status":"UNPLAYABLE"' in raw:
        base["video_unavailable"] = 1
    else:
        base["video_unavailable"] = 0

    # JS escape 디코드 (1MB 페이지에서 ~10ms)
    t = _decode_js_escapes(raw)

    mv = MFLV_VID.search(t)
    mt = MFLV_TITLE.search(t)
    has_embed = 1 if mv else 0
    embed_target_id = mv.group(2) if mv else None
    embed_title = mt.group(1) if mt else None

    has_ads = bool(POPUP_FLAG.search(t))
    shopping_link, product_name = None, None
    if has_ads:
        idx = t.find('"merchantName":')
        if idx != -1:
            snippet = t[max(0, idx - 5000): idx]
            urls = URL_PAT.findall(snippet)
            if urls:
                shopping_link = urls[-1].replace("\\u0026", "&").replace("\\/", "/")
        mn = NAME_PAT.search(t)
        if mn:
            product_name = mn.group(1).split(", 판매처:")[0]

    # 음원 추출
    sound_title = None
    sound_is_original = None
    ms = SOUND_PAT.search(t)
    if ms:
        sound_title = ms.group(1)
        # @로 시작 = creator audio (자체 음원), "오리지널 사운드" 등 = original
        token_lower = sound_title.strip().lower()
        if sound_title.startswith("@") or any(tok in token_lower for tok in ORIGINAL_SOUND_TOKENS):
            sound_is_original = 1
        else:
            sound_is_original = 0  # library music / song attribution

    # ── AI 합성/변형 콘텐츠 공시(synthetic content disclosure) 라벨 ──
    # YouTube가 AI생성·변형 콘텐츠에 붙이는 공시. Stage 3가 이미 가져온 HTML을 재활용(추가 요청 0).
    # best-effort 마커 — AI-특정 문구만(내부 feature flag 'mdx_enable_privacy_disclosure_ui' 등 제외).
    # ★ 실제 라벨된 영상으로 정확 문구 검증 필요(현 데이터셋엔 AI콘텐츠 거의 없음). 새 마커 발견 시 추가.
    raw_low = raw.lower()
    SYNTH_MARKERS = ("altered or synthetic", "syntheticcontent", "alteredorsynthetic",
                     "significantly edited or digitally generated",
                     "sound or visuals were significantly edited")
    synthetic_disclosure = 1 if any(mk in raw_low for mk in SYNTH_MARKERS) else 0

    base.update({"has_embed": has_embed, "embed_target_id": embed_target_id,
                 "embed_title": embed_title, "ads_present": int(has_ads),
                 "shopping_link": shopping_link, "product_name": product_name,
                 "sound_attribution_title": sound_title, "sound_is_original": sound_is_original,
                 "synthetic_disclosure": synthetic_disclosure})
    return base


# ──────────── DB helpers ────────────
def ensure_schema(conn):
    """Idempotent schema migrations — embed target tracking 컬럼 추가."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(scrape_log)").fetchall()}
    for col in ("n_embed_targets_discovered", "n_embed_targets_refreshed"):
        if col not in cols:
            conn.execute(f"ALTER TABLE scrape_log ADD COLUMN {col} INTEGER DEFAULT 0")
            log.info(f"  migrated: added scrape_log.{col}")
    # AI 합성/변형 콘텐츠 공시(synthetic content disclosure) 라벨 — embeds_daily에 기록
    edcols = {r[1] for r in conn.execute("PRAGMA table_info(embeds_daily)").fetchall()}
    if "synthetic_disclosure" not in edcols:
        conn.execute("ALTER TABLE embeds_daily ADD COLUMN synthetic_disclosure INTEGER")
        log.info("  migrated: added embeds_daily.synthetic_disclosure")
    conn.commit()


def get_active_channels(conn) -> List[str]:
    if config.ACTIVE_CHANNELS_ONLY_INTERSECTION:
        # YT 카테고리 라벨 있는 채널만 (= 272개)
        rows = conn.execute("SELECT channel_id FROM channels WHERE category_yt IS NOT NULL").fetchall()
    else:
        rows = conn.execute("SELECT channel_id FROM channels").fetchall()
    return [r[0] for r in rows]


def get_active_video_ids(conn, channel_id: str) -> List[str]:
    cutoff = (dt.datetime.utcnow() - dt.timedelta(days=config.ACTIVE_VIDEO_LOOKBACK_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT video_id FROM videos WHERE channel_id=? AND published_at>=?",
        (channel_id, cutoff)
    ).fetchall()
    return [r[0] for r in rows]


def get_active_shorts(conn, channel_id: str) -> List[str]:
    cutoff = (dt.datetime.utcnow() - dt.timedelta(days=config.EMBED_REPARSE_LOOKBACK_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT video_id FROM videos WHERE channel_id=? AND is_shorts=1 AND published_at>=?",
        (channel_id, cutoff)
    ).fetchall()
    return [r[0] for r in rows]


def get_longform_target_shorts(conn, scrape_date: str, cap: int) -> List[str]:
    """Stage 3b 대상: 롱폼(>180s) 타깃을 임베드하는 숏들.
    오늘 이미 파싱된 숏은 제외, least-recently-seen(embeds_daily 최종 관측일) 우선 → CAP까지 rotating."""
    thr = getattr(config, "LONGFORM_DURATION_THRESHOLD", 180)
    rows = conn.execute("""
        SELECT e.shorts_id, MAX(ed.scrape_date) AS last_seen
        FROM embeds e
        JOIN videos v ON v.video_id = e.embed_target_id
        LEFT JOIN embeds_daily ed ON ed.shorts_id = e.shorts_id
        WHERE v.duration_s > ?
          AND e.shorts_id NOT IN (SELECT shorts_id FROM embeds_daily WHERE scrape_date = ?)
        GROUP BY e.shorts_id
        ORDER BY (last_seen IS NOT NULL), last_seen ASC
        LIMIT ?
    """, (thr, scrape_date, cap)).fetchall()
    return [r[0] for r in rows]


def upsert_channel_daily(conn, ch_item: Dict, scrape_date: str, scraped_at: str):
    st = ch_item.get("statistics", {}) or {}
    sub = None if str(st.get("hiddenSubscriberCount", "false")).lower() == "true" else safe_int(st.get("subscriberCount"))
    conn.execute("""
        INSERT INTO channels_daily(channel_id, scrape_date, subscriber_count, total_view_count, total_video_count, scraped_at_utc)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(channel_id, scrape_date) DO UPDATE SET
          subscriber_count=excluded.subscriber_count,
          total_view_count=excluded.total_view_count,
          total_video_count=excluded.total_video_count,
          scraped_at_utc=excluded.scraped_at_utc
    """, (ch_item["id"], scrape_date, sub,
          safe_int(st.get("viewCount")), safe_int(st.get("videoCount")), scraped_at))
    # channels.last_seen_at도 업데이트
    conn.execute("UPDATE channels SET last_seen_at=? WHERE channel_id=?", (scraped_at, ch_item["id"]))


def upsert_new_video(conn, channel_id: str, v: Dict, scraped_at: str):
    sn = v.get("snippet", {}) or {}
    cd = v.get("contentDetails", {}) or {}
    duration_s = iso8601_duration_to_seconds(cd.get("duration"))
    is_shorts = 1 if (duration_s is not None and duration_s <= 180) else 0
    thumbs = sn.get("thumbnails") or {}
    thumb = thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}
    conn.execute("""
        INSERT INTO videos(video_id, channel_id, title, description, published_at, duration_s,
                           category_id, default_language, thumbnail_url, keywords, is_shorts,
                           discovered_at, last_seen_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(video_id) DO UPDATE SET
          last_seen_at=excluded.last_seen_at,
          title=COALESCE(excluded.title, videos.title),
          description=COALESCE(excluded.description, videos.description)
    """, (v["id"], channel_id, sn.get("title"), sn.get("description"),
          sn.get("publishedAt"), duration_s, safe_int(sn.get("categoryId")),
          sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
          thumb.get("url"), ";".join(sn.get("tags") or []) or None,
          is_shorts, scraped_at, scraped_at))


def upsert_video_daily(conn, v: Dict, scrape_date: str, scraped_at: str):
    sts = v.get("statistics", {}) or {}
    conn.execute("""
        INSERT INTO videos_daily(video_id, scrape_date, view_count, like_count, comment_count, scraped_at_utc)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(video_id, scrape_date) DO UPDATE SET
          view_count=excluded.view_count,
          like_count=excluded.like_count,
          comment_count=excluded.comment_count,
          scraped_at_utc=excluded.scraped_at_utc
    """, (v["id"], scrape_date, safe_int(sts.get("viewCount")),
          safe_int(sts.get("likeCount")), safe_int(sts.get("commentCount")), scraped_at))


def record_embed_state(conn, shorts_id: str, em: Dict, scrape_date: str, scraped_at: str) -> int:
    """Return: 1 if embed state changed vs last known, 0 otherwise"""
    # embeds_daily 항상 인서트
    conn.execute("""
        INSERT INTO embeds_daily(shorts_id, scrape_date, has_embed, embed_target_id, embed_title,
                                 ads_present, shopping_link, product_name,
                                 sound_attribution_title, sound_is_original,
                                 http_status, video_unavailable, synthetic_disclosure, scraped_at_utc)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(shorts_id, scrape_date) DO UPDATE SET
          has_embed=excluded.has_embed,
          embed_target_id=excluded.embed_target_id,
          embed_title=excluded.embed_title,
          ads_present=excluded.ads_present,
          shopping_link=excluded.shopping_link,
          product_name=excluded.product_name,
          sound_attribution_title=excluded.sound_attribution_title,
          sound_is_original=excluded.sound_is_original,
          http_status=excluded.http_status,
          video_unavailable=excluded.video_unavailable,
          synthetic_disclosure=excluded.synthetic_disclosure,
          scraped_at_utc=excluded.scraped_at_utc
    """, (shorts_id, scrape_date, em["has_embed"], em["embed_target_id"], em["embed_title"],
          em["ads_present"], em["shopping_link"], em["product_name"],
          em.get("sound_attribution_title"), em.get("sound_is_original"),
          em.get("http_status"), em.get("video_unavailable"), em.get("synthetic_disclosure"), scraped_at))

    # embeds 테이블: 변경 감지
    prev = conn.execute(
        "SELECT embed_target_id FROM embeds WHERE shorts_id=? AND removed_at IS NULL",
        (shorts_id,)
    ).fetchall()
    prev_targets = {r[0] for r in prev}
    new_target = em["embed_target_id"]
    changed = 0

    if em["has_embed"] and new_target:
        if new_target not in prev_targets:
            # 새 엣지
            conn.execute("""
                INSERT INTO embeds(shorts_id, embed_target_id, embed_target_url, embed_title,
                                   is_self_embed, first_seen_at, last_seen_at, removed_at)
                VALUES(?,?,?,?,?,?,?,NULL)
                ON CONFLICT(shorts_id, embed_target_id) DO UPDATE SET
                  last_seen_at=excluded.last_seen_at,
                  removed_at=NULL
            """, (shorts_id, new_target,
                  f"https://www.youtube.com/watch?v={new_target}",
                  em["embed_title"], None, scraped_at, scraped_at))
            changed = 1
        else:
            conn.execute("UPDATE embeds SET last_seen_at=? WHERE shorts_id=? AND embed_target_id=?",
                         (scraped_at, shorts_id, new_target))
        # 다른 target들은 removed
        for t in prev_targets - {new_target}:
            conn.execute("UPDATE embeds SET removed_at=? WHERE shorts_id=? AND embed_target_id=?",
                         (scraped_at, shorts_id, t))
            changed = 1
    else:
        # embed 사라짐
        for t in prev_targets:
            conn.execute("UPDATE embeds SET removed_at=? WHERE shorts_id=? AND embed_target_id=?",
                         (scraped_at, shorts_id, t))
            changed = 1

    return changed


# ──────────── Embed target tracking (Phase 3 mechanism) ────────────
def discover_and_track_embed_targets(conn, yt, scrape_date, scraped_at, dry_run, active_ch_ids, stats):
    """Stage 4: Phase 3 (causal mechanism) 분석용 target longform tracking.

    임베드 target은 active panel 채널 밖 영상도 많아서 stage 2가 못 잡는다.
    여기서:
      (A) embeds_daily에서 발견된 새 target (videos 미등록) → API로 메타 → videos+videos_daily 등록
      (B) 기존 target 중 채널이 active panel 밖인 것들 → videos_daily 갱신
          (panel 안 target은 stage 2가 이미 갱신함)
    """
    # (A) 신규 target 발견
    new_targets = [r[0] for r in conn.execute("""
        SELECT DISTINCT ed.embed_target_id
        FROM embeds_daily ed
        LEFT JOIN videos v ON v.video_id = ed.embed_target_id
        WHERE ed.has_embed = 1
          AND ed.embed_target_id IS NOT NULL
          AND v.video_id IS NULL
    """).fetchall()]
    log.info(f"[stage 4a] embed targets to discover: {len(new_targets):,}")

    if not dry_run and new_targets:
        for chunk in batched(new_targets, config.BATCH_VIDEOS):
            try:
                items = yt.videos(chunk)
                for v in items:
                    sn = v.get("snippet", {}) or {}
                    ch_id = sn.get("channelId")
                    if not ch_id:
                        continue
                    # 채널 FK 보장 — category_yt IS NULL이라 active panel 자동 진입 안 함
                    conn.execute("""
                        INSERT OR IGNORE INTO channels(channel_id, channel_title, discovered_at, last_seen_at)
                        VALUES(?,?,?,?)
                    """, (ch_id, sn.get("channelTitle"), scraped_at, scraped_at))
                    upsert_new_video(conn, ch_id, v, scraped_at)
                    upsert_video_daily(conn, v, scrape_date, scraped_at)
                    stats["n_embed_targets_discovered"] += 1
            except QuotaExhausted:
                raise
            except Exception as e:
                log.warning(f"  embed target discovery chunk failed: {e}")
        conn.commit()

    # (B) 기존 non-panel target stats 갱신
    placeholders = ",".join("?" * len(active_ch_ids)) if active_ch_ids else "''"
    existing_ids = [r[0] for r in conn.execute(f"""
        SELECT DISTINCT v.video_id
        FROM videos v
        WHERE v.video_id IN (SELECT embed_target_id FROM embeds WHERE embed_target_id IS NOT NULL)
          AND v.channel_id NOT IN ({placeholders})
    """, list(active_ch_ids) if active_ch_ids else []).fetchall()]
    log.info(f"[stage 4b] non-panel embed targets to refresh: {len(existing_ids):,}")

    if not dry_run and existing_ids:
        for chunk in batched(existing_ids, config.BATCH_VIDEOS):
            try:
                items = yt.videos(chunk)
                for v in items:
                    upsert_video_daily(conn, v, scrape_date, scraped_at)
                    stats["n_embed_targets_refreshed"] += 1
            except QuotaExhausted:
                raise
            except Exception as e:
                log.warning(f"  embed target refresh chunk failed: {e}")
        conn.commit()


# ──────────── Thumbnail uploader (B2) ────────────
_b2_bucket = None
def _get_b2_bucket():
    """Lazy-init B2 bucket connection (only when needed)."""
    global _b2_bucket
    if _b2_bucket is not None:
        return _b2_bucket
    key_id = os.environ.get("B2_KEY_ID")
    app_key = os.environ.get("B2_APPLICATION_KEY")
    bucket_name = os.environ.get("B2_BUCKET", "yt-daily-jh")
    if not (key_id and app_key):
        return None
    try:
        from b2sdk.v2 import InMemoryAccountInfo, B2Api
        info = InMemoryAccountInfo()
        api = B2Api(info)
        api.authorize_account("production", key_id, app_key)
        _b2_bucket = api.get_bucket_by_name(bucket_name)
        return _b2_bucket
    except Exception as e:
        log.warning(f"  B2 init failed: {e}")
        return None

def upload_thumbnail(video_id: str, thumbnail_url: str) -> bool:
    """Download thumbnail and upload to B2 (idempotent — skips if already uploaded)."""
    bucket = _get_b2_bucket()
    if bucket is None:
        return False
    file_name = f"thumbnails/{video_id}.jpg"
    # Check existence
    try:
        for fi, _ in bucket.ls(folder_to_list=file_name, recursive=False):
            if fi.file_name == file_name:
                return True  # already there
    except Exception:
        pass
    # Download
    try:
        r = SHORTS_SESSION.get(thumbnail_url, timeout=10)
        if r.status_code != 200 or len(r.content) < 1000:
            return False
        bucket.upload_bytes(data_bytes=r.content, file_name=file_name, content_type="image/jpeg")
        return True
    except Exception as e:
        log.warning(f"  thumbnail upload fail {video_id}: {e}")
        return False


# ──────────── main run ────────────
def run(limit=None, dry_run=False, skip_embeds=False):
    scrape_date = today_utc()
    scraped_at = now_utc_iso()

    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    ensure_schema(conn)

    # scrape_log row 생성
    cur = conn.execute(
        "INSERT INTO scrape_log(scrape_date, started_at_utc, status) VALUES(?,?,?)",
        (scrape_date, scraped_at, "running")
    )
    run_id = cur.lastrowid
    conn.commit()

    quota = QuotaTracker(config.DAILY_QUOTA_LIMIT, config.QUOTA_SAFETY_MARGIN)
    if dry_run:
        yt = None
    else:
        yt = YT(config.get_api_key(), quota)

    active = get_active_channels(conn)
    if limit:
        active = active[:limit]
    log.info(f"=== Scrape {scrape_date} | {len(active)} channels | dry_run={dry_run} | skip_embeds={skip_embeds} ===")

    stats = {"n_channels_done": 0, "n_channels_failed": 0,
             "n_videos_updated": 0, "n_new_videos": 0,
             "n_embeds_reparsed": 0, "n_embed_changes": 0,
             "n_embed_targets_discovered": 0, "n_embed_targets_refreshed": 0}

    try:
        # ───────── 1) 채널 stats + 신규 업로드 발견 ─────────
        log.info("[stage 1] channel stats + new uploads discovery")
        for ch_chunk in batched(active, config.BATCH_CHANNELS):
            if dry_run:
                log.info(f"  [dry] would fetch {len(ch_chunk)} channels")
                stats["n_channels_done"] += len(ch_chunk)
                continue
            try:
                items = yt.channels(ch_chunk)
                for ci in items:
                    upsert_channel_daily(conn, ci, scrape_date, scraped_at)
                    # uploads playlist에서 신규 발견
                    uploads_pl = (ci.get("contentDetails", {}).get("relatedPlaylists") or {}).get("uploads")
                    if uploads_pl:
                        try:
                            recent_ids = yt.uploads_recent(uploads_pl, max_pages=1)
                            existing = set(r[0] for r in conn.execute(
                                f"SELECT video_id FROM videos WHERE video_id IN ({','.join('?'*len(recent_ids))})",
                                recent_ids
                            ).fetchall()) if recent_ids else set()
                            new_ids = [v for v in recent_ids if v not in existing]
                            if new_ids:
                                # 신규 영상 메타 즉시 가져옴
                                new_meta = yt.videos(new_ids)
                                for v in new_meta:
                                    upsert_new_video(conn, ci["id"], v, scraped_at)
                                    upsert_video_daily(conn, v, scrape_date, scraped_at)
                                    # 신규 영상 썸네일 → B2 (best effort, 실패해도 진행)
                                    sn = v.get("snippet", {}) or {}
                                    thumbs = sn.get("thumbnails") or {}
                                    thumb = thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}
                                    if thumb.get("url"):
                                        upload_thumbnail(v["id"], thumb["url"])
                                stats["n_new_videos"] += len(new_meta)
                                log.info(f"  + {ci['id']}: {len(new_meta)} new videos")
                        except QuotaExhausted:
                            raise
                        except Exception as e:
                            log.warning(f"  uploads fetch failed for {ci['id']}: {e}")
                stats["n_channels_done"] += len(items)
                conn.commit()
            except QuotaExhausted:
                raise
            except Exception as e:
                log.error(f"  channel chunk failed: {e}")
                stats["n_channels_failed"] += len(ch_chunk)

        # ───────── 2) 활성 영상 stats 갱신 ─────────
        log.info("[stage 2] update active video stats")
        all_video_ids = []
        for cid in active:
            all_video_ids.extend(get_active_video_ids(conn, cid))
        log.info(f"  active videos to update: {len(all_video_ids):,}")

        if not dry_run:
            for v_chunk in batched(all_video_ids, config.BATCH_VIDEOS):
                try:
                    items = yt.videos(v_chunk)
                    for v in items:
                        upsert_video_daily(conn, v, scrape_date, scraped_at)
                    stats["n_videos_updated"] += len(items)
                except QuotaExhausted:
                    raise
                except Exception as e:
                    log.error(f"  video chunk failed: {e}")
            conn.commit()

        # ───────── 3) Shorts embed 재파싱 ─────────
        if not skip_embeds:
            log.info("[stage 3] re-parse Shorts embeds (HTML)")
            all_shorts = []
            for cid in active:
                all_shorts.extend(get_active_shorts(conn, cid))
            log.info(f"  active shorts to re-parse: {len(all_shorts):,}")

            if not dry_run:
                for sid in all_shorts:
                    em = parse_shorts_embed(sid)
                    changed = record_embed_state(conn, sid, em, scrape_date, scraped_at)
                    stats["n_embeds_reparsed"] += 1
                    stats["n_embed_changes"] += changed
                    time.sleep(config.HTML_THROTTLE_SEC)
                    if stats["n_embeds_reparsed"] % 100 == 0:
                        log.info(f"    progress: {stats['n_embeds_reparsed']}/{len(all_shorts)}, changes={stats['n_embed_changes']}")
                        conn.commit()
                conn.commit()

        # ───────── 3b) 롱폼 타깃 dose 추적 (숏→롱 funnel 식별용) ─────────
        # 활성 숏(Stage 3)은 거의 숏→숏만 → 롱폼 타깃이 일별 추적에서 누락.
        # 롱폼을 임베드하는 숏들을 rotating으로 재방문해 롱폼 타깃 일별 dose 변동 포착.
        if not skip_embeds:
            cap = getattr(config, "LONGFORM_DOSE_REPARSE_CAP", 0)
            if cap > 0 and not dry_run:
                lf_shorts = get_longform_target_shorts(conn, scrape_date, cap)
                log.info(f"[stage 3b] longform-target dose reparse: {len(lf_shorts):,} shorts (cap {cap})")
                n_lf = 0
                for sid in lf_shorts:
                    try:
                        em = parse_shorts_embed(sid)
                        changed = record_embed_state(conn, sid, em, scrape_date, scraped_at)
                        stats["n_embeds_reparsed"] += 1
                        stats["n_embed_changes"] += changed
                        stats["n_longform_dose_reparsed"] = stats.get("n_longform_dose_reparsed", 0) + 1
                        n_lf += 1
                        time.sleep(config.HTML_THROTTLE_SEC)
                        if n_lf % 100 == 0:
                            log.info(f"    stage3b progress: {n_lf}/{len(lf_shorts)}")
                            conn.commit()
                    except Exception as e:
                        log.warning(f"  stage3b reparse fail {sid}: {e}")
                conn.commit()
                log.info(f"  stage 3b done: {n_lf:,} longform-target shorts reparsed")

        # ───────── 4) Embed target tracking (Phase 3 mechanism용) ─────────
        if not skip_embeds:
            log.info("[stage 4] embed target discovery + refresh")
            discover_and_track_embed_targets(
                conn, yt, scrape_date, scraped_at, dry_run,
                active_ch_ids=set(active), stats=stats)

        status = "success"
        notes = None
    except QuotaExhausted as e:
        log.warning(f"  quota exhausted: {e}")
        status = "partial"
        notes = str(e)
    except Exception as e:
        log.exception(f"  fatal: {e}")
        status = "failed"
        notes = str(e)

    # log row 마무리
    conn.execute("""
        UPDATE scrape_log SET ended_at_utc=?, n_channels_done=?, n_channels_failed=?,
          n_videos_updated=?, n_new_videos=?, n_embeds_reparsed=?, n_embed_changes=?,
          n_embed_targets_discovered=?, n_embed_targets_refreshed=?,
          quota_units_used=?, status=?, notes=? WHERE run_id=?
    """, (now_utc_iso(), stats["n_channels_done"], stats["n_channels_failed"],
          stats["n_videos_updated"], stats["n_new_videos"],
          stats["n_embeds_reparsed"], stats["n_embed_changes"],
          stats["n_embed_targets_discovered"], stats["n_embed_targets_refreshed"],
          quota.used, status, notes, run_id))
    conn.commit()
    # WAL을 본 DB 파일에 완전 병합 후 비움 — 업로드 시 .db 단일 파일이 일관성을 갖도록.
    # (이 체크포인트 없이 WAL이 남은 채 .db만 B2 업로드되면 "disk image malformed" 손상 발생)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integ != "ok":
            log.error(f"integrity_check after checkpoint: {integ}")
    except Exception as e:
        log.error(f"wal_checkpoint failed: {e}")
    conn.close()

    log.info(f"=== Done | status={status} | quota={quota.used}/{config.DAILY_QUOTA_LIMIT} | {stats}")


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="처리 채널 수 제한")
    ap.add_argument("--dry-run", action="store_true", help="API 콜 안 함")
    ap.add_argument("--skip-embeds", action="store_true", help="HTML 파싱 스킵")
    return ap.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(limit=args.limit, dry_run=args.dry_run, skip_embeds=args.skip_embeds)
