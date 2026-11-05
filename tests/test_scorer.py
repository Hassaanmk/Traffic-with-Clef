import json
import shutil
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import scorer
from conftest import FakeRedis
from decision_client import DecisionError

JOB = {"camera_id": "JamCams_00001.06514", "camera_name": "Hyde Park Corner",
       "video_url": "https://example.test/clip.mp4", "etag": "queued-etag",
       "captured_at": "2026-11-05T08:00:00+00:00", "attempts": 0}


class Download:
    status_code = 200
    content = b"mp4"
    headers = {"ETag": '"scored-etag"', "Last-Modified": "Thu, 05 Nov 2026 08:00:00 GMT"}

    def raise_for_status(self):
        pass


class Client:
    """Answers every call; the frame call for frame 2 fails if asked to."""

    def __init__(self, fail_frame=None, fail_clip=None):
        self.fail_frame, self.fail_clip = fail_frame, fail_clip

    def decide(self, state, images, questions):
        if len(images) > 1:
            if self.fail_clip:
                raise self.fail_clip
            return {"answers": {"congestion": {"type": "score", "score": 1.3}},
                    "usage": {"input_tokens": 1215}, "model": "clef-flash"}, 1600
        if self.fail_frame and f"{scorer.FRAME_TIMES[self.fail_frame]:.1f} seconds" in state:
            raise DecisionError("HTTP 429", 429, True)
        return {"answers": {"congestion": {"type": "score", "score": 1.2}},
                "usage": {"input_tokens": 440}}, 900


class DB:
    def __init__(self, fail=False):
        self.rows, self.fail = [], fail

    def execute(self, sql, row):
        if self.fail:
            raise RuntimeError("database gone")
        self.rows.append(row)


def make_ctx(client=None, db=None):
    return SimpleNamespace(r=FakeRedis(), db=db or DB(), client=client or Client(),
                           session=SimpleNamespace(get=lambda url, timeout: Download()),
                           questions={"congestion": {}}, frame_questions={"congestion": {}},
                           questions_version=2)


@pytest.fixture(autouse=True)
def four_fake_frames(monkeypatch):
    monkeypatch.setattr(scorer, "extract_frames", lambda clip: [b"f0", b"f1", b"f2", b"f3"])


def events(ctx):
    return [json.loads(m)["event"] for _, m in ctx.r.published]


def test_score_returns_row_and_streams_every_answer():
    ctx = make_ctx()
    row, live = scorer.score(JOB, ctx)

    assert row["clip_etag"] == "scored-etag"           # what was downloaded, not what was queued
    assert row["input_tokens"] == 1215 + 4 * 440
    assert row["latency_ms"] == 1600 and row["frames"] == 4
    assert [f["status"] for f in live["frames"]] == ["done"] * 4
    ev = events(ctx)
    assert ev[0] == "started" and ev.count("frame") == 4 and ev.count("clip") == 1
    assert all(ctx.r.get(f"frame:scored-etag:{i}") for i in range(4))
    assert ctx.r.ttl["frame:scored-etag:0"] == scorer.FRAME_TTL


def test_one_failed_frame_does_not_fail_the_clip():
    ctx = make_ctx(Client(fail_frame=2))
    row, live = scorer.score(JOB, ctx)
    assert [f["status"] for f in live["frames"]] == ["done", "done", "failed", "done"]
    assert row["input_tokens"] == 1215 + 3 * 440


def test_failed_clip_call_publishes_failed_and_raises():
    ctx = make_ctx(Client(fail_clip=DecisionError("HTTP 503", 503, True)))
    with pytest.raises(DecisionError):
        scorer.score(JOB, ctx)
    assert events(ctx)[-1] == "failed"


def test_handle_stores_row_counts_neurons_and_clears_processing():
    ctx = make_ctx()
    raw = json.dumps(JOB)
    ctx.r.lists[scorer.PROCESSING] = [raw]
    scorer.handle(raw, ctx)

    assert len(ctx.db.rows) == 1
    assert events(ctx)[-1] == "done"
    assert ctx.r.llen(scorer.PROCESSING) == 0 and ctx.r.llen(scorer.QUEUE) == 0
    used = scorer.neurons_used(ctx.r, datetime.now(timezone.utc))
    assert used == pytest.approx((1215 + 4 * 440) * scorer.NEURONS_PER_TOKEN)


def test_handle_requeues_a_retryable_failure():
    ctx = make_ctx(db=DB(fail=True))
    raw = json.dumps(JOB)
    ctx.r.lists[scorer.PROCESSING] = [raw]
    scorer.handle(raw, ctx)

    assert ctx.r.llen(scorer.PROCESSING) == 0
    assert json.loads(ctx.r.lists[scorer.QUEUE][0])["attempts"] == 1


def test_handle_drops_a_bad_request_and_the_last_attempt():
    for client, job in ((Client(fail_clip=DecisionError("HTTP 400", 400, False)), JOB),
                        (Client(fail_clip=DecisionError("HTTP 503", 503, True)),
                         {**JOB, "attempts": scorer.MAX_ATTEMPTS - 1})):
        ctx = make_ctx(client)
        raw = json.dumps(job)
        ctx.r.lists[scorer.PROCESSING] = [raw]
        scorer.handle(raw, ctx)
        assert ctx.r.llen(scorer.QUEUE) == 0 and ctx.r.llen(scorer.PROCESSING) == 0


def test_redis_socket_outlives_the_blocking_wait():
    # redis-py 8 closes reads after 5 s by default; BLMOVE must not trip that.
    assert scorer.SOCKET_TIMEOUT > scorer.BLOCK_SECONDS


def test_seconds_until_utc_midnight():
    now = datetime(2026, 11, 5, 23, 59, 30, tzinfo=timezone.utc)
    assert scorer.seconds_until_utc_midnight(now) == 30


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_extract_frames_from_a_real_clip(monkeypatch, tmp_path):
    monkeypatch.undo()  # use the real extract_frames
    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=352x288:rate=25",
                    "-t", "9.5", "-pix_fmt", "yuv420p", str(clip)], check=True)
    frames = scorer.extract_frames(clip.read_bytes())
    assert len(frames) == 4
    assert all(f.startswith(b"\xff\xd8") for f in frames)  # JPEG magic bytes
