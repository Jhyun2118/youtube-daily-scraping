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


def parse_shorts_embed(video_id: str) -> Dict:
    """Return: has_embed, embed_target_id, embed_title, ads_present, shopping_link, product_name"""
    empty = {"has_embed": 0, "embed_target_id": None, "embed_title": None,
             "ads_present": 0, "shopping_link": None, "product_name": None}
    try:
        r = SHORTS_SESSION.get(f"https://www.youtube.com/shorts/{video_id}",
                               timeout=config.HTML_TIMEOUT)
        if r.status_code != 200:
            return empty
        raw = r.text
    except Exception as e:
        log.warning(f"  HTML fail {video_id}: {e}")
        return empty

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

    return {"has_embed": has_embed, "embed_target_id": embed_target_id,
            "embed_title": embed_title, "ads_present": int(has_ads),
            "shopping_link": shopping_link, "product_name": product_name}


# ──────────── DB helpers ────────────
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
                                 ads_present, shopping_link, product_name, scraped_at_utc)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(shorts_id, scrape_date) DO UPDATE SET
          has_embed=excluded.has_embed,
          embed_target_id=excluded.embed_target_id,
          embed_title=excluded.embed_title,
          ads_present=excluded.ads_present,
          shopping_link=excluded.shopping_link,
          product_name=excluded.product_name,
          scraped_at_utc=excluded.scraped_at_utc
    """, (shorts_id, scrape_date, em["has_embed"], em["embed_target_id"], em["embed_title"],
          em["ads_present"], em["shopping_link"], em["product_name"], scraped_at))

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


# ──────────── main run ────────────
def run(limit=None, dry_run=False, skip_embeds=False):
    scrape_date = today_utc()
    scraped_at = now_utc_iso()

    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

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
             "n_embeds_reparsed": 0, "n_embed_changes": 0}

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
          quota_units_used=?, status=?, notes=? WHERE run_id=?
    """, (now_utc_iso(), stats["n_channels_done"], stats["n_channels_failed"],
          stats["n_videos_updated"], stats["n_new_videos"],
          stats["n_embeds_reparsed"], stats["n_embed_changes"],
          quota.used, status, notes, run_id))
    conn.commit()
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
