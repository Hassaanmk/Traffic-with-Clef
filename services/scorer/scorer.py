"""Scorer: take clip jobs from Redis, ask Clef-flash about 4 frames, store the answers.

Runs until stopped. KEDA starts and stops scorer pods based on the queue length.
Jobs move from `clips` to `processing` while being worked on, so a crash never
loses one; the grabber puts stale `processing` jobs back on the queue.
"""

import json
import logging
import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import psycopg
import redis
import requests
import yaml
from prometheus_client import Counter, Gauge, Histogram, start_http_server
from psycopg.types.json import Jsonb

from decision_client import DecisionError, WorkersAIClient

QUEUE = "clips"
PROCESSING = "processing"
MAX_ATTEMPTS = 3
# Seconds into the ~9.5 s clip; the dashboard reads the same FRAME_TIMES to mark them.
FRAME_TIMES = [float(t) for t in os.environ.get("FRAME_TIMES", "1.0,3.5,6.0,8.5").split(",")]
NEURONS_PER_TOKEN = 3455 / 1_000_000
QUESTIONS_FILE = Path(__file__).resolve().parent / "questions.yaml"

log = logging.getLogger("scorer")

CLIPS_SCORED = Counter("streetcam_clips_scored_total", "Clips scored", ["camera"])
ERRORS = Counter("streetcam_errors_total", "Failed attempts", ["stage"])
LATENCY = Histogram("streetcam_call_latency_seconds", "Clef-flash call latency",
                    buckets=[0.25, 0.5, 1, 1.5, 2, 3, 5, 10])
TOKENS = Counter("streetcam_input_tokens_total", "Input tokens billed")
NEURONS_TODAY = Gauge("streetcam_neurons_today", "Neurons used today (UTC)")
QUEUE_DEPTH = Gauge("streetcam_queue_depth", "Jobs waiting in the clips list")
CONGESTION = Gauge("streetcam_congestion", "Latest congestion score, 0-4", ["camera"])

INSERT_SQL = """
INSERT INTO decisions (camera_id, camera_name, clip_etag, captured_at, answers, model,
                       questions_version, input_tokens, latency_ms, frames)
VALUES (%(camera_id)s, %(camera_name)s, %(clip_etag)s, %(captured_at)s, %(answers)s, %(model)s,
        %(questions_version)s, %(input_tokens)s, %(latency_ms)s, %(frames)s)
ON CONFLICT (camera_id, clip_etag) DO NOTHING
"""


@dataclass
class Context:
    r: redis.Redis
    db: psycopg.Connection
    client: WorkersAIClient
    session: requests.Session
    questions: dict
    questions_version: int


def extract_frames(clip_bytes, times=FRAME_TIMES):
    """Cut one JPEG per timestamp out of an mp4, all in memory."""
    frames = []
    for t in times:
        result = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-ss", str(t), "-i", "pipe:0",
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "3", "pipe:1"],
            input=clip_bytes, capture_output=True, check=True, timeout=30)
        if result.stdout:
            frames.append(result.stdout)
    if not frames:
        raise ValueError("ffmpeg returned no frames")
    return frames


def build_state(job, captured_at, n_frames):
    return (f"TfL traffic camera '{job['camera_name']}', London, "
            f"{captured_at:%Y-%m-%d %H:%M} UTC. {n_frames} frames from one short clip, "
            "about 2.5 seconds apart, in order. Judge the road shown in the images.")


def neuron_key(now):
    return f"neurons:{now:%Y-%m-%d}"


def neurons_used(r, now):
    return float(r.get(neuron_key(now)) or 0)


def record_usage(r, now, tokens):
    key = neuron_key(now)
    r.incrbyfloat(key, tokens * NEURONS_PER_TOKEN)
    r.expire(key, 2 * 86400)


def seconds_until_utc_midnight(now):
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now).total_seconds()


def score(job, ctx):
    """Download the clip, ask Clef-flash, and return the row to store."""
    resp = ctx.session.get(job["video_url"], timeout=30)
    resp.raise_for_status()
    # The clip may have refreshed since the grabber looked; store what was actually scored.
    etag = resp.headers.get("ETag", job["etag"]).strip('"')
    modified = resp.headers.get("Last-Modified")
    captured_at = parsedate_to_datetime(modified) if modified else datetime.fromisoformat(job["captured_at"])

    frames = extract_frames(resp.content)
    state = build_state(job, captured_at, len(frames))
    result, latency_ms = ctx.client.decide(state, frames, ctx.questions)

    return {
        "camera_id": job["camera_id"],
        "camera_name": job["camera_name"],
        "clip_etag": etag,
        "captured_at": captured_at,
        "answers": Jsonb(result["answers"]),
        "model": result.get("model", "unknown"),
        "questions_version": ctx.questions_version,
        "input_tokens": result.get("usage", {}).get("input_tokens", 0),
        "latency_ms": latency_ms,
        "frames": len(frames),
    }


