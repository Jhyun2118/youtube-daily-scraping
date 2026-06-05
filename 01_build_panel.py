"""
01_build_panel.py
- 5개 카테고리 youtube_combined CSV 통합
- 채널별 treatment_date (첫 embed Shorts published_at) 정의
- Playboard 일별 채널 시계열과 머지 → channel-day 패널
출력:
  data/videos_all.pkl
  data/treatment_summary.csv
  data/channel_day_panel.pkl
"""
import os
import re
import warnings
import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")

BASE_Y4 = "/Users/jhyunlee/Library/CloudStorage/Dropbox/바탕화면/Gradschool/year4/youtube_api_sample"
BASE    = "/Users/jhyunlee/Library/CloudStorage/Dropbox/바탕화면/Gradschool/year5/youtube_daily_scraping"
OUT     = os.path.join(BASE, "data")

CATS = ["Education", "Comedy", "Film_Animation", "Howto_Style", "Science_Technology"]

# ---------- 1) 5개 카테고리 머지 ----------
print("[1] Loading 5 category CSVs ...")
frames = []
for cat in CATS:
    fp = os.path.join(BASE_Y4, "outputs", f"youtube_combined_{cat}.csv")
    df = pd.read_csv(fp, low_memory=False)
    df = df[df["row_type"] == "video"].copy()
    frames.append(df)
    print(f"   {cat:<22} {len(df):>8,} videos")

vids = pd.concat(frames, ignore_index=True)
print(f"   TOTAL videos: {len(vids):,}  ({vids['channel_id'].nunique()} channels)")

# 정리
vids["published_at"] = pd.to_datetime(vids["published_at"], utc=True, errors="coerce")
vids["published_date"] = vids["published_at"].dt.tz_convert(None).dt.normalize()
vids["is_shorts"] = vids["is_shorts"].astype(str).str.lower().eq("true")
vids["has_embed"] = vids["embed_link"].notna() & (vids["embed_link"].astype(str).str.strip() != "")
vids["embedded_vid"] = vids["embed_link"].str.extract(r"watch\?v=([A-Za-z0-9_-]+)")

vids.to_pickle(os.path.join(OUT, "videos_all.pkl"))
print(f"   → saved videos_all.pkl ({os.path.getsize(os.path.join(OUT,'videos_all.pkl'))/1024/1024:.1f} MB)")

# ---------- 2) Treatment 정의 ----------
print("\n[2] Defining treatment per channel ...")
treated_shorts = vids[vids["is_shorts"] & vids["has_embed"]].copy()

# 채널별 첫 임베드 Shorts
first_embed = (
    treated_shorts.groupby("channel_id")["published_date"]
    .min()
    .reset_index()
    .rename(columns={"published_date": "treatment_date"})
)

# 채널 단위 요약
ch_summary = (
    vids.groupby("channel_id")
    .agg(
        channel_title=("channel_title", "first"),
        category=("category", "first"),
        subscriber_count=("subscriber_count", "first"),
        n_videos=("video_id", "nunique"),
        n_shorts=("is_shorts", "sum"),
        n_embed_shorts=("has_embed", lambda s: int(((vids.loc[s.index, "is_shorts"]) & s).sum())),
        first_video_date=("published_date", "min"),
        last_video_date=("published_date", "max"),
    )
    .reset_index()
)
ch_summary = ch_summary.merge(first_embed, on="channel_id", how="left")
ch_summary["is_treated"] = ch_summary["treatment_date"].notna()
ch_summary["embed_intensity"] = ch_summary["n_embed_shorts"] / ch_summary["n_shorts"].replace(0, np.nan)

ch_summary.to_csv(os.path.join(OUT, "treatment_summary.csv"), index=False)
print(f"   Treated channels: {ch_summary['is_treated'].sum()} / {len(ch_summary)}")
print(f"   Treatment date range: {ch_summary['treatment_date'].min()} ~ {ch_summary['treatment_date'].max()}")
print(f"   → saved treatment_summary.csv")

# ---------- 3) Playboard 머지 ----------
print("\n[3] Loading Playboard daily channel time series ...")
pb_fp = os.path.join(BASE_Y4, "playboard_data", "playboard_viewership_combined_fin.csv")
pb = pd.read_csv(pb_fp, dtype=str)
pb.columns = [c.strip() for c in pb.columns]

# 날짜 row만
mask = pb["날짜"].str.match(r"^\d{4}\.\d{2}\.\d{2}$", na=False)
pb = pb[mask].copy()
pb["date"] = pd.to_datetime(pb["날짜"].str.replace(".", "-", regex=False), errors="coerce")
pb["daily_views"] = pd.to_numeric(pb["조회수"].str.replace(",", "", regex=False), errors="coerce")
pb["cum_views"] = pd.to_numeric(pb["누적 조회수"].str.replace(",", "", regex=False), errors="coerce")

pb_keep = pb[["Channel ID", "date", "daily_views", "cum_views"]].rename(columns={"Channel ID": "channel_id"})
pb_keep = pb_keep.dropna(subset=["date", "daily_views"])
print(f"   Playboard rows: {len(pb_keep):,} ({pb_keep['channel_id'].nunique()} channels)")
print(f"   Date range: {pb_keep['date'].min().date()} ~ {pb_keep['date'].max().date()}")

# 교집합: YouTube CSV에 있는 채널만
ch_in_yt = set(ch_summary["channel_id"])
ch_in_pb = set(pb_keep["channel_id"])
both = ch_in_yt & ch_in_pb
print(f"   Channels in YT only: {len(ch_in_yt - ch_in_pb)}")
print(f"   Channels in PB only: {len(ch_in_pb - ch_in_yt)}")
print(f"   Channels in BOTH: {len(both)}")

panel = pb_keep[pb_keep["channel_id"].isin(both)].copy()
panel = panel.merge(
    ch_summary[["channel_id", "channel_title", "category", "subscriber_count",
                "treatment_date", "is_treated", "n_shorts", "n_embed_shorts", "embed_intensity"]],
    on="channel_id", how="left"
)

# Treatment 변수
panel["treatment_date"] = pd.to_datetime(panel["treatment_date"])
panel["days_from_treat"] = (panel["date"] - panel["treatment_date"]).dt.days
panel["post_treat"] = (panel["days_from_treat"] >= 0).astype("Int64")
# never-treated → days_from_treat NaN, post_treat 0
panel.loc[~panel["is_treated"].astype(bool), "post_treat"] = 0

panel.to_pickle(os.path.join(OUT, "channel_day_panel.pkl"))
print(f"\n   Panel rows: {len(panel):,}")
print(f"   → saved channel_day_panel.pkl ({os.path.getsize(os.path.join(OUT,'channel_day_panel.pkl'))/1024/1024:.1f} MB)")

# 빠른 sanity check
print("\n[Sanity] Channel-day panel preview:")
print(panel[["channel_id", "date", "daily_views", "is_treated", "treatment_date", "days_from_treat", "post_treat"]].head(10).to_string())
print("\nTreatment timing histogram:")
print(panel.drop_duplicates("channel_id").groupby([panel["is_treated"], panel["category"]]).size().unstack(fill_value=0))
