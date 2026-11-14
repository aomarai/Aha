-- /summarize (#110): a per-server opt-in (off by default) and a per-user daily quota.
-- Expand only: a nullable-free column with a default, and a new table. The previous release ignores both.
ALTER TABLE guild_settings ADD COLUMN summaries_enabled boolean NOT NULL DEFAULT false;

-- One row per user per UTC day. Only counters, never the text that was summarized.
CREATE TABLE summarize_usage (
    user_id bigint NOT NULL,
    day     date   NOT NULL,
    used    integer NOT NULL CHECK (used >= 0),
    PRIMARY KEY (user_id, day)
);
