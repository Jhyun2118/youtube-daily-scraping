"""
03_left_censoring_fix.py
- Left-censoring 보정: cohort을 "윈도우 시작 후 N개월 이후 첫 임베드 등장" 채널만으로 제한
- Pre-trend regression test (treated cohort의 t<0 구간 기울기 검정)
- Cutoff sensitivity sweep: N=3, 6, 9, 12개월
- 깨끗한 cohort으로 event study 재생성
출력: figs/07_*.png, data/clean_treatment_summary.csv
"""
import os
import warnings
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib as mpl
from scipy import stats

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
ts["treatment_date"] = pd.to_datetime(ts["treatment_date"])

PB_START = panel["date"].min()           # 2023-08-06
PB_END   = panel["date"].max()           # 2025-08-04
print(f"  Playboard window: {PB_START.date()} ~ {PB_END.date()}")
print(f"  Treated (raw):    {ts['is_treated'].sum()}")

# ============================================================
# 1) Cutoff sensitivity sweep
# ============================================================
print("\n[1] Cutoff sensitivity sweep")
sweep_rows = []
for n_pre in [0, 3, 6, 9, 12]:
    for n_post in [0, 3]:
        cutoff_start = PB_START + pd.DateOffset(months=n_pre)
        cutoff_end   = PB_END   - pd.DateOffset(months=n_post)
        clean = ts[
            (ts["is_treated"] == True) &
            (ts["treatment_date"] >= cutoff_start) &
            (ts["treatment_date"] <= cutoff_end)
        ]
        sweep_rows.append({
            "pre_buffer_months": n_pre, "post_buffer_months": n_post,
            "cutoff_start": cutoff_start.date(), "cutoff_end": cutoff_end.date(),
            "n_clean_treated": len(clean)
        })
sweep_df = pd.DataFrame(sweep_rows)
print(sweep_df.to_string(index=False))

# ============================================================
# 2) Pre-trend regression test (raw cohort, treated only, t<0)
# ============================================================
print("\n[2] Pre-trend regression — raw cohort (treated only)")
def pre_trend_test(panel_sub, window_lo=-180, window_hi=-1):
    """채널별 demean된 log_views를 t에 회귀 → 기울기 0 검정"""
    es = panel_sub[panel_sub["is_treated"] == True].copy()
    es = es[es["days_from_treat"].between(window_lo, window_hi)].copy()
    es["log_views"] = np.log1p(es["daily_views"])
    es["log_views_dm"] = es.groupby("channel_id")["log_views"].transform(lambda s: s - s.mean())
    x = es["days_from_treat"].values
    y = es["log_views_dm"].values
    mask = np.isfinite(x) & np.isfinite(y)
    slope, intercept, r, p, se = stats.linregress(x[mask], y[mask])
    return {"n_obs": int(mask.sum()), "n_channels": es["channel_id"].nunique(),
            "slope_per_day": slope, "se": se, "t": slope/se if se>0 else np.nan, "p": p}

raw_pt = pre_trend_test(panel)
print(f"   raw: n={raw_pt['n_obs']:,} obs, {raw_pt['n_channels']} ch")
print(f"   slope = {raw_pt['slope_per_day']:.5f} log-views/day  (SE={raw_pt['se']:.5f}, p={raw_pt['p']:.3g})")
print(f"   → equivalent to {raw_pt['slope_per_day']*30*100:.2f}% / month")

# ============================================================
# 3) Clean cohort 만들기 (pre_buffer=6mo, post_buffer=3mo)
# ============================================================
N_PRE, N_POST = 6, 3
cutoff_start = PB_START + pd.DateOffset(months=N_PRE)
cutoff_end   = PB_END   - pd.DateOffset(months=N_POST)
print(f"\n[3] Clean cohort: treatment_date in [{cutoff_start.date()}, {cutoff_end.date()}]")
clean_treated = ts[
    (ts["is_treated"] == True) &
    (ts["treatment_date"] >= cutoff_start) &
    (ts["treatment_date"] <= cutoff_end)
]["channel_id"].tolist()
print(f"   clean treated: {len(clean_treated)}")