def handle(raw, ctx):
    job = json.loads(raw)
    stage = "score"
    try:
        row = score(job, ctx)
        stage = "store"
        ctx.db.execute(INSERT_SQL, row)

        now = datetime.now(timezone.utc)
        record_usage(ctx.r, now, row["input_tokens"])
        CLIPS_SCORED.labels(job["camera_id"]).inc()
        TOKENS.inc(row["input_tokens"])
        LATENCY.observe(row["latency_ms"] / 1000)
        congestion = row["answers"].obj.get("congestion", {}).get("score")
        if congestion is not None:
            CONGESTION.labels(job["camera_id"]).set(congestion)
        log.info("scored %s etag=%s congestion=%s tokens=%s latency_ms=%s",
                 job["camera_id"], row["clip_etag"][:12], congestion, row["input_tokens"], row["latency_ms"])
    except Exception as exc:
        ERRORS.labels(stage).inc()
        attempts = job.get("attempts", 0) + 1
        retryable = not (isinstance(exc, DecisionError) and not exc.retryable)
        if retryable and attempts < MAX_ATTEMPTS:
            job["attempts"] = attempts
            ctx.r.lpush(QUEUE, json.dumps(job))
            log.warning("failed %s at %s (attempt %d), requeued: %s", job["camera_id"], stage, attempts, exc)
        else:
            log.error("dropped %s at %s after %d attempts: %s", job["camera_id"], stage, attempts, exc)
    finally:
        ctx.r.lrem(PROCESSING, 1, raw)


def wait_for(name, connect, stopping):
    """Retry `connect` until it succeeds. Pods can start before Redis or Postgres is ready."""
    delay = 1
    while True:
        try:
            return connect()
        except (redis.RedisError, psycopg.OperationalError) as exc:
            log.warning("waiting for %s: %s", name, exc)
            if stopping.wait(delay):
                raise SystemExit(0)
            delay = min(delay * 2, 30)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    daily_limit = float(os.environ.get("DAILY_NEURON_LIMIT", "10000"))
    pause_at = float(os.environ.get("PAUSE_AT", "0.95"))
    exit_when_empty = os.environ.get("EXIT_WHEN_EMPTY") == "1"

    # Kubernetes sends SIGTERM before removing a pod; finish the current job, then exit.
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())

    def connect_db():
        return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)

    def connect_redis():
        r = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
        r.ping()
        return r

    spec = yaml.safe_load(QUESTIONS_FILE.read_text())
    session = requests.Session()
    ctx = Context(
        r=wait_for("redis", connect_redis, stopping),
        db=wait_for("postgres", connect_db, stopping),
        client=WorkersAIClient(os.environ["CF_ACCOUNT_ID"], os.environ["CF_API_TOKEN"], session=session),
        session=session,
        questions=spec["questions"],
        questions_version=spec["version"],
    )
    start_http_server(int(os.environ.get("METRICS_PORT", "9100")))

    log.info("scorer started, questions v%s", ctx.questions_version)
    while not stopping.is_set():
        if ctx.db.closed or ctx.db.broken:
            ctx.db = wait_for("postgres", connect_db, stopping)
        now = datetime.now(timezone.utc)
        used = neurons_used(ctx.r, now)
        NEURONS_TODAY.set(used)
        QUEUE_DEPTH.set(ctx.r.llen(QUEUE))
        if used >= daily_limit * pause_at:
            wait = min(60, seconds_until_utc_midnight(now))
            log.warning("paused: %.0f of %.0f neurons used today", used, daily_limit)
            stopping.wait(wait)
            continue

        raw = ctx.r.blmove(QUEUE, PROCESSING, timeout=5, src="RIGHT", dest="LEFT")
        if raw is None:
            if exit_when_empty:
                break
            continue
        handle(raw, ctx)

    log.info("scorer stopped")
    ctx.db.close()


if __name__ == "__main__":
    main()
