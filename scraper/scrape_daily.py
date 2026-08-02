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
from concurrent.futures import ThreadPoolExecutor
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
# 병렬 재파싱용 커넥션 풀 확대 (기본 10 → 워커 수 이상). requests.Session은
# urllib3 풀 기반이라 read-only GET을 스레드 간 공유해도 안전.
_pool = max(getattr(config, "EMBED_PARSE_WORKERS", 12), 10)
_adapter = requests.adapters.HTTPAdapter(pool_connections=_pool, pool_maxsize=_pool)
SHORTS_SESSION.mount("https://", _adapter)
SHORTS_SESSION.mount("http://", _adapter)


def parse_shorts_throttled(video_id: str) -> Dict:
    """워커 스레드용: parse + per-thread throttle. (DB는 만지지 않음 → 스레드 안전)"""
    em = parse_shorts_embed(video_id)
    if config.HTML_THROTTLE_SEC:
        time.sleep(config.HTML_THROTTLE_SEC)
    return em


def parse_shorts_parallel(sids: List[str]):
    """sids를 병렬로 파싱해 (sid, em) 결과를 입력 순서대로 반환.
    네트워크 파싱만 병렬; 호출부가 메인 스레드에서 순차로 DB에 기록한다."""
    workers = max(getattr(config, "EMBED_PARSE_WORKERS", 12), 1)
    if workers <= 1 or len(sids) <= 1:
        return [(s, parse_shorts_throttled(s)) for s in sids]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        ems = list(ex.map(parse_shorts_throttled, sids))
    return list(zip(sids, ems))

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
# ★ 2026-07-16 YouTube 개편: reelMultiFormatLinkViewModel 문자열이 HTML에서 사라짐
#   → has_embed가 전 영상 0으로 떨어짐(무증상 파손, 7/16~8/02 임베드 데이터 공백).
#   링크카드는 reelCarouselViewModel > reelCarouselButtonViewModel 로 이동.
#   캐러셀에는 링크 외 버튼(AI 공시 등)도 들어가므로 버튼 경계를 넘지 않도록
#   negative lookahead로 스팬을 가둔다. 넘치면 이웃 릴의 추천 영상을 링크로 오인함.
#   선택 규칙 = 마지막 매치. (확정링크 45개 검증: 마지막 45/45 복원, 첫 매치는 34/45)
CAROUSEL_BTN = re.compile(
    r'reelCarouselButtonViewModel":\{((?:(?!reelCarouselButtonViewModel).){0,3500}?)'
    r'reelWatchEndpoint":\{"videoId":"([A-Za-z0-9_-]{11})"'
    r'((?:(?!reelCarouselButtonViewModel).){0,1500})',
    re.DOTALL,
)
# 링크카드 제목은 버튼 블록의 accessibilityText (구 MFLV의 title.content 대응)
CAROUSEL_TITLE = re.compile(r'"accessibilityText":"([^"]{1,300})"')
# AI 공시 배지 (2026-08-02 확인된 현행 문구) + 앵커링용 캐러셀 블록 시작점
AI_BADGE = re.compile(r'"accessibilityText":"(AI: [^"]{0,200})"')
AI_LEGACY = re.compile(r'altered or synthetic|syntheticContent|alteredOrSynthetic'
                       r'|significantly edited or digitally generated', re.I)