# Never-treated은 그대로 유지
never_treated = ts[ts["is_treated"] == False]["channel_id"].tolist()
print(f"   never-treated controls: {len(never_treated)}")

keep_channels = set(clean_treated) | set(never_treated)
panel_clean = panel[panel["channel_id"].isin(keep_channels)].copy()
panel_clean["is_treated_clean"] = panel_clean["channel_id"].isin(clean_treated)

panel_clean.to_pickle(os.path.join(DATA, "channel_day_panel_clean.pkl"))

# clean cohort에 대한 pre-trend test
clean_pt = pre_trend_test(panel_clean[panel_clean["channel_id"].isin(clean_treated)])
print(f"   clean cohort pre-trend slope = {clean_pt['slope_per_day']:.5f} log-views/day (p={clean_pt['p']:.3g})")
print(f"   → {clean_pt['slope_per_day']*30*100:.2f}% / month  (vs raw: {raw_pt['slope_per_day']*30*100:.2f}%)")

# ============================================================
# 4) Event study by cutoff (sensitivity)
# ============================================================
print("\n[4] Event study sensitivity by cutoff")
WIN = 180
fig, axes = plt.subplots(1, 2, figsize=(13, 4.8))

cutoffs = [0, 3, 6, 9, 12]
colors = plt.cm.viridis(np.linspace(0, 0.85, len(cutoffs)))

for j, ax in enumerate(axes):
    metric = "log_views" if j == 0 else "log_views_z"
    for i, n_pre in enumerate(cutoffs):
        cs = PB_START + pd.DateOffset(months=n_pre)
        ce = PB_END   - pd.DateOffset(months=3)
        treated_ids = ts[
            (ts["is_treated"] == True) &
            (ts["treatment_date"] >= cs) &
            (ts["treatment_date"] <= ce)
        ]["channel_id"].tolist()
        if len(treated_ids) < 5:
            continue
        sub = panel[panel["channel_id"].isin(treated_ids)].copy()
        sub = sub[sub["days_from_treat"].between(-WIN, WIN)].copy()
        sub["log_views"] = np.log1p(sub["daily_views"])
        sub["log_views_z"] = sub.groupby("channel_id")["log_views"].transform(lambda s: (s - s.mean()) / s.std(ddof=0))
        ev = sub.groupby("days_from_treat")[metric].mean().reset_index()
        ev["sm"] = ev[metric].rolling(7, min_periods=1).mean()
        ax.plot(ev["days_from_treat"], ev["sm"], color=colors[i], lw=1.4,
                label=f"≥{n_pre}mo pre-buffer (n={len(treated_ids)})")
    ax.axvline(0, color="k", ls="--", lw=0.8)
    if j == 1:
        ax.axhline(0, color="grey", ls=":", lw=0.6)
    ax.set_xlabel("Days from t=0")
    ax.set_ylabel("Mean log(daily_views+1)" if j == 0 else "Within-channel z-score")
    ax.set_title("Raw level" if j == 0 else "Within-channel normalised")
    ax.legend(fontsize=8, loc="best")

plt.suptitle("Event study sensitivity to left-censoring cutoff", y=1.02)
plt.tight_layout()
plt.savefig(os.path.join(FIGS, "07_cutoff_sensitivity.png"), bbox_inches="tight")
plt.close()

# ============================================================
# 5) Final clean event study (6mo pre, 3mo post buffer)
# ============================================================
print(f"\n[5] Final clean event study (cohort n={len(clean_treated)})")
sub = panel[panel["channel_id"].isin(clean_treated)].copy()
sub = sub[sub["days_from_treat"].between(-WIN, WIN)].copy()
sub["log_views"] = np.log1p(sub["daily_views"])
sub["log_views_z"] = sub.groupby("channel_id")["log_views"].transform(lambda s: (s - s.mean()) / s.std(ddof=0))

