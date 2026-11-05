"""Dashboard: a web page showing Clef-flash's answers per camera, live.

Routes:
  /                     the page (static/index.html)
  /api/summary          totals, latest answers, 24 h series and recent rows (Postgres + Redis)
  /api/events           Server-Sent Events: each answer the moment the scorer gets it back
  /api/frame/<etag>/<i> one frame the scorer sent to Clef (kept in Redis for a few hours)
"""

import base64
import json
import logging
import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg
import redis

STATIC = Path(__file__).resolve().parent / "static"
NEURONS_PER_TOKEN = 3455 / 1_000_000
TFL_MEDIA = "https://s3-eu-west-1.amazonaws.com/jamcams.tfl.gov.uk/{}.{}"
FRAME_TIMES = [float(t) for t in os.environ.get("FRAME_TIMES", "1.0,3.5,6.0,8.5").split(",")]
EVENTS = "streetcam:events"
FRAME_PATH = re.compile(r"^/api/frame/([0-9A-Za-z-]+)/(\d+)$")

LATEST_SQL = """
SELECT DISTINCT ON (camera_id) camera_id, camera_name, clip_etag, captured_at, scored_at,
       answers, latency_ms, frame_answers
FROM decisions ORDER BY camera_id, captured_at DESC
"""
SERIES_SQL = """
SELECT camera_id, captured_at, (answers->'congestion'->>'score')::float, latency_ms,
       (SELECT array_agg((f->>'latency_ms')::int) FROM jsonb_array_elements(frame_answers) f
         WHERE f ? 'latency_ms')
FROM decisions WHERE captured_at > now() - interval '24 hours' ORDER BY captured_at
"""
TODAY = "scored_at >= (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
TODAY_SQL = f"""
SELECT count(*), coalesce(sum(input_tokens), 0), coalesce(sum(frames), 0),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms),
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
FROM decisions WHERE {TODAY}
"""
FRAME_LATENCY_SQL = f"""
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY (f->>'latency_ms')::int)
FROM decisions d, jsonb_array_elements(d.frame_answers) f
WHERE d.{TODAY} AND f ? 'latency_ms'
"""
RECENT_SQL = """
SELECT camera_name, captured_at, answers, frame_answers, input_tokens, latency_ms
FROM decisions ORDER BY captured_at DESC, id DESC LIMIT 20
"""

log = logging.getLogger("dashboard")
rds = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)


def iso(ts):
    return ts.isoformat() if isinstance(ts, datetime) else ts


def media_url(camera_id, ext):
    # JamCams_00001.06514 -> .../00001.06514.mp4 (or .jpg)
    return TFL_MEDIA.format(camera_id.removeprefix("JamCams_"), ext)


def live_etag(url):
    """ETag of the clip TfL is serving now, to tell whether it is the one we scored."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=5) as resp:
            return resp.headers.get("ETag", "").strip('"') or None
    except OSError as exc:
        log.warning("HEAD %s failed: %s", url, exc)
        return None


def live_states(camera_ids):
    """The scorer's latest per-clip state for each camera, if Redis still has it."""
    try:
        raw = rds.mget([f"live:{cid}" for cid in camera_ids]) if camera_ids else []
    except redis.RedisError as exc:
        log.warning("redis unavailable: %s", exc)
        return {}
    return {cid: json.loads(v) for cid, v in zip(camera_ids, raw, strict=True) if v}


def summary():
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        latest = conn.execute(LATEST_SQL).fetchall()
        series = conn.execute(SERIES_SQL).fetchall()
        clips, tokens, frames, p50, p95 = conn.execute(TODAY_SQL).fetchone()
        frame_p50 = conn.execute(FRAME_LATENCY_SQL).fetchone()[0]
        recent = conn.execute(RECENT_SQL).fetchall()
        total = conn.execute("SELECT count(*) FROM decisions").fetchone()[0]

    videos = [media_url(row[0], "mp4") for row in latest]
    with ThreadPoolExecutor(max_workers=8) as pool:
        tags = list(pool.map(live_etag, videos))
    live = live_states([row[0] for row in latest])

    cameras = [{
        "id": cid, "name": name, "scored_etag": etag, "captured_at": iso(cap), "scored_at": iso(scored),
        "answers": answers, "latency_ms": lat, "frame_answers": frame_answers,
        "live_etag": tag, "live": live.get(cid), "video": video, "poster": media_url(cid, "jpg"),
    } for (cid, name, etag, cap, scored, answers, lat, frame_answers), video, tag
        in zip(latest, videos, tags, strict=True)]

    points = {}
    for cid, cap, congestion, latency, frame_latencies in series:
        points.setdefault(cid, []).append({"t": iso(cap), "congestion": congestion,
                                           "latency_ms": latency, "frame_latency_ms": frame_latencies or []})

    return {
        "generated_at": iso(datetime.now().astimezone()),
        "frame_times": FRAME_TIMES,
        "today": {"clips": clips, "frames": frames, "tokens": tokens, "neurons": round(tokens * NEURONS_PER_TOKEN),
                  "neuron_limit": int(os.environ.get("DAILY_NEURON_LIMIT", "10000")),
                  "clip_p50_ms": p50, "clip_p95_ms": p95, "frame_p50_ms": frame_p50},
        "total_clips": total,
        "cameras": cameras,
        "series": points,
        "recent": [{"camera": n, "captured_at": iso(c), "answers": a, "frame_answers": fa, "tokens": t,
                    "latency_ms": ms} for n, c, a, fa, t, ms in recent],
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            return self.send(200, b"ok", "text/plain")
        if self.path in ("/", "/index.html"):
            return self.send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/summary":
            try:
                body = json.dumps(summary()).encode()
            except psycopg.Error as exc:
                log.error("database error: %s", exc)
                return self.send(503, json.dumps({"error": "database unavailable"}).encode(), "application/json")
            return self.send(200, body, "application/json")
        if self.path == "/api/events":
            return self.events()
        if match := FRAME_PATH.match(self.path):
            data = rds.get(f"frame:{match[1]}:{match[2]}")
            if data is None:
                return self.send(404, b"frame expired", "text/plain")
            return self.send(200, base64.b64decode(data), "image/jpeg", cache="public, max-age=21600")
        self.send(404, b"not found", "text/plain")

    def events(self):
        """Forward every scorer event to the browser as it happens."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        pubsub = rds.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(EVENTS)
        try:
            while True:
                message = pubsub.get_message(timeout=15)
                line = f"data: {message['data']}\n\n" if message else ": keepalive\n\n"
                self.wfile.write(line.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser tab closed
        finally:
            pubsub.close()

    def send(self, status, body, content_type, cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass  # health checks every few seconds would flood the logs


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("", port), Handler)
    server.daemon_threads = True  # open event streams must not block shutdown
    log.info("dashboard on :%d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