CAROUSEL_BLOCK = re.compile(r'reelCarouselViewModel')
# 영상 가용성: ytInitialPlayerResponse의 playabilityStatus (OK / LOGIN_REQUIRED=비공개 / ERROR=삭제)
PLAYABILITY = re.compile(r'"playabilityStatus":\{"status":"([A-Z_]+)"')
AI_ANCHOR_WINDOW = 9000        # 마지막 캐러셀 블록의 유효 범위(문자)
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
            "sound_n_matches": None,
            "http_status": None, "video_unavailable": None, "playability_status": None,
            "synthetic_disclosure": None,
            "embed_source": None, "embed_n_matches": None,
            "ai_disclosure_raw": None, "ai_disclosure_text": None}
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

    # JS escape 디코드 (1MB 페이지에서 ~10ms)
    t = _decode_js_escapes(raw)

    # ── 영상 가용성(삭제·비공개) 판정 ──
    # ★2026-08-02 전면 교체. 구 조건은 `'unavailable' in raw[:50000]`이 너무 느슨해
    #   살아있는 영상의 86%를 unavailable로 오탐하면서, 정작 API 정답지 대비 재현율은 40%(8/20)였다.
    #   (이 컬럼으로 필터링한 분석 스크립트들이 표본의 89%를 비무작위로 날리고 있었음)
    # YouTube는 삭제·비공개 영상에도 HTTP 200을 준다(전 패널에서 404는 단 1건) → status code로는 판정 불가.
    # 정답 = ytInitialPlayerResponse의 playabilityStatus 첫 매치.
    #   API 대조 검증: 생존 20/20 'OK', 삭제·비공개 20/20 'LOGIN_REQUIRED'(비공개)·'ERROR'(삭제) → 완전 분리.
    mp = PLAYABILITY.search(t)
    playability = mp.group(1) if mp else None
    base["playability_status"] = playability
    base["video_unavailable"] = None if playability is None else int(playability != "OK")

    # 구 MFLV 우선 (YouTube가 되돌릴 경우 대비) → 없으면 신 캐러셀 경로
    mv = MFLV_VID.search(t)
    embed_source, embed_n_matches = None, None
    if mv:
        mt = MFLV_TITLE.search(t)
        has_embed, embed_source = 1, "mflv"
        embed_target_id = mv.group(2)
        embed_title = mt.group(1) if mt else None
        embed_n_matches = 1
    else:
        btns = list(CAROUSEL_BTN.finditer(t))
        embed_n_matches = len(btns)
        if btns:
            m = btns[-1]                       # 마지막 매치 = 현재 릴의 링크카드
            has_embed, embed_source = 1, "carousel"
            embed_target_id = m.group(2)
            ma = CAROUSEL_TITLE.search(m.group(1) + m.group(3))
            embed_title = ma.group(1) if ma else None
        else:
            has_embed = 0
            embed_target_id = None
            embed_title = None

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
    # ★2026-08-02 수정: 기존엔 .search() = 페이지 첫 매치를 썼는데, Shorts 페이지엔
    #   이웃 릴이 프리로드돼 있어 92%가 음원 문자열을 2개 갖는다(첫 매치 = 남의 음원).
    #   판정 기준 = 같은 영상 5회 재요청 시 값 일치율(진짜 음원은 몇 분 새 안 변함):
    #   first 16/25(64%) vs **last 25/25(100%)**. 과거 일별 데이터의 인접일 일치 62%와
    #   first 규칙의 64%가 일치 → 기존 값의 상당수가 이웃 릴 음원이었음.
    sound_title = None
    sound_is_original = None
    snds = SOUND_PAT.findall(t)
    sound_n_matches = len(snds)
    if snds:
        sound_title = snds[-1]
        # @로 시작 = creator audio (자체 음원), "오리지널 사운드" 등 = original
        token_lower = sound_title.strip().lower()
        if sound_title.startswith("@") or any(tok in token_lower for tok in ORIGINAL_SOUND_TOKENS):
            sound_is_original = 1
        else:
            sound_is_original = 0  # library music / song attribution

    # ── AI 합성/변형 콘텐츠 공시(synthetic content disclosure) 라벨 ──
    # ★2026-08-02 정정: 구 마커('altered or synthetic' 등)는 현행 HTML에 존재하지 않음
    #   (무작위 150개 검사 0/150). 전 기간 0으로 기록된 건 "AI 콘텐츠 부재"가 아니라 측정 실패였음.
    #   현행 라벨 = 캐러셀의 INFO 버튼: {"iconName":"INFO","title":"AI",
    #                                   "accessibilityText":"AI: Content was made with AI"}
    # ★앵커링 필수: Shorts 페이지엔 이웃 릴이 프리로드돼 있어 페이지 전체 검색은 남의 배지를 잡는다.
    #   링크카드와 동일하게 '마지막 캐러셀 블록' 안에 있을 때만 이 영상 것으로 인정.
    # ⚠️ 정확도 미검증: 라벨 확실한 정답지가 없어 재요청 3회 일관성(29/30)만 확인된 상태.
    #   따라서 원자료(ai_disclosure_raw/_text)를 함께 남겨 나중에 규칙을 바꿔 재산출할 수 있게 한다.
    badges = list(AI_BADGE.finditer(t))
    car_starts = [m.start() for m in CAROUSEL_BLOCK.finditer(t)]
    ai_raw_hits = len(badges)
    ai_text = badges[-1].group(1) if badges else None
    synthetic_disclosure = 0
    if badges and car_starts:
        last_car = car_starts[-1]
        if any(last_car <= m.start() < last_car + AI_ANCHOR_WINDOW for m in badges):
            synthetic_disclosure = 1
    # 구 마커가 되살아나면(YouTube 롤백) 침묵하지 않도록 흔적을 남긴다 — 앵커 측정에는 넣지 않음
    if not badges:
        legacy = AI_LEGACY.search(raw)
        if legacy:
            ai_text = "LEGACY:" + legacy.group(0)[:120]
            ai_raw_hits = -1

    base.update({"has_embed": has_embed, "embed_target_id": embed_target_id,
                 "embed_title": embed_title,
                 "embed_source": embed_source, "embed_n_matches": embed_n_matches,
                 "ads_present": int(has_ads),
                 "shopping_link": shopping_link, "product_name": product_name,
                 "sound_attribution_title": sound_title, "sound_is_original": sound_is_original,
                 "sound_n_matches": sound_n_matches,
                 "synthetic_disclosure": synthetic_disclosure,
                 "ai_disclosure_raw": ai_raw_hits, "ai_disclosure_text": ai_text})
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
    # 임베드 추출 출처('mflv'|'carousel') + 페이지 내 캐러셀 버튼 매치 수.
    # 분석 시 오탐 필터용 감사 컬럼 — 매치 2개 이상이면 이웃 릴 혼입 가능성 검토.
    if "embed_source" not in edcols:
        conn.execute("ALTER TABLE embeds_daily ADD COLUMN embed_source TEXT")
        log.info("  migrated: added embeds_daily.embed_source")
    if "embed_n_matches" not in edcols:
        conn.execute("ALTER TABLE embeds_daily ADD COLUMN embed_n_matches INTEGER")
        log.info("  migrated: added embeds_daily.embed_n_matches")
    # AI 공시 원자료 — synthetic_disclosure(앵커링 결과)와 별도로 페이지 전체 검출수·문구 보존.
    # 앵커 규칙이 틀린 것으로 판명나도 이 두 컬럼으로 재산출 가능하게 하기 위함.
    if "ai_disclosure_raw" not in edcols:
        conn.execute("ALTER TABLE embeds_daily ADD COLUMN ai_disclosure_raw INTEGER")
        log.info("  migrated: added embeds_daily.ai_disclosure_raw")
    if "ai_disclosure_text" not in edcols:
        conn.execute("ALTER TABLE embeds_daily ADD COLUMN ai_disclosure_text TEXT")
        log.info("  migrated: added embeds_daily.ai_disclosure_text")
    # 페이지 내 음원 문자열 개수 — 2 이상이면 이웃 릴 혼입 구간(=선택 규칙이 실제로 작동한 행)
    if "sound_n_matches" not in edcols:
        conn.execute("ALTER TABLE embeds_daily ADD COLUMN sound_n_matches INTEGER")
        log.info("  migrated: added embeds_daily.sound_n_matches")
    # 가용성 원자료 — video_unavailable(0/1)의 근거 문자열. OK/LOGIN_REQUIRED/ERROR 구분 보존
    if "playability_status" not in edcols:
        conn.execute("ALTER TABLE embeds_daily ADD COLUMN playability_status TEXT")
        log.info("  migrated: added embeds_daily.playability_status")
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
                                 sound_attribution_title, sound_is_original, sound_n_matches,
                                 http_status, video_unavailable, playability_status, synthetic_disclosure,
                                 embed_source, embed_n_matches,
                                 ai_disclosure_raw, ai_disclosure_text, scraped_at_utc)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(shorts_id, scrape_date) DO UPDATE SET
          has_embed=excluded.has_embed,
          embed_target_id=excluded.embed_target_id,
          embed_title=excluded.embed_title,
          ads_present=excluded.ads_present,
          shopping_link=excluded.shopping_link,
          product_name=excluded.product_name,
          sound_attribution_title=excluded.sound_attribution_title,
          sound_is_original=excluded.sound_is_original,
          sound_n_matches=excluded.sound_n_matches,
          http_status=excluded.http_status,
          video_unavailable=excluded.video_unavailable,
          playability_status=excluded.playability_status,
          synthetic_disclosure=excluded.synthetic_disclosure,
          embed_source=excluded.embed_source,
          embed_n_matches=excluded.embed_n_matches,
          ai_disclosure_raw=excluded.ai_disclosure_raw,
          ai_disclosure_text=excluded.ai_disclosure_text,
          scraped_at_utc=excluded.scraped_at_utc
    """, (shorts_id, scrape_date, em["has_embed"], em["embed_target_id"], em["embed_title"],
          em["ads_present"], em["shopping_link"], em["product_name"],
          em.get("sound_attribution_title"), em.get("sound_is_original"), em.get("sound_n_matches"),
          em.get("http_status"), em.get("video_unavailable"), em.get("playability_status"),
          em.get("synthetic_disclosure"),
          em.get("embed_source"), em.get("embed_n_matches"),
          em.get("ai_disclosure_raw"), em.get("ai_disclosure_text"), scraped_at))

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
                # 네트워크 파싱은 병렬, DB 기록은 메인 스레드 순차 (배치 단위 commit)
                for batch in batched(all_shorts, 500):
                    for sid, em in parse_shorts_parallel(batch):
                        changed = record_embed_state(conn, sid, em, scrape_date, scraped_at)
                        stats["n_embeds_reparsed"] += 1
                        stats["n_embed_changes"] += changed
                    conn.commit()
                    log.info(f"    progress: {stats['n_embeds_reparsed']}/{len(all_shorts)}, changes={stats['n_embed_changes']}")
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
                for batch in batched(lf_shorts, 500):
                    for sid, em in parse_shorts_parallel(batch):
                        try:
                            changed = record_embed_state(conn, sid, em, scrape_date, scraped_at)
                            stats["n_embeds_reparsed"] += 1
                            stats["n_embed_changes"] += changed
                            stats["n_longform_dose_reparsed"] = stats.get("n_longform_dose_reparsed", 0) + 1
                            n_lf += 1
                        except Exception as e:
                            log.warning(f"  stage3b reparse fail {sid}: {e}")
                    conn.commit()
                    log.info(f"    stage3b progress: {n_lf}/{len(lf_shorts)}")
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
