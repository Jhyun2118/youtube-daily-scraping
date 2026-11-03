"""
링크카드 부착 시점 추적기, GitHub Actions판 (2026-11-03)

원본: 1029_2026_shorts_to_longform/track_link_timing.py (맥에서 11/02 02:43 UTC부터 돌던 것).
JH 출장(11/04 비행) 동안 맥 없이 이어 돌리려고 옮긴 판. 같은 DB를 B2에서 받아 이어 쓴다.

원본과 다른 점
  - 채널 목록: tracker_channels.csv(11/03 원본 channels() 결과를 고정, 387채널). 라이브 DB 없이 돈다.
  - 한 번 실행에 --max-minutes 동안만 돌고 끝난다(Actions 작업 한도). 다음 실행이 DB를 이어받는다.
  - 다음 탐색 회차 시각은 메모리가 아니라 DB의 마지막 회차(tab_fetches)에서 계산한다.
  - 다 끝나면 meta에 done=1을 쓰고 이후 실행은 바로 끝난다.
나머지(회차 2시간, 발견 7일, 재확인 +0/2/6/24/72h, 3회 재시도, 요청 간격)는 원본과 같다.
Actions 일정 지연으로 회차와 재확인 시각이 원본보다 늦어질 수 있다. 실제 시각은 done_utc, cycle_utc에 남는다.

사용: TRACKER_DB=path python3 track_link_timing_cloud.py --max-minutes 100
"""
import os
import re
import sys
import csv
import time
import sqlite3
import argparse
import logging
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrape_daily as sd  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DB = os.environ.get("TRACKER_DB", os.path.expanduser("~/youtube_daily_db/link_timing_tracker.db"))
CHANNELS_CSV = os.path.join(HERE, "tracker_channels.csv")
CYCLE_S = 2 * 3600
DISCOVERY_DAYS = 7
FOLLOWUPS_H = [0, 2, 6, 24, 72]
REQ_DELAY_S = 0.6

root = logging.getLogger()
for h in list(root.handlers):
    root.removeHandler(h)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler()])
log = logging.getLogger("tracker")

LOCKUP_ID = re.compile(r'"shortsLockupViewModel":\{.{0,800}?"videoId":"([A-Za-z0-9_-]{11})"', re.S)


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.isoformat(timespec="seconds")


def channels():
    with open(CHANNELS_CSV, encoding="utf-8") as f:
        return {r["channel_id"]: r["source"] for r in csv.DictReader(f)}


def fetch_tab(cid):
    try:
        r = sd.SHORTS_SESSION.get(f"https://www.youtube.com/channel/{cid}/shorts", timeout=20)
        if r.status_code != 200:
            return None
        t = r.text
        i = t.find("var ytInitialData")
        if i < 0:
            return None
        ids = []
        for m in LOCKUP_ID.finditer(t, i):
            if m.group(1) not in ids:
                ids.append(m.group(1))
        return ids
    except Exception as e:
        log.warning(f"tab fail {cid}: {e}")
        return None


def run_check(con, sid, off):
    em = sd.parse_shorts_embed(sid)
    ok = em.get("has_embed") is not None
    con.execute("""UPDATE checks SET tries=tries+1, done_utc=?, http_status=?, has_embed=?, target_id=?,
                   target_kind=?, embed_title=?, embed_source=? WHERE short_id=? AND offset_h=?""",
                (iso(now()) if ok else None, em.get("http_status"), em.get("has_embed"),
                 em.get("embed_target_id"), em.get("embed_target_kind"), em.get("embed_title"),
                 em.get("embed_source"), sid, off))
    if not ok:
        con.execute("UPDATE checks SET due_utc=? WHERE short_id=? AND offset_h=? AND tries<3",
                    (iso(now() + dt.timedelta(minutes=10)), sid, off))
    con.commit()
    time.sleep(REQ_DELAY_S)


def due_checks(con):
    return con.execute("""SELECT short_id, offset_h FROM checks
                          WHERE done_utc IS NULL AND tries < 3 AND due_utc <= ? ORDER BY due_utc""",
                       (iso(now()),)).fetchall()


def discovery_cycle(con, chans, prev_cycle, deadline):
    t = now()
    known = {r[0] for r in con.execute("SELECT short_id FROM shorts")}
    n_new = n_fail = 0
    for cid, src in chans.items():
        ids = fetch_tab(cid)
        con.execute("INSERT INTO tab_fetches VALUES (?,?,?,?)", (iso(t), cid, int(ids is not None), len(ids or [])))
        if ids is None:
            n_fail += 1
        else:
            for sid in ids:
                if sid in known:
                    continue
                known.add(sid)
                con.execute("INSERT INTO shorts VALUES (?,?,?,?,?,?)", (sid, cid, src, 0, iso(now()), prev_cycle))
                n_new += 1
                seen = now()
                con.executemany("INSERT OR IGNORE INTO checks(short_id, offset_h, due_utc) VALUES (?,?,?)",
                                [(sid, h, iso(seen + dt.timedelta(hours=h))) for h in FOLLOWUPS_H])
        con.commit()
        time.sleep(REQ_DELAY_S)
        for sid, off in due_checks(con):
            run_check(con, sid, off)
            if now() >= deadline:
                break
    log.info(f"cycle {iso(t)} | channels {len(chans)} (tab fail {n_fail}) | new shorts {n_new:,}")


def main(max_minutes):
    con = sqlite3.connect(OUT_DB)
    if con.execute("SELECT v FROM meta WHERE k='done'").fetchone():
        log.info("already done")
        return
    start = con.execute("SELECT v FROM meta WHERE k='start_utc'").fetchone()[0]
    stop_discovery = dt.datetime.fromisoformat(start) + dt.timedelta(days=DISCOVERY_DAYS)
    chans = channels()
    deadline = now() + dt.timedelta(minutes=max_minutes)
    log.info(f"start {start} | channels {len(chans)} | discovery until {iso(stop_discovery)} | run until {iso(deadline)}")
    while now() < deadline:
        last = con.execute("SELECT MAX(cycle_utc) FROM tab_fetches").fetchone()[0]
        next_cycle = dt.datetime.fromisoformat(last) + dt.timedelta(seconds=CYCLE_S) if last else now()
        if now() < stop_discovery and now() >= next_cycle:
            discovery_cycle(con, chans, last, deadline)
        for sid, off in due_checks(con):
            run_check(con, sid, off)
            if now() >= deadline:
                break
        pending = con.execute("SELECT COUNT(*) FROM checks WHERE done_utc IS NULL AND tries < 3").fetchone()[0]
        if now() >= stop_discovery and pending == 0:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('done', ?)", (iso(now()),))
            con.commit()
            log.info("TRACKER_DONE")
            break
        time.sleep(30)
    n_new = con.execute("SELECT COUNT(*) FROM shorts WHERE baseline=0").fetchone()[0]
    pend = con.execute("SELECT COUNT(*) FROM checks WHERE done_utc IS NULL AND tries < 3").fetchone()[0]
    log.info(f"exit | new shorts {n_new:,} | pending checks {pend:,}")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=float, default=100)
    main(ap.parse_args().max_minutes)
