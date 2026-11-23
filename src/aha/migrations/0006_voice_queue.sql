-- Voice queue (#171, ADR 0002): the tracks waiting to play in each guild, so a queue survives a
-- bot restart or deploy. The lowest id of a guild is the track that is playing now; finishing a
-- track deletes its row. Expand only: a new table, which the previous release ignores.
CREATE TABLE voice_queue (
    id           bigserial PRIMARY KEY,
    guild_id     bigint  NOT NULL,
    -- A YouTube video id (checked here as well as in the application).
    video_id     text    NOT NULL CHECK (video_id ~ '^[A-Za-z0-9_-]{11}$'),
    title        text    NOT NULL,
    length_ms    integer NOT NULL CHECK (length_ms >= 0),
    requester_id bigint  NOT NULL,
    added_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX voice_queue_guild ON voice_queue (guild_id, id);
CREATE INDEX voice_queue_requester ON voice_queue (requester_id);
