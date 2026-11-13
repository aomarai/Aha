-- Per-guild settings (#98). One row per guild that has changed anything; no row means defaults.
-- guild_id is the key, updated_by is the Discord user who last changed it (see aha.datainventory:
-- the row is deleted with the guild, 30 days after the bot is removed from it).
CREATE TABLE guild_settings (
    guild_id          bigint      PRIMARY KEY,
    locale            text,
    disabled_commands text[]      NOT NULL DEFAULT '{}' CHECK (cardinality(disabled_commands) <= 100),
    log_channel_id    bigint,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    updated_by        bigint      NOT NULL
);
