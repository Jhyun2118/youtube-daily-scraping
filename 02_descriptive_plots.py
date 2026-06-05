"""
02_descriptive_plots.py
- 첫 descriptive 도표 (treated vs control trajectory)
출력: figs/*.png
"""
import os
import warnings
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib as mpl

warnings.filterwarnings("ignore")
mpl.rcParams["figure.dpi"] = 110
mpl.rcParams["axes.spines.top"] = False
mpl.rcParams["axes.spines.right"] = False

BASE = "/Users/jhyunlee/Library/CloudStorage/Dropbox/바탕화면/Gradschool/year5/youtube_daily_scraping"
DATA = os.path.join(BASE, "data")
FIGS = os.path.join(BASE, "figs")

print("[load]")
panel = pd.read_pickle(os.path.join(DATA, "channel_day_panel.pkl"))
ts = pd.read_csv(os.path.join(DATA, "treatment_summary.csv"))

panel["date"] = pd.to_datetime(panel["date"])
panel["treatment_date"] = pd.to_datetime(panel["treatment_date"])
print(f"  panel: {len(panel):,} rows, {panel['channel_id'].nunique()} channels")
print(f"  treated: {panel.drop_duplicates('channel_id')['is_treated'].sum()}")

# ============================================================
# Fig 1: 채널별 treatment 도입 시점 분포 (staggered DID visualization)
# ============================================================
print("\n[fig 1] Treatment timing histogram")
fig, ax = plt.subplots(figsize=(10, 4.5))
treated = ts[ts["is_treated"] == True].copy()
treated["treatment_date"] = pd.to_datetime(treated["treatment_date"])
treated["treat_month"] = treated["treatment_date"].dt.to_period("M").dt.to_timestamp()

by_cat = treated.groupby(["treat_month", "category"]).size().unstack(fill_value=0)
by_cat.plot(kind="bar", stacked=True, ax=ax, width=0.85, colormap="tab10")
ax.set_xlabel("First embed-Shorts date (month)")
ax.set_ylabel("# channels first treated")
ax.set_title(f"Staggered treatment timing — {len(treated)} treated channels across 5 categories")
ax.legend(title="Category", loc="upper left", fontsize=8)
labels = [d.strftime("%Y-%m") for d in by_cat.index]
ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
plt.tight_layout()
plt.savefig(os.path.join(FIGS, "01_treatment_timing.png"))
plt.close()

# ============================================================
# Fig 2: 카테고리별 채택률 (treated / total)
# ============================================================
print("[fig 2] Adoption rate by category")
adopt = ts.groupby("category").agg(
    n_total=("channel_id", "nunique"),
    n_treated=("is_treated", "sum"),
).reset_index()
adopt["rate"] = adopt["n_treated"] / adopt["n_total"]

fig, ax = plt.subplots(figsize=(8, 4))
ax.bar(adopt["category"], adopt["rate"], color="steelblue")
for i, r in adopt.iterrows():
    ax.text(i, r["rate"] + 0.02, f'{r["n_treated"]}/{r["n_total"]}\n({r["rate"]*100:.0f}%)',
            ha="center", fontsize=9)
ax.set_ylabel("Adoption rate (treated / total channels)")
ax.set_ylim(0, 1.1)
ax.set_title("Embed-Shorts adoption rate by category")
ax.set_xticklabels(adopt["category"], rotation=20, ha="right", fontsize=9)
plt.tight_layout()
plt.savefig(os.path.join(FIGS, "02_adoption_rate.png"))
plt.close()

# ============================================================
# Fig 3: Raw daily view trajectory — treated vs never-treated (calendar time)
# ============================================================
print("[fig 3] Raw trajectory by treatment status (calendar time)")
agg = (panel.groupby(["date", "is_treated"])["daily_views"]
       .mean()
       .reset_index())

fig, ax = plt.subplots(figsize=(11, 4.5))
for tr, g in agg.groupby("is_treated"):
    label = "Treated channels" if tr else "Never-treated channels"
    color = "C3" if tr else "C0"
    # 7-day rolling
    g = g.sort_values("date").copy()
    g["sm"] = g["daily_views"].rolling(7, min_periods=1).mean()
    ax.plot(g["date"], g["sm"], label=label, color=color, lw=1.6)

ax.set_ylabel("Mean daily channel views (7-day MA)")
ax.set_title("Calendar-time trajectory: treated vs never-treated channels")
ax.legend()
ax.set_yscale("log")
plt.tight_layout()
plt.savefig(os.path.join(FIGS, "03_raw_trajectory.png"))
plt.close()

# ============================================================
# Fig 4: Event study — days from treatment (treated only)
# ============================================================
print("[fig 4] Event study around treatment_date")
WINDOW = 180  # ±180 days
es = panel[panel["is_treated"] == True].copy()
es = es[es["days_from_treat"].between(-WINDOW, WINDOW)].copy()
# log views for cleaner comparison
es["log_views"] = np.log1p(es["daily_views"])

# 채널별 z-score → 절대 규모 차이 제거
es["log_views_z"] = es.groupby("channel_id")["log_views"].transform(lambda s: (s - s.mean()) / s.std(ddof=0))

