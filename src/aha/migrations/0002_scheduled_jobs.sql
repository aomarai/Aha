-- Durable, replica-safe work queue (#96). Any pod claims due rows with FOR UPDATE SKIP LOCKED.
-- user_id / guild_id are real columns (not buried in the payload) so a person's or a server's
-- jobs can be found and deleted (see aha.datainventory and docs/privacy.md).
CREATE TABLE scheduled_jobs (
    id           bigserial PRIMARY KEY,
    kind         text        NOT NULL CHECK (kind <> ''),
    run_at       timestamptz NOT NULL,
    user_id      bigint,
    guild_id     bigint,
    payload      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    -- Counted when a job is CLAIMED, so a job that crashes the process still reaches dead.
    attempts     integer     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    -- A claim holds a lease until this time; a crashed pod simply stops renewing it.
    locked_until timestamptz,
    last_error   text,
    dead         boolean     NOT NULL DEFAULT false,
    dead_at      timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CHECK (user_id IS NOT NULL OR guild_id IS NOT NULL)
);

-- The poller's query: due, not dead, ordered by run_at.
CREATE INDEX scheduled_jobs_due ON scheduled_jobs (run_at) WHERE NOT dead;
CREATE INDEX scheduled_jobs_user ON scheduled_jobs (user_id) WHERE user_id IS NOT NULL;
CREATE INDEX scheduled_jobs_guild ON scheduled_jobs (guild_id) WHERE guild_id IS NOT NULL;
