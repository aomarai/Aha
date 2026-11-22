-- Show-release reminders (/watch): one row per user per show. The next notification is a
-- scheduled_jobs row (kind 'show') referenced by job_id; advancing the chain is a compare-and-swap
-- on job_id, so a redelivered job can never start a second chain.
-- Expand only: a new table, which the previous release ignores.
CREATE TABLE show_subscriptions (
    id           bigserial PRIMARY KEY,
    user_id      bigint  NOT NULL,
    -- Only set when delivery is to a channel: the server whose departure ends the subscription.
    guild_id     bigint,
    -- NULL means a direct message.
    channel_id   bigint,
    -- The TVmaze show id and its name as it was when the user subscribed.
    show_id      integer NOT NULL,
    show_name    text    NOT NULL CHECK (show_name <> ''),
    -- How long before the episode to remind: 0 to 7 days.
    lead_seconds integer NOT NULL DEFAULT 0 CHECK (lead_seconds BETWEEN 0 AND 604800),
    locale       text    NOT NULL DEFAULT 'en-US',
    job_id       bigint,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, show_id),
    CHECK (channel_id IS NULL OR guild_id IS NOT NULL)
);

CREATE INDEX show_subscriptions_guild ON show_subscriptions (guild_id) WHERE guild_id IS NOT NULL;
