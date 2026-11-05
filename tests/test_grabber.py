import json

import grabber
from conftest import FakeRedis

CAMERA = {"camera_id": "JamCams_00001.06514", "camera_name": "Hyde Park Corner",
          "video_url": "https://example.test/00001.06514.mp4"}


def test_new_etag_is_queued_once():
    r = FakeRedis()
    assert grabber.queue_if_new(r, CAMERA, "etag-1", "2026-11-05T08:00:00+00:00", 1000.0)
    assert not grabber.queue_if_new(r, CAMERA, "etag-1", "2026-11-05T08:00:00+00:00", 1300.0)

    assert r.llen(grabber.QUEUE) == 1
    job = json.loads(r.lists[grabber.QUEUE][0])
    assert job["etag"] == "etag-1" and job["attempts"] == 0
    assert r.hget(grabber.LAST_ETAG, CAMERA["camera_id"]) == "etag-1"


def test_changed_etag_is_queued_again():
    r = FakeRedis()
    grabber.queue_if_new(r, CAMERA, "etag-1", "t", 1000.0)
    assert grabber.queue_if_new(r, CAMERA, "etag-2", "t", 1300.0)
    assert r.llen(grabber.QUEUE) == 2


def test_only_stale_jobs_are_requeued():
    r = FakeRedis()
    now = 10_000.0
    stale = json.dumps({"camera_id": "a", "queued_at": now - grabber.STALE_JOB_SECONDS - 1, "attempts": 0})
    fresh = json.dumps({"camera_id": "b", "queued_at": now - 60, "attempts": 0})
    r.lists[grabber.PROCESSING] = [stale, fresh]

    assert grabber.requeue_stale(r, now) == 1
    assert r.lists[grabber.PROCESSING] == [fresh]
    moved = json.loads(r.lists[grabber.QUEUE][0])
    assert moved["camera_id"] == "a" and moved["attempts"] == 1 and moved["queued_at"] == now