ev = sub.groupby("days_from_treat").agg(
    mean_log=("log_views", "mean"),
    mean_z=("log_views_z", "mean"),
    se_z=("log_views_z", lambda s: s.std(ddof=0)/np.sqrt(len(s))),
    n=("channel_id", "nunique"),
).reset_index()

# pre-trend fit overlay
pre = ev[ev["days_from_treat"] < 0]
post = ev[ev["days_from_treat"] >= 0]
pre_slope, pre_int, _, _, pre_se = stats.linregress(pre["days_from_treat"], pre["mean_z"])
post_slope, post_int, _, _, post_se = stats.linregress(post["days_from_treat"], post["mean_z"])

fig, axes = plt.subplots(1, 2, figsize=(13, 4.8))
ax = axes[0]
ax.plot(ev["days_from_treat"], ev["mean_log"].rolling(7, min_periods=1).mean(), lw=1.6, color="C3")
ax.axvline(0, color="k", ls="--", lw=0.8)
ax.set_xlabel("Days from t=0")
ax.set_ylabel("Mean log(daily_views+1)")
ax.set_title(f"Clean cohort (≥6mo pre, ≤3mo from end), n={len(clean_treated)} ch")

ax = axes[1]
ax.plot(ev["days_from_treat"], ev["mean_z"].rolling(7, min_periods=1).mean(), lw=1.6, color="C2")
ax.fill_between(ev["days_from_treat"],
                (ev["mean_z"] - 1.96*ev["se_z"]).rolling(7, min_periods=1).mean(),
                (ev["mean_z"] + 1.96*ev["se_z"]).rolling(7, min_periods=1).mean(),
                alpha=0.18, color="C2")
ax.plot(pre["days_from_treat"], pre_slope*pre["days_from_treat"]+pre_int,
        ls="--", color="navy", lw=1, label=f"pre slope = {pre_slope:.5f}/day (p={'<.001' if pre_se>0 and abs(pre_slope/pre_se)>3 else '>.05'})")
ax.plot(post["days_from_treat"], post_slope*post["days_from_treat"]+post_int,
        ls="--", color="darkred", lw=1, label=f"post slope = {post_slope:.5f}/day")
ax.axvline(0, color="k", ls="--", lw=0.8)
ax.axhline(0, color="grey", ls=":", lw=0.6)
ax.set_xlabel("Days from t=0")
ax.set_ylabel("Within-channel z-score")
ax.set_title("Within-channel demeaned (95% CI shaded)")
ax.legend(fontsize=8)
plt.tight_layout()
plt.savefig(os.path.join(FIGS, "08_clean_event_study.png"))
plt.close()

# ============================================================
# 6) Placebo: never-treated에 가짜 treatment date 부여
# ============================================================
print("\n[6] Placebo test — never-treated channels with random fake treatment dates")
np.random.seed(42)
# Each never-treated channel gets a fake t=0, drawn from the same distribution as real treatment dates
real_dates = ts[ts["is_treated"]==True]["treatment_date"].dropna().reset_index(drop=True)
placebo_dates = np.random.choice(real_dates.values, size=len(never_treated))

placebo_map = dict(zip(never_treated, pd.to_datetime(placebo_dates)))
panel_pb = panel[panel["channel_id"].isin(never_treated)].copy()
panel_pb["fake_treat_date"] = panel_pb["channel_id"].map(placebo_map)
panel_pb["fake_days"] = (panel_pb["date"] - panel_pb["fake_treat_date"]).dt.days

pb = panel_pb[panel_pb["fake_days"].between(-WIN, WIN)].copy()
pb["log_views"] = np.log1p(pb["daily_views"])
pb["log_views_z"] = pb.groupby("channel_id")["log_views"].transform(lambda s: (s - s.mean()) / s.std(ddof=0))

ev_pb = pb.groupby("fake_days").agg(
    mean_log=("log_views", "mean"),
    mean_z=("log_views_z", "mean"),
).reset_index()

