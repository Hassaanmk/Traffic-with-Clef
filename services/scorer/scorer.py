"""Scorer: take clip jobs from Redis, ask Clef-flash about 4 frames, store the answers.

Runs until stopped. KEDA starts and stops scorer pods based on the queue length.
Jobs move from `clips` to `processing` while being worked on, so a crash never
loses one; the grabber puts stale `processing` jobs back on the queue.

Each clip gets one call with all frames together plus one short call per frame,
all in parallel. Every answer is published to Redis the moment it returns, so the
dashboard can show the clip filling in live. Frames are kept in Redis for a few
hours for the dashboard and never written to Postgres.
"""

import base64
import json
import logging
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
# BLMOVE waits this long for a job. redis-py 8 closes reads after 5 s by default,
# so the socket timeout must be longer than the wait or an empty queue crashes the pod.
BLOCK_SECONDS = 5
SOCKET_TIMEOUT = BLOCK_SECONDS + 10
MAX_ATTEMPTS = 3
# Seconds into the ~9.5 s clip; the dashboard reads the same FRAME_TIMES to mark them.
FRAME_TIMES = [float(t) for t in os.environ.get("FRAME_TIMES", "1.0,3.5,6.0,8.5").split(",")]
NEURONS_PER_TOKEN = 3455 / 1_000_000
EVENTS = "streetcam:events"   # Redis pub/sub channel the dashboard listens on
FRAME_TTL = 6 * 3600           # seconds the dashboard can still show a clip's frames
LIVE_TTL = 24 * 3600
QUESTIONS_FILE = Path(__file__).resolve().parent / "questions.yaml"

log = logging.getLogger("scorer")

CLIPS_SCORED = Counter("streetcam_clips_scored_total", "Clips scored", ["camera"])
ERRORS = Counter("streetcam_errors_total", "Failed attempts", ["stage"])
LATENCY = Histogram("streetcam_call_latency_seconds", "Clef-flash call latency", ["kind"],
                    buckets=[0.25, 0.5, 1, 1.5, 2, 3, 5, 10])
TOKENS = Counter("streetcam_input_tokens_total", "Input tokens billed")
NEURONS_TODAY = Gauge("streetcam_neurons_today", "Neurons used today (UTC)")
QUEUE_DEPTH = Gauge("streetcam_queue_depth", "Jobs waiting in the clips list")
CONGESTION = Gauge("streetcam_congestion", "Latest congestion score, 0-4", ["camera"])

INSERT_SQL = """
INSERT INTO decisions (camera_id, camera_name, clip_etag, captured_at, answers, model,
                       questions_version, input_tokens, latency_ms, frames, frame_answers)
VALUES (%(camera_id)s, %(camera_name)s, %(clip_etag)s, %(captured_at)s, %(answers)s, %(model)s,
        %(questions_version)s, %(input_tokens)s, %(latency_ms)s, %(frames)s, %(frame_answers)s)
ON CONFLICT (camera_id, clip_etag) DO NOTHING
"""


@dataclass
class Context:
    r: redis.Redis
    db: psycopg.Connection
    client: WorkersAIClient
    session: requests.Session
    questions: dict
    frame_questions: dict
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


def frame_state(job, captured_at, t):
    return (f"TfL traffic camera '{job['camera_name']}', London, "
            f"{captured_at:%Y-%m-%d %H:%M} UTC. One frame, {t:.1f} seconds into a short clip. "
            "Judge the road shown in the image.")


