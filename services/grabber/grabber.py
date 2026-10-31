"""Grabber: queue TfL JamCam clips that changed since the last run.

Runs once per CronJob tick (every 5 minutes), then exits.
TfL's API timestamps go stale, so a clip counts as new when its ETag on TfL's
storage changes.
"""

import json
import logging
import os
import sys
import time
from email.utils import parsedate_to_datetime

import redis
import requests

TFL_PLACE = "https://api.tfl.gov.uk/Place/{camera_id}"
QUEUE = "clips"
PROCESSING = "processing"
LAST_ETAG = "last_etag"
STALE_JOB_SECONDS = 600  # a job in `processing` this long belongs to a crashed scorer

log = logging.getLogger("grabber")


def camera_info(camera_id, session):
    resp = session.get(TFL_PLACE.format(camera_id=camera_id), timeout=20)
    resp.raise_for_status()
    place = resp.json()
    props = {p["key"]: p["value"] for p in place["additionalProperties"]}
    return {"camera_id": camera_id, "camera_name": place["commonName"],
            "video_url": props["videoUrl"], "available": props.get("available") == "true"}


def clip_version(video_url, session):
    head = session.head(video_url, timeout=20)
    head.raise_for_status()
    etag = head.headers["ETag"].strip('"')
    captured_at = parsedate_to_datetime(head.headers["Last-Modified"]).isoformat()
    return etag, captured_at


def queue_if_new(r, camera, etag, captured_at, now):
    """Push a job unless this camera's clip has the same ETag as last time."""
    if r.hget(LAST_ETAG, camera["camera_id"]) == etag:
        return False
    job = {
        "camera_id": camera["camera_id"],
        "camera_name": camera["camera_name"],
        "video_url": camera["video_url"],
        "etag": etag,
        "captured_at": captured_at,
        "queued_at": now,
        "attempts": 0,
    }
    pipe = r.pipeline()
    pipe.lpush(QUEUE, json.dumps(job))
    pipe.hset(LAST_ETAG, camera["camera_id"], etag)
    pipe.execute()
    return True


def requeue_stale(r, now):
    """Move jobs left in `processing` by a crashed scorer back onto the queue."""
    moved = 0
    for raw in r.lrange(PROCESSING, 0, -1):
        job = json.loads(raw)
        if now - job["queued_at"] > STALE_JOB_SECONDS and r.lrem(PROCESSING, 1, raw):
            job["queued_at"] = now
            job["attempts"] = job.get("attempts", 0) + 1
            r.lpush(QUEUE, json.dumps(job))
            moved += 1
    return moved


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cameras = [c.strip() for c in os.environ["CAMERAS"].split(",") if c.strip()]
    r = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    session = requests.Session()
    now = time.time()

    moved = requeue_stale(r, now)
    if moved:
        log.warning("requeued %d stale jobs", moved)

    queued = failed = 0
    for camera_id in cameras:
        try:
            camera = camera_info(camera_id, session)
            if not camera["available"]:
                log.info("skip %s: marked unavailable", camera_id)
                continue
            etag, captured_at = clip_version(camera["video_url"], session)
            if queue_if_new(r, camera, etag, captured_at, now):
                queued += 1
                log.info("queued %s etag=%s captured_at=%s", camera_id, etag[:12], captured_at)
            else:
                log.info("skip %s: clip unchanged", camera_id)
        except Exception as exc:
            failed += 1
            log.error("failed %s: %s", camera_id, exc)

    log.info("done: %d queued, %d failed, %d cameras", queued, failed, len(cameras))
    if failed == len(cameras):
        sys.exit(1)


if __name__ == "__main__":
    main()
