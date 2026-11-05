# Traffic with Clef

London TfL JamCam clips scored by Clef-flash (Cloudflare Workers AI) every 5 minutes, on a local kind cluster. Goal: a 24-hour proof-of-concept run and a LinkedIn post. See README.md for the overview.

## Commands

```bash
make up        # kind cluster, KEDA, secrets, images, deploy (from scratch)
make deploy    # rebuild all three images, kind load, kubectl apply, restart scorer
make status    # pods, CronJob, Jobs, ScaledObject in namespace streetcam
make grab      # run the grabber now
make logs      # follow scorer logs
make db        # psql into postgres-0
make dashboard # port-forward the results page to http://localhost:8080
make migrate   # apply db/migrations/*.sql to the running database (make deploy runs it)
make test      # ruff + pytest, no cluster needed (pip install -r requirements-dev.txt first)
make down      # delete the cluster

.venv/bin/python spike/spike.py --find "<name>"            # look up camera ids
.venv/bin/python spike/spike.py --camera <id> --dry-run    # one clip, no API call
```

After any change under `services/`, run `make deploy`; the cluster runs images loaded with `kind load`, not your working tree.

## Layout

- `services/grabber/grabber.py`: runs once per CronJob tick, queues changed clips, exits.
- `services/scorer/scorer.py`: long-running loop; `decision_client.py` wraps the Clef-flash call; `questions.yaml` is the question schema.
- `services/dashboard/`: stdlib HTTP server. `/api/summary` reads Postgres plus `live:<camera>` from Redis; `/api/events` relays the Redis `streetcam:events` pub/sub channel as Server-Sent Events; `/api/frame/<etag>/<i>` serves frames from Redis. `static/index.html` renders it (Chart.js from cdnjs). The video is TfL's current mp4; the page compares its ETag with the scored one.
- `db/schema.sql`: `decisions` table, runs only on Postgres's first start. Changes to an existing database go in `db/migrations/NNN_*.sql`, written to be safe to re-run (`IF NOT EXISTS`); `make deploy` applies them. Update `schema.sql` too, so fresh clusters match.
- `deploy/k8s/`: plain YAML, applied with `kubectl apply -f`. `deploy/keda/` is separate because the ScaledObject type exists only after KEDA is installed.
- `spike/spike.py`: standalone day 1 test script, not used by the cluster.
- `tests/`: pytest unit tests with an in-memory Redis stand-in (`conftest.FakeRedis`); no cluster, network or Cloudflare calls. Run `make test` before committing.

## Things that are easy to get wrong

- **Secrets:** `.env` holds only `CF_ACCOUNT_ID` and `CF_API_TOKEN` and is git-ignored. `make secrets` copies them into the `cloudflare` Secret. The Postgres password is generated once in the `postgres` Secret; never regenerate it while the database volume exists.
- **New clip detection:** use the ETag/Last-Modified headers on the S3 video URL. TfL's API `modified` timestamps are stale by days or weeks.
- **Queue safety:** scorers move jobs `clips` to `processing` with BLMOVE and remove them only after the Postgres insert; the grabber requeues jobs stuck in `processing` for over 10 minutes. Inserts use `ON CONFLICT (camera_id, clip_etag) DO NOTHING`, so retries are safe.
- **Scaling:** the scorer Deployment has no `replicas` field on purpose; KEDA owns it (0 to 3, `listLength: "1"`). Adding `replicas` makes every apply fight KEDA.
- **Images:** pods use `imagePullPolicy: Never` with `:dev` tags. `ErrImageNeverPull` means the image was not loaded into kind.
- **Startup order:** the scorer retries Redis and Postgres until they answer; keep that, pods can start before their dependencies.
- **Redis timeouts:** redis-py 8 closes socket reads after 5 s by default. The scorer's `BLMOVE` blocks for `BLOCK_SECONDS`, so its client sets `socket_timeout` above that; otherwise an empty queue crashes the pod.
- **Per-frame calls:** each clip makes 1 clip call + 1 call per frame, in parallel (`questions` and `frame_questions` in `questions.yaml`). Up to 10 concurrent calls can hit Cloudflare's `429 Capacity temporarily exceeded`; `decision_client` retries 429/5xx twice with backoff, and a frame that still fails is stored as `status: failed` without failing the clip. `WorkersAIClient` keeps one HTTP session per thread.
- **Live state:** the scorer writes `live:<camera>` (24 h) and `frame:<etag>:<i>` (6 h) to Redis and publishes every change on `streetcam:events`. Frames are never written to Postgres.

## Budget

Cloudflare gives 10,000 free neurons per day, reset at 00:00 UTC; Clef-flash costs $0.038 per million input tokens (~3,455 neurons per million). Measured: 102 tokens per 352x288 frame, 808 for the state and 7 questions, ~1,215 per 4-frame clip call, ~440 per single-frame call; ~2,970 per clip in total. With 2 cameras every 5 minutes that is ~59% of the allowance. Check `usage.input_tokens` before adding cameras, frames or questions. The scorer pauses at `PAUSE_AT` (95%) of `DAILY_NEURON_LIMIT`, counted in Redis.

## Clef-flash response shape

`result.answers[id]` is one of:
- `{"type": "noul", "noul": p_yes}`
- `{"type": "choice", "choice": id, "probabilities": {...}, "confidence": c}`
- `{"type": "score", "score": expected_level, "legend": {...}, "probabilities": {...}, "confidence": c}`

`score` is the probability-weighted mean level. `result.model` and `result.usage.input_tokens` are stored with every row. Bump `version` in `questions.yaml` whenever question wording or options change.
