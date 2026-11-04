"""
롱폼 링크 댓글 작성자 수집, GitHub Actions판 (2026-11-03, 탐색: CIST 23번 댓글 흐름을 롱폼 대상으로)

대기열: longform_comments_queue.csv (1029_2026_shorts_to_longform/1103_2026_롱폼댓글수집/01_build_queue.py 산출)
  role target  = 나이 14일+ 날에 링크가 걸린 롱폼 181개
  role short   = 그 롱폼에 링크를 건 숏(11/02 v3 스윕) 2,462개
  role control = 대상마다 같은 채널 비교 롱폼 최대 2개
수집: commentThreads(part=snippet, order=time, 100개/쪽 = 1 unit), 최상위 댓글만.
  숏: 1쪽(최신 100개), CIST 11_comment_collect.py와 같음.
  롱폼(target, control): stop_date(대상 첫 링크일 - 30일)보다 오래된 댓글이 나올 때까지 쪽을 넘김. 영상당 최대 80쪽.
쿼터: --cap 상한(기본 3000)까지만 쓰고 끝낸다. 영상 하나를 다 받은 뒤에만 완료로 표시(중간에 끊기면 다음 실행이 처음부터 다시).
  quotaExceeded가 나오면 바로 멈춘다.
DB: COMMENTS_DB (fetch, comments 테이블; CIST comments_pilot.sqlite와 같은 열 + role, pages)

사용: COMMENTS_DB=path YOUTUBE_API_KEY=... python3 longform_comments.py --cap 3000
"""
import os
import sys
import csv
import time
import sqlite3
import argparse
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
QUEUE = os.environ.get("COMMENTS_QUEUE", os.path.join(HERE, "longform_comments_queue.csv"))
DB = os.environ.get("COMMENTS_DB", os.path.expanduser("~/youtube_daily_db/longform_comments_1103.sqlite"))
MAX_PAGES = 80
URL = "https://www.googleapis.com/youtube/v3/commentThreads"


def main(cap, force=False):
    key = config.get_api_key()
    q = list(csv.DictReader(open(QUEUE, encoding="utf-8")))
    q.sort(key=lambda r: int(r["priority"]))
    db = sqlite3.connect(DB)
    db.execute("""CREATE TABLE IF NOT EXISTS fetch (video_id TEXT PRIMARY KEY, role TEXT, status TEXT, n INTEGER,
                  pages INTEGER, total_reported INTEGER, fetched_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS comments (video_id TEXT, comment_id TEXT PRIMARY KEY, author_ch_id TEXT,
                  published_at TEXT, like_count INTEGER)""")
    # 쿼터일(태평양 자정 리셋)마다 한 번, 일별 수집이 끝났을 시간대(06~14시 PT)에만 돈다.
    # 추적기 workflow가 실행마다 이 workflow를 걸어 주므로 여러 번 불려도 하루 상한을 넘지 않게 막는다.
    pt = ZoneInfo("America/Los_Angeles")
    now_pt = datetime.now(pt)
    if not force:
        if not 6 <= now_pt.hour < 14:
            print(f"outside 06-14 PT window ({now_pt:%H:%M} PT), skip", flush=True)
            return
        last = db.execute("SELECT MAX(fetched_at) FROM fetch").fetchone()[0]
        if last and datetime.fromisoformat(last).replace(tzinfo=timezone.utc).astimezone(pt).date() == now_pt.date():
            print(f"already ran this quota day ({now_pt:%Y-%m-%d} PT), skip", flush=True)
            return
    done = {r[0] for r in db.execute("SELECT video_id FROM fetch")}
    todo = [r for r in q if r["video_id"] not in done]
    print(f"queue {len(q):,} | done {len(done):,} | todo {len(todo):,} | cap {cap:,}", flush=True)

    units, stop = 0, ""
    for r in todo:
        if units >= cap:
            stop = "cap"
            break
        v, role, stop_date = r["video_id"], r["role"], r["stop_date"] or ""
        paged = role in ("target", "control")
        token, pages, n, total, status, rows = None, 0, 0, None, "ok", []
        while True:
            params = {"part": "snippet", "videoId": v, "maxResults": 100, "textFormat": "plainText",
                      "order": "time", "key": key}
            if token:
                params["pageToken"] = token
            try:
                resp = requests.get(URL, params=params, timeout=30)
            except Exception as e:
                print(f"  network error {type(e).__name__}, wait 30s", flush=True)
                time.sleep(30)
                continue
            units += 1
            pages += 1
            if resp.status_code != 200:
                try:
                    reason = resp.json()["error"]["errors"][0]["reason"]
                except Exception:
                    reason = ""
                if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
                    stop = f"quota {reason}"
                else:
                    status = f"{resp.status_code}:{reason}"
                break
            js = resp.json()
            items = js.get("items", [])
            total = js.get("pageInfo", {}).get("totalResults")
            for it in items:
                s = it["snippet"]["topLevelComment"]["snippet"]
                rows.append((v, it["id"], (s.get("authorChannelId") or {}).get("value"), s.get("publishedAt"), s.get("likeCount")))
            n += len(items)
            token = js.get("nextPageToken")
            oldest = items[-1]["snippet"]["topLevelComment"]["snippet"].get("publishedAt", "") if items else ""
            if not paged or not token or (stop_date and oldest[:10] < stop_date):
                break
            if pages >= MAX_PAGES:
                status = "ok_capped"
                break
            if units >= cap + MAX_PAGES:   # 한 영상이 상한을 크게 넘지 않게
                stop = "cap mid-video"
                break
            time.sleep(0.05)
        if stop.startswith("quota") or stop == "cap mid-video":
            break
        db.executemany("INSERT OR IGNORE INTO comments VALUES (?,?,?,?,?)", rows)
        db.execute("INSERT OR REPLACE INTO fetch VALUES (?,?,?,?,?,?,datetime('now'))", (v, role, status, n, pages, total))
        db.commit()
        if units // 200 != (units - pages) // 200:
            print(f"  {units:,} units", flush=True)
    st = dict(db.execute("SELECT role || ':' || status, COUNT(*) FROM fetch GROUP BY 1").fetchall())
    left = len([r for r in q if r["video_id"] not in {x[0] for x in db.execute("SELECT video_id FROM fetch")}])
    db.close()
    print(f"used {units:,} units | stop {stop or 'queue end'} | left {left:,} | {st}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=3000)
    ap.add_argument("--force", action="store_true", help="시간대와 하루 1회 제한 무시")
    a = ap.parse_args()
    main(a.cap, a.force)
