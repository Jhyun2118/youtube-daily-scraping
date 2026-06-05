"""
seed_from_csv.py — 1회 실행
기존 youtube_combined_*.csv + playboard CSV를 SQLite에 시드.
실행 후 데이터:
  channels         : 272개 (YT∩PB)
  videos           : 365k
  channels_daily   : Playboard 일별 (2년치, 1.1M행)
  videos_daily     : Y4 스냅샷 1행씩 (스크랩 시점)
  embeds           : 88k엣지
  embeds_daily     : Y4 스냅샷 1행씩
"""
import os
import re
import sqlite3
import warnings
import datetime as dt
import pandas as pd

import config  # DB_PATH from local non-Dropbox dir

warnings.filterwarnings("ignore")

BASE_Y4 = "/Users/jhyunlee/Library/CloudStorage/Dropbox/바탕화면/Gradschool/year4/youtube_api_sample"
DB_PATH = config.DB_PATH
SCHEMA  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
CATS    = ["Education", "Comedy", "Film_Animation", "Howto_Style", "Science_Technology"]

# 기존 YouTube CSV의 스크래핑 시점 (window_end_utc)을 스크랩 날짜로 사용
SNAPSHOT_DATE = "2025-08-07"
NOW = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def init_db():
    if os.path.exists(DB_PATH):
        print(f"  DB already exists: {DB_PATH}")
        print(f"  → delete first if you want to re-seed")
        return False
    conn = sqlite3.connect(DB_PATH)
    with open(SCHEMA) as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()
    print(f"  ✓ created {DB_PATH}")
    return True


def load_videos_from_csv():
    print("\n[1] Loading 5 category CSVs ...")
    frames = []
    for cat in CATS:
        fp = os.path.join(BASE_Y4, "outputs", f"youtube_combined_{cat}.csv")
        df = pd.read_csv(fp, low_memory=False)
        df = df[df["row_type"] == "video"].copy()
        df["category_yt"] = cat
        frames.append(df)
        print(f"  {cat:<22} {len(df):>8,}")
    vids = pd.concat(frames, ignore_index=True)
    vids["is_shorts"] = vids["is_shorts"].astype(str).str.lower().eq("true").astype(int)
    vids["has_embed"] = vids["embed_link"].notna() & (vids["embed_link"].astype(str).str.strip() != "")
    vids["embedded_vid"] = vids["embed_link"].str.extract(r"watch\?v=([A-Za-z0-9_-]+)")
    return vids


def load_playboard():
    print("\n[2] Loading Playboard daily ...")
    fp = os.path.join(BASE_Y4, "playboard_data", "playboard_viewership_combined_fin.csv")
    pb = pd.read_csv(fp, dtype=str)
    pb.columns = [c.strip() for c in pb.columns]
    mask = pb["날짜"].str.match(r"^\d{4}\.\d{2}\.\d{2}$", na=False)
    pb = pb[mask].copy()
    pb["scrape_date"] = pd.to_datetime(pb["날짜"].str.replace(".", "-", regex=False)).dt.strftime("%Y-%m-%d")
    pb["daily_views"] = pd.to_numeric(pb["조회수"].str.replace(",", "", regex=False), errors="coerce")
    pb["cum_views"]   = pd.to_numeric(pb["누적 조회수"].str.replace(",", "", regex=False), errors="coerce")
    pb = pb.rename(columns={"Channel ID": "channel_id", "Category": "category_playboard",
                            "Country Code (ISO 3166)": "country", "Channel Creation Date": "channel_created_at"})
    pb = pb.dropna(subset=["scrape_date", "daily_views"])
    print(f"  PB rows: {len(pb):,}  channels: {pb['channel_id'].nunique()}")
    return pb


def seed_channels(conn, vids, pb):
    print("\n[3] Seeding channels ...")
    yt_ch = vids.groupby("channel_id").agg(
        channel_title=("channel_title", "first"),
        category_yt=("category_yt", "first"),
    ).reset_index()

    pb_ch = pb.groupby("channel_id").agg(
        country=("country", "first"),
        category_playboard=("category_playboard", "first"),
        channel_created_at=("channel_created_at", "first"),
    ).reset_index()

    # 둘 다 인서트 (UNION). 정보 있는 컬럼은 채워둠.
    all_ch = pd.merge(yt_ch, pb_ch, on="channel_id", how="outer")
    all_ch["discovered_at"] = NOW
    all_ch["last_seen_at"] = NOW

    all_ch[["channel_id", "channel_title", "country", "category_playboard",
            "category_yt", "channel_created_at", "discovered_at", "last_seen_at"]
    ].to_sql("channels", conn, if_exists="append", index=False)
    print(f"  ✓ {len(all_ch):,} channels")


def seed_videos(conn, vids):
    print("\n[4] Seeding videos (static) ...")
    v = vids[["video_id", "channel_id", "title", "published_at", "duration_s",
              "category_id", "default_language", "thumbnail_url", "keywords", "is_shorts"]].copy()
    v["description"] = None  # 작년 스크래퍼가 안 가져옴
    v["discovered_at"] = NOW
    v["last_seen_at"] = NOW
    v = v.drop_duplicates("video_id")
    v.to_sql("videos", conn, if_exists="append", index=False,
             dtype={"is_shorts": "INTEGER", "duration_s": "INTEGER", "category_id": "INTEGER"})
    print(f"  ✓ {len(v):,} videos")