fig, ax = plt.subplots(figsize=(11, 4.5))
ax.plot(ev_pb["fake_days"], ev_pb["mean_z"].rolling(7, min_periods=1).mean(),
        color="grey", lw=1.4, label=f"Placebo (never-treated, n={len(never_treated)})")
ax.plot(ev["days_from_treat"], ev["mean_z"].rolling(7, min_periods=1).mean(),
        color="C2", lw=1.6, label=f"Real treated (clean, n={len(clean_treated)})")
ax.axvline(0, color="k", ls="--", lw=0.8)
ax.axhline(0, color="grey", ls=":", lw=0.6)
ax.set_xlabel("Days from t=0 (real or placebo)")
ax.set_ylabel("Within-channel z-score (7-day MA)")
ax.set_title("Placebo test: real vs fake treatment dates")
ax.legend()
plt.tight_layout()
plt.savefig(os.path.join(FIGS, "09_placebo.png"))
plt.close()

# ============================================================
# 7) ATT 추정 (simple TWFE on clean cohort + never-treated)
# ============================================================
print("\n[7] Simple TWFE estimate (clean cohort + never-treated)")
twfe = panel[panel["channel_id"].isin(set(clean_treated) | set(never_treated))].copy()
twfe["is_clean_treated"] = twfe["channel_id"].isin(clean_treated)
twfe["post"] = ((twfe["is_clean_treated"]) & (twfe["days_from_treat"] >= 0)).astype(int)
twfe["log_views"] = np.log1p(twfe["daily_views"])

# 채널 FE + 날짜 FE를 within-transformation으로 처리
twfe["log_views_dm_ch"] = twfe.groupby("channel_id")["log_views"].transform(lambda s: s - s.mean())
twfe["post_dm_ch"] = twfe.groupby("channel_id")["post"].transform(lambda s: s - s.mean())
twfe["log_views_dm"] = twfe.groupby("date")["log_views_dm_ch"].transform(lambda s: s - s.mean())
twfe["post_dm"] = twfe.groupby("date")["post_dm_ch"].transform(lambda s: s - s.mean())

mask = np.isfinite(twfe["log_views_dm"]) & np.isfinite(twfe["post_dm"])
x = twfe.loc[mask, "post_dm"].values
y = twfe.loc[mask, "log_views_dm"].values
beta = (x @ y) / (x @ x)
# cluster-robust SE는 생략, 단순 SE만
resid = y - beta*x
n = len(x); k = 1
se = np.sqrt((resid @ resid)/(n-k)) / np.sqrt(x @ x)
print(f"   ATT (post coef) = {beta:.4f} log-views  (naive SE={se:.4f}, t={beta/se:.2f})")
print(f"   → {(np.exp(beta)-1)*100:.2f}% change in daily views post-treatment, two-way FE")

# Save sweep + summary
sweep_df.to_csv(os.path.join(DATA, "cutoff_sweep.csv"), index=False)
pd.DataFrame([
    {"metric": "raw pre-trend slope (log-views/day)", "value": f"{raw_pt['slope_per_day']:.5f}", "p": f"{raw_pt['p']:.3g}"},
    {"metric": "clean pre-trend slope (log-views/day)", "value": f"{clean_pt['slope_per_day']:.5f}", "p": f"{clean_pt['p']:.3g}"},
    {"metric": "ATT (TWFE log-views)", "value": f"{beta:.4f}", "p": f"t={beta/se:.2f}"},
    {"metric": "ATT (% change)", "value": f"{(np.exp(beta)-1)*100:.2f}%", "p": ""},
    {"metric": "n clean treated", "value": str(len(clean_treated)), "p": ""},
    {"metric": "n never-treated", "value": str(len(never_treated)), "p": ""},
]).to_csv(os.path.join(DATA, "left_censoring_results.csv"), index=False)

print(f"\n[done] saved: 07_cutoff_sensitivity.png, 08_clean_event_study.png, 09_placebo.png")
print(f"[done] data: cutoff_sweep.csv, left_censoring_results.csv, channel_day_panel_clean.pkl")
