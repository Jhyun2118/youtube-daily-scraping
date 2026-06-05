# YouTube Shorts-Longform Embed 프로젝트 현황 노트
*작성 2026-06-05*

## RQ
숏폼 영상에 임베드된 (롱폼) 링크를 통한 transition이 영상 engagement 지표와 채널 성장에 어떤 영향을 미치는가.

## 데이터 인벤토리

| 소스 | 단위 | 시계열 | 비고 |
|---|---|---|---|
| `youtube_combined_*.csv` (5 카테고리) | 영상 | 1회 스냅샷 (2025-08-07) | 365,375 영상, 272 채널 |
| `playboard_viewership_combined_fin.csv` | 채널 | 일별, 2023-08-06 ~ 2025-08-04 (730일) | 2,006 채널 |
| 머지 가능 채널 (intersection) | 채널 | 양쪽 | **272** |
| 확장 가능 풀 (PB 단독) | 채널 | PB만 | 1,734 |

### 임베드 풍부도 (5 카테고리 합)
- Shorts 240,154개 중 37%가 embed_link 보유 (88,319건)
- Self-embed 비율 88-100% (cross-embed 거의 없음)
- 임베드 사용 채널 187/272 = 69%

## 분석 결과 (요약)

채널-day 패널 152,251행으로 staggered DID + event study 수행.

### 1차 발견 *(naive DID)*
- t=0 (각 채널의 첫 임베드 Shorts 게시일) 직후 daily_views level jump 약 +0.3~0.4 SD (z-score)
- Pre-period에도 강한 우상향 트렌드 존재

### 2차 검증 *(left-censoring 보정)*
| 진단 | 결과 |
|---|---|
| Pre-trend slope (raw cohort, n=133) | +0.0058 log-views/day, *p* < .001 (+17.6%/월) |
| Pre-trend slope (clean cohort, 6mo buffer, n=78) | +0.0091 log-views/day, *p* < .001 (+27.3%/월) |
| Cutoff sensitivity 0/3/6/9/12mo | 모두 동일한 post-jump 패턴 → 셀렉션이 본질 |
| Placebo (never-treated에 가짜 t=0) | t=0 점프는 **없음**, post-rise는 **있음** |
| Naive TWFE ATT | 1.77 log-views (*t* = 1.64, *n.s.*) |

### 해석
- 임베드 도입 채널 = 도입 전부터 이미 성장 중 *(★★★ 셀렉션 확정)*
- t=0 level discontinuity 존재 *(★★ placebo에 없음 → 진짜)*
- "임베드가 growth rate를 가속" 가설은 미지지 *(post slope ≤ pre slope)*
- 단순 DID는 secular trend로 인한 과대추정

## 식별 한계
1. Treatment timing이 부정확. `published_at`을 t=0로 썼지만 임베드는 사후 추가/제거 가능 (작년 데이터로 검증 불가).
2. Outcome이 채널 레벨. 임베드 받은 **특정 롱폼**의 trajectory는 못 봄.
3. 스냅샷 시점 (2025-08-07) 임베드 ≠ 게시 시점 임베드 (left-truncation on the edge).
4. Selection on growth: 단순 DID·TWFE 적용 불가.

## 다음 단계

### 즉시 (영상-day 패널 구축)
매일 스크래핑 시작. 매 1일 누적은 향후 식별 자원.
- 영상 일별 view/like/comment 패널 (현재 결손 1순위)
- 임베드 변경 추적 (Shorts 재스크랩으로 timing 정밀화)
- 신규 업로드 감지

### 데이터 누적 후 (6-12개월)
- Synthetic control: 채널별 pre-period trajectory 매칭
- Time-RDD: t=0 ±14일 윈도우, level discontinuity만 식별
- Callaway-Sant'Anna staggered DID with proper control units
- 영상 레벨 event study (임베드 받은 시점 정밀 식별 후)

### 페이퍼 후보
- (A) **Cross-sectional descriptive**: 채택률, 네트워크 구조, heterogeneity. 기존 데이터로 가능.
- (B) **Channel-level evidence**: 셀렉션·trend·jump의 anatomy. 기존 데이터로 가능 (정직한 limitation 명시).
- (C) **Video-level causal**: 영상 패널 구축 후. 6-12개월 후.

## 산출물
```
year5/youtube_daily_scraping/
├── STATUS.md (this)
├── 01_build_panel.py
├── 02_descriptive_plots.py
├── 03_left_censoring_fix.py
├── data/
│   ├── videos_all.pkl              (147 MB)
│   ├── channel_day_panel.pkl       (14 MB)
│   ├── channel_day_panel_clean.pkl
│   ├── treatment_summary.csv
│   ├── cutoff_sweep.csv
│   ├── left_censoring_results.csv
│   └── panel_summary.csv
└── figs/
    ├── 01_treatment_timing.png
    ├── 02_adoption_rate.png
    ├── 03_raw_trajectory.png
    ├── 04_event_study.png
    ├── 05_heterogeneity_intensity.png
    ├── 06_heterogeneity_category.png
    ├── 07_cutoff_sensitivity.png
    ├── 08_clean_event_study.png
    └── 09_placebo.png
```
