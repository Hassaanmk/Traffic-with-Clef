-- Per-frame answers, added with questions v2. Safe to run more than once.
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS frame_answers jsonb;