ev = es.groupby("days_from_treat").agg(
    mean_log=("log_views", "mean"),
    mean_z=("log_views_z", "mean"),
    n=("channel_id", "nunique"),
).reset_index()

fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
ax = axes[0]
ax.plot(ev["days_from_treat"], ev["mean_log"], lw=1.4, color="C3")
ax.axvline(0, color="k", ls="--", lw=0.8)
ax.set_xlabel("Days from first embed-Shorts (t=0)")
ax.set_ylabel("Mean log(daily_views + 1)")
ax.set_title(f"Event study (treated channels, ±{WINDOW}d)")

ax = axes[1]
ax.plot(ev["days_from_treat"], ev["mean_z"], lw=1.4, color="C2")
ax.axvline(0, color="k", ls="--", lw=0.8)
ax.axhline(0, color="grey", ls=":", lw=0.6)
ax.set_xlabel("Days from first embed-Shorts (t=0)")
ax.set_ylabel("Within-channel z-score of log views")
ax.set_title("Within-channel normalised (each channel demeaned)")
plt.tight_layout()
plt.savefig(os.path.join(FIGS, "04_event_study.png"))
plt.close()

# ============================================================
# Fig 5: Treatment intensity heterogeneity
# ============================================================
print("[fig 5] Heterogeneity by embed intensity")
ts2 = ts[ts["is_treated"] == True].copy()
ts2 = ts2.dropna(subset=["embed_intensity"])
ts2["intensity_bin"] = pd.qcut(ts2["embed_intensity"], q=4, labels=["Q1 (low)", "Q2", "Q3", "Q4 (high)"])

# join intensity bin into event panel
es2 = es.merge(ts2[["channel_id", "intensity_bin"]], on="channel_id", how="left").dropna(subset=["intensity_bin"])
ev2 = es2.groupby(["days_from_treat", "intensity_bin"])["log_views_z"].mean().reset_index()

fig, ax = plt.subplots(figsize=(10, 4.5))
for q, g in ev2.groupby("intensity_bin"):
    ax.plot(g["days_from_treat"], g["log_views_z"].rolling(7, min_periods=1).mean(),
            lw=1.4, label=str(q))
ax.axvline(0, color="k", ls="--", lw=0.8)
ax.axhline(0, color="grey", ls=":", lw=0.6)
ax.set_xlabel("Days from t=0")
ax.set_ylabel("Within-channel z-score (7-day MA)")
ax.set_title("Heterogeneity: by embed intensity quartile (n_embed_shorts / n_shorts)")
ax.legend(title="Embed intensity")
plt.tight_layout()
plt.savefig(os.path.join(FIGS, "05_heterogeneity_intensity.png"))
plt.close()

# ============================================================
# Fig 6: Heterogeneity by category
# ============================================================
print("[fig 6] Heterogeneity by category")
ev3 = es.merge(ts[["channel_id", "category"]], on="channel_id", how="left", suffixes=("", "_y"))
if "category_y" in ev3.columns:
    ev3["category"] = ev3["category_y"].fillna(ev3["category"])
ev3 = ev3.groupby(["days_from_treat", "category"])["log_views_z"].mean().reset_index()

fig, ax = plt.subplots(figsize=(11, 4.5))
for cat, g in ev3.groupby("category"):
    ax.plot(g["days_from_treat"], g["log_views_z"].rolling(7, min_periods=1).mean(),
            lw=1.3, label=cat)
ax.axvline(0, color="k", ls="--", lw=0.8)
ax.axhline(0, color="grey", ls=":", lw=0.6)
ax.set_xlabel("Days from t=0")
ax.set_ylabel("Within-channel z-score (7-day MA)")
ax.set_title("Heterogeneity by category")
ax.legend(fontsize=8)
plt.tight_layout()
plt.savefig(os.path.join(FIGS, "06_heterogeneity_category.png"))
plt.close()

# ============================================================
# Summary table
# ============================================================
print("\n=== Summary table ===")
summary = pd.DataFrame({
    "metric": [
        "Total channels (in both YT+PB)",
        "Treated channels",
        "Never-treated channels",
        "Adoption rate (overall)",
        "Treatment date min",
        "Treatment date max",
        "Panel rows (channel-days)",
        "Date range (PB)",
        "Mean pre-treat log_views",
        "Mean post-treat log_views",
    ],
    "value": [
        panel["channel_id"].nunique(),
        panel.drop_duplicates("channel_id")["is_treated"].sum(),
        (~panel.drop_duplicates("channel_id")["is_treated"]).sum(),
        f"{panel.drop_duplicates('channel_id')['is_treated'].mean()*100:.1f}%",
        ts[ts["is_treated"]==True]["treatment_date"].min(),
        ts[ts["is_treated"]==True]["treatment_date"].max(),
        len(panel),
        f"{panel['date'].min().date()} ~ {panel['date'].max().date()}",
        f"{es.loc[es['days_from_treat']<0, 'log_views'].mean():.3f}",
        f"{es.loc[es['days_from_treat']>=0, 'log_views'].mean():.3f}",
    ]
})
print(summary.to_string(index=False))
summary.to_csv(os.path.join(DATA, "panel_summary.csv"), index=False)

print(f"\n[done] 6 figures saved to {FIGS}")
for f in sorted(os.listdir(FIGS)):
    print(f"  {f}")