def seed_videos_daily(conn, vids):
    print("\n[5] Seeding videos_daily (snapshot 1행) ...")
    vd = vids[["video_id", "view_count", "like_count", "comment_count"]].copy()
    vd["scrape_date"] = SNAPSHOT_DATE
    vd["scraped_at_utc"] = NOW
    vd = vd.drop_duplicates("video_id")
    vd.to_sql("videos_daily", conn, if_exists="append", index=False)
    print(f"  ✓ {len(vd):,} video-day rows")


def seed_channels_daily(conn, vids, pb):
    print("\n[6] Seeding channels_daily ...")
    # Playboard 일별 + YT 1회 (subscriber_count, total_video_count from YT)
    pbd = pb[["channel_id", "scrape_date", "cum_views"]].copy().rename(columns={"cum_views": "total_view_count"})
    pbd["subscriber_count"] = None
    pbd["total_video_count"] = None
    pbd["scraped_at_utc"] = NOW
    pbd = pbd.drop_duplicates(subset=["channel_id", "scrape_date"])
    pbd.to_sql("channels_daily", conn, if_exists="append", index=False)
    print(f"  ✓ {len(pbd):,} PB channel-day rows")

    # YT 스냅샷 채널-day 1행 (subscriber_count)
    yt_cd = vids.groupby("channel_id").agg(
        subscriber_count=("subscriber_count", "first"),
        total_video_count=("channel_video_count_total", "first"),
    ).reset_index()
    yt_cd["scrape_date"] = SNAPSHOT_DATE
    yt_cd["scraped_at_utc"] = NOW
    yt_cd["total_view_count"] = None
    # 같은 (channel_id, scrape_date)가 PB에 이미 있으면 UPSERT 안 됨 → 별도 처리
    # SQLite INSERT OR IGNORE then UPDATE for the YT-side fields
    cur = conn.cursor()
    rows = 0
    for _, r in yt_cd.iterrows():
        cur.execute("""
            INSERT INTO channels_daily(channel_id, scrape_date, subscriber_count, total_view_count, total_video_count, scraped_at_utc)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(channel_id, scrape_date) DO UPDATE SET
              subscriber_count = COALESCE(excluded.subscriber_count, channels_daily.subscriber_count),
              total_video_count = COALESCE(excluded.total_video_count, channels_daily.total_video_count)
        """, (r["channel_id"], r["scrape_date"], r["subscriber_count"],
              r["total_view_count"], r["total_video_count"], r["scraped_at_utc"]))
        rows += 1
    conn.commit()
    print(f"  ✓ {rows:,} YT channel-day upserts")


def seed_embeds(conn, vids):
    print("\n[7] Seeding embeds ...")
    em = vids[(vids["is_shorts"] == 1) & (vids["has_embed"])].copy()
    em = em[em["embedded_vid"].notna()]
    em["is_self_embed"] = em["embedded_vid"].isin(em["channel_id"].map(
        vids.set_index("video_id")["channel_id"]
    )).astype(int)
    # is_self_embed 더 정확하게: embedded_vid의 channel_id == shorts의 channel_id
    vid_to_ch = dict(zip(vids["video_id"], vids["channel_id"]))
    em["target_channel"] = em["embedded_vid"].map(vid_to_ch)
    em["is_self_embed"] = (em["target_channel"] == em["channel_id"]).astype(int)

    edges = em[["video_id", "embedded_vid", "embed_link", "embed_title", "is_self_embed"]].copy()
    edges.columns = ["shorts_id", "embed_target_id", "embed_target_url", "embed_title", "is_self_embed"]
    edges["first_seen_at"] = NOW
    edges["last_seen_at"] = NOW
    edges["removed_at"] = None
    edges = edges.drop_duplicates(subset=["shorts_id", "embed_target_id"])
    edges.to_sql("embeds", conn, if_exists="append", index=False)
    print(f"  ✓ {len(edges):,} embed edges")

    # embeds_daily (snapshot)
    em_d = em[["video_id", "embedded_vid", "embed_title", "ads_present", "shopping_link", "product_name"]].copy()
    em_d.columns = ["shorts_id", "embed_target_id", "embed_title", "ads_present", "shopping_link", "product_name"]
    em_d["has_embed"] = 1
    em_d["scrape_date"] = SNAPSHOT_DATE
    em_d["scraped_at_utc"] = NOW
    em_d["ads_present"] = em_d["ads_present"].astype(str).str.lower().eq("true").astype(int)
    em_d = em_d.drop_duplicates(subset=["shorts_id", "scrape_date"])
    em_d[["shorts_id", "scrape_date", "has_embed", "embed_target_id", "embed_title",
          "ads_present", "shopping_link", "product_name", "scraped_at_utc"]].to_sql(
        "embeds_daily", conn, if_exists="append", index=False
    )
    print(f"  ✓ {len(em_d):,} embed-day snapshots")


def main():
    print("=" * 60)
    print("Seeding youtube_daily.db from existing CSVs")
    print("=" * 60)
    if not init_db():
        return
    vids = load_videos_from_csv()
    pb = load_playboard()
    conn = sqlite3.connect(DB_PATH)
    seed_channels(conn, vids, pb)
    seed_videos(conn, vids)
    seed_videos_daily(conn, vids)
    seed_channels_daily(conn, vids, pb)
    seed_embeds(conn, vids)
    conn.close()

    # 최종 카운트
    conn = sqlite3.connect(DB_PATH)
    print("\n[final counts]")
    for t in ["channels", "videos", "channels_daily", "videos_daily", "embeds", "embeds_daily"]:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<20} {n:>10,}")
    size_mb = os.path.getsize(DB_PATH) / 1024 / 1024
    print(f"\n  DB size: {size_mb:.1f} MB")
    conn.close()


if __name__ == "__main__":
    main()
