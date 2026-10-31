"""Dashboard: a small web page showing Clef-flash's latest answers per camera.

Serves static/index.html and /api/summary (JSON read from Postgres).
"""

import json
import logging
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg

STATIC = Path(__file__).resolve().parent / "static"
NEURONS_PER_TOKEN = 3455 / 1_000_000
TFL_MEDIA = "https://s3-eu-west-1.amazonaws.com/jamcams.tfl.gov.uk/{}.{}"
FRAME_TIMES = [float(t) for t in os.environ.get("FRAME_TIMES", "1.0,3.5,6.0,8.5").split(",")]

LATEST_SQL = """
SELECT DISTINCT ON (camera_id) camera_id, camera_name, clip_etag, captured_at, scored_at, answers, latency_ms
FROM decisions ORDER BY camera_id, captured_at DESC
"""
SERIES_SQL = """
SELECT camera_id, captured_at,
       (answers->'congestion'->>'score')::float,
       (answers->'moving'->>'noul')::float,
       (answers->'wet'->>'noul')::float
FROM decisions WHERE captured_at > now() - interval '24 hours' ORDER BY captured_at
"""
TODAY_SQL = """
SELECT count(*), coalesce(sum(input_tokens), 0),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms),
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
FROM decisions WHERE scored_at >= (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
"""
RECENT_SQL = """
SELECT camera_name, captured_at, answers, input_tokens, latency_ms
FROM decisions ORDER BY captured_at DESC, id DESC LIMIT 20
"""

log = logging.getLogger("dashboard")


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


def summary():
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        latest = conn.execute(LATEST_SQL).fetchall()
        series = conn.execute(SERIES_SQL).fetchall()
        clips, tokens, p50, p95 = conn.execute(TODAY_SQL).fetchone()
        recent = conn.execute(RECENT_SQL).fetchall()
        total = conn.execute("SELECT count(*) FROM decisions").fetchone()[0]

    videos = [media_url(row[0], "mp4") for row in latest]
    with ThreadPoolExecutor(max_workers=8) as pool:
        live = list(pool.map(live_etag, videos))

    cameras = [{
        "id": cid, "name": name, "captured_at": iso(cap), "scored_at": iso(scored),
        "answers": answers, "latency_ms": lat, "scored_etag": etag, "live_etag": live_tag,
        "video": video, "poster": media_url(cid, "jpg"),
    } for (cid, name, etag, cap, scored, answers, lat), video, live_tag in zip(latest, videos, live)]

    points = {}
    for cid, cap, congestion, moving, wet in series:
        points.setdefault(cid, []).append({"t": iso(cap), "congestion": congestion, "moving": moving, "wet": wet})

    return {
        "generated_at": iso(datetime.now().astimezone()),
        "today": {"clips": clips, "tokens": tokens, "neurons": round(tokens * NEURONS_PER_TOKEN),
                  "neuron_limit": int(os.environ.get("DAILY_NEURON_LIMIT", "10000")),
                  "latency_p50_ms": p50, "latency_p95_ms": p95},
        "total_clips": total,
        "frame_times": FRAME_TIMES,
        "cameras": cameras,
        "series": points,
        "recent": [{"camera": n, "captured_at": iso(c), "answers": a, "tokens": t, "latency_ms": l}
                   for n, c, a, t, l in recent],
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            return self.send(200, b"ok", "text/plain")
        if self.path == "/api/summary":
            try:
                body = json.dumps(summary()).encode()
            except psycopg.Error as exc:
                log.error("database error: %s", exc)
                return self.send(503, json.dumps({"error": "database unavailable"}).encode(), "application/json")
            return self.send(200, body, "application/json")
        if self.path in ("/", "/index.html"):
            return self.send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        self.send(404, b"not found", "text/plain")

    def send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass  # health checks every few seconds would flood the logs


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    port = int(os.environ.get("PORT", "8080"))
    log.info("dashboard on :%d", port)
    ThreadingHTTPServer(("", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