def publish(ctx, live, event):
    """Store the camera's current clip state for new viewers and push it to live ones."""
    payload = json.dumps({**live, "event": event})
    ctx.r.set(f"live:{live['camera_id']}", payload, ex=LIVE_TTL)
    ctx.r.publish(EVENTS, payload)


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
    """Download the clip, ask Clef-flash about it, and return (row to store, live state)."""
    resp = ctx.session.get(job["video_url"], timeout=30)
    resp.raise_for_status()
    # The clip may have refreshed since the grabber looked; store what was actually scored.
    etag = resp.headers.get("ETag", job["etag"]).strip('"')
    modified = resp.headers.get("Last-Modified")
    captured_at = parsedate_to_datetime(modified) if modified else datetime.fromisoformat(job["captured_at"])

    frames = extract_frames(resp.content)
    for i, img in enumerate(frames):
        ctx.r.set(f"frame:{etag}:{i}", base64.b64encode(img).decode(), ex=FRAME_TTL)

    live = {
        "camera_id": job["camera_id"], "camera_name": job["camera_name"], "etag": etag,
        "captured_at": captured_at.isoformat(), "started_at": datetime.now(timezone.utc).isoformat(),
        "state": "scoring", "total_ms": None,
        "frames": [{"i": i, "t": t, "status": "pending"} for i, t in enumerate(FRAME_TIMES[:len(frames)])],
        "clip": {"status": "pending"},
    }
    publish(ctx, live, "started")

    start = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=len(frames) + 1) as pool:
            clip_state = build_state(job, captured_at, len(frames))
            clip_call = pool.submit(ctx.client.decide, clip_state, frames, ctx.questions)
            frame_calls = {pool.submit(ctx.client.decide, frame_state(job, captured_at, f["t"]), [frames[f["i"]]],
                                       ctx.frame_questions): f["i"] for f in live["frames"]}
            for call in as_completed([clip_call, *frame_calls]):
                if call is clip_call:
                    result, latency_ms = call.result()  # a failed clip call retries the whole job
                    live["clip"] = {"status": "done", "answers": result["answers"], "latency_ms": latency_ms,
                                    "input_tokens": result.get("usage", {}).get("input_tokens", 0),
                                    "model": result.get("model", "unknown")}
                    LATENCY.labels("clip").observe(latency_ms / 1000)
                    publish(ctx, live, "clip")
                    continue
                frame = live["frames"][frame_calls[call]]
                try:
                    result, latency_ms = call.result()
                except DecisionError as exc:
                    # One frame failing doesn't sink the clip; its answer is just missing.
                    ERRORS.labels("frame").inc()
                    frame.update(status="failed", error=str(exc)[:200])
                else:
                    frame.update(status="done", answers=result["answers"], latency_ms=latency_ms,
                                 input_tokens=result.get("usage", {}).get("input_tokens", 0))
                    LATENCY.labels("frame").observe(latency_ms / 1000)
                publish(ctx, live, "frame")
    except Exception:
        live["state"] = "failed"
        publish(ctx, live, "failed")
        raise
    live["total_ms"] = round((time.perf_counter() - start) * 1000)

    clip = live["clip"]
    return {
        "camera_id": job["camera_id"],
        "camera_name": job["camera_name"],
        "clip_etag": etag,
        "captured_at": captured_at,
        "answers": Jsonb(clip["answers"]),
        "model": clip["model"],
        "questions_version": ctx.questions_version,
        "input_tokens": clip["input_tokens"] + sum(f.get("input_tokens", 0) for f in live["frames"]),
        "latency_ms": clip["latency_ms"],
        "frames": len(frames),
        "frame_answers": Jsonb(live["frames"]),
    }, live


def handle(raw, ctx):
    job = json.loads(raw)
    stage = "score"
    try:
        row, live = score(job, ctx)
        stage = "store"
        ctx.db.execute(INSERT_SQL, row)
        live["state"] = "done"
        publish(ctx, live, "done")

        now = datetime.now(timezone.utc)
        record_usage(ctx.r, now, row["input_tokens"])
        CLIPS_SCORED.labels(job["camera_id"]).inc()
        TOKENS.inc(row["input_tokens"])
        congestion = row["answers"].obj.get("congestion", {}).get("score")
        if congestion is not None:
            CONGESTION.labels(job["camera_id"]).set(congestion)
        log.info("scored %s etag=%s congestion=%s tokens=%s clip_ms=%s total_ms=%s",
                 job["camera_id"], row["clip_etag"][:12], congestion, row["input_tokens"],
                 row["latency_ms"], live["total_ms"])
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
                raise SystemExit(0) from None
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
        r = redis.from_url(os.environ["REDIS_URL"], decode_responses=True, socket_timeout=SOCKET_TIMEOUT)
        r.ping()
        return r

    spec = yaml.safe_load(QUESTIONS_FILE.read_text())
    ctx = Context(
        r=wait_for("redis", connect_redis, stopping),
        db=wait_for("postgres", connect_db, stopping),
        client=WorkersAIClient(os.environ["CF_ACCOUNT_ID"], os.environ["CF_API_TOKEN"]),
        session=requests.Session(),
        questions=spec["questions"],
        frame_questions=spec["frame_questions"],
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

        raw = ctx.r.blmove(QUEUE, PROCESSING, timeout=BLOCK_SECONDS, src="RIGHT", dest="LEFT")
        if raw is None:
            if exit_when_empty:
                break
            continue
        handle(raw, ctx)

    log.info("scorer stopped")
    ctx.db.close()


if __name__ == "__main__":
    main()
