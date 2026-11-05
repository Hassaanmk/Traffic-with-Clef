-- One row per scored clip. Clips themselves are never stored.
CREATE TABLE IF NOT EXISTS decisions (
    id                bigserial   PRIMARY KEY,
    camera_id         text        NOT NULL,
    camera_name       text        NOT NULL,
    clip_etag         text        NOT NULL,
    captured_at       timestamptz NOT NULL,          -- clip's Last-Modified on TfL storage
    scored_at         timestamptz NOT NULL DEFAULT now(),
    answers           jsonb       NOT NULL,          -- full Clef-flash answers, probabilities included
    model             text        NOT NULL,          -- "model" from the response, to catch silent changes
    questions_version integer     NOT NULL,          -- version from questions.yaml
    input_tokens      integer     NOT NULL,
    latency_ms        integer     NOT NULL,
    frames            smallint    NOT NULL,
    frame_answers     jsonb,                         -- per-frame answers and latencies (questions v2+)
    UNIQUE (camera_id, clip_etag)                     -- a retried job can't insert twice
);

CREATE INDEX IF NOT EXISTS decisions_camera_time ON decisions (camera_id, captured_at);
