-- ============================================================
-- YouTube daily scraping schema
-- ============================================================

-- Static channel info (slowly changing)
CREATE TABLE IF NOT EXISTS channels (
    channel_id          TEXT PRIMARY KEY,
    channel_title       TEXT,
    country             TEXT,
    category_playboard  TEXT,
    category_yt         TEXT,
    channel_created_at  TEXT,
    discovered_at       TEXT NOT NULL,    -- when first added to our DB
    last_seen_at        TEXT NOT NULL     -- last successful scrape
);

-- Static video info (slowly changing fields; engagement goes in videos_daily)
CREATE TABLE IF NOT EXISTS videos (
    video_id            TEXT PRIMARY KEY,
    channel_id          TEXT NOT NULL,
    title               TEXT,
    description         TEXT,
    published_at        TEXT,
    duration_s          INTEGER,
    category_id         INTEGER,
    default_language    TEXT,
    thumbnail_url       TEXT,
    keywords            TEXT,
    is_shorts           INTEGER,          -- 0/1, determined at discovery; can be re-evaluated
    discovered_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
);
CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_videos_published ON videos(published_at);
CREATE INDEX IF NOT EXISTS idx_videos_shorts ON videos(is_shorts);

-- Daily channel snapshot (panel)
CREATE TABLE IF NOT EXISTS channels_daily (
    channel_id              TEXT NOT NULL,
    scrape_date             TEXT NOT NULL,    -- YYYY-MM-DD (UTC)
    subscriber_count        INTEGER,
    total_view_count        INTEGER,
    total_video_count       INTEGER,
    scraped_at_utc          TEXT NOT NULL,
    PRIMARY KEY (channel_id, scrape_date),
    FOREIGN KEY (channel_id) REFERENCES channels(channel_id)
);
CREATE INDEX IF NOT EXISTS idx_chday_date ON channels_daily(scrape_date);

-- Daily video snapshot (panel) — THE KEY MISSING DATA
CREATE TABLE IF NOT EXISTS videos_daily (
    video_id            TEXT NOT NULL,
    scrape_date         TEXT NOT NULL,
    view_count          INTEGER,
    like_count          INTEGER,
    comment_count       INTEGER,
    scraped_at_utc      TEXT NOT NULL,
    PRIMARY KEY (video_id, scrape_date),
    FOREIGN KEY (video_id) REFERENCES videos(video_id)
);
CREATE INDEX IF NOT EXISTS idx_vidday_date ON videos_daily(scrape_date);

-- Embed mapping (current state) — one row per (shorts -> longform) edge
CREATE TABLE IF NOT EXISTS embeds (
    shorts_id           TEXT NOT NULL,
    embed_target_id     TEXT,             -- the longform video_id
    embed_target_url    TEXT,
    embed_title         TEXT,
    is_self_embed       INTEGER,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    removed_at          TEXT,             -- when embed disappeared (NULL = still present)
    PRIMARY KEY (shorts_id, embed_target_id),
    FOREIGN KEY (shorts_id) REFERENCES videos(video_id)
);
CREATE INDEX IF NOT EXISTS idx_embed_target ON embeds(embed_target_id);

-- Daily embed-state snapshot (audit log for embed changes)
CREATE TABLE IF NOT EXISTS embeds_daily (
    shorts_id           TEXT NOT NULL,
    scrape_date         TEXT NOT NULL,
    has_embed           INTEGER,          -- 0/1
    embed_target_id     TEXT,
    embed_title         TEXT,
    ads_present         INTEGER,
    shopping_link       TEXT,
    product_name        TEXT,
    scraped_at_utc      TEXT NOT NULL,
    PRIMARY KEY (shorts_id, scrape_date)
);
CREATE INDEX IF NOT EXISTS idx_embedday_date ON embeds_daily(scrape_date);

-- Scrape run log
CREATE TABLE IF NOT EXISTS scrape_log (
    run_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scrape_date         TEXT NOT NULL,
    started_at_utc      TEXT NOT NULL,
    ended_at_utc        TEXT,
    n_channels_done     INTEGER DEFAULT 0,
    n_channels_failed   INTEGER DEFAULT 0,
    n_videos_updated    INTEGER DEFAULT 0,
    n_new_videos        INTEGER DEFAULT 0,
    n_embeds_reparsed   INTEGER DEFAULT 0,
    n_embed_changes     INTEGER DEFAULT 0,
    quota_units_used    INTEGER DEFAULT 0,
    status              TEXT,             -- 'running' / 'success' / 'partial' / 'failed'
    notes               TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_date ON scrape_log(scrape_date);
