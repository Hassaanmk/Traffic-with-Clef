"""Day 1 spike: one TfL JamCam clip in, Clef-flash answers out.

Usage:
  python spike/spike.py --find "hyde park"          # search cameras by name
  python spike/spike.py --camera JamCams_00001.06514 --dry-run
  python spike/spike.py --camera JamCams_00001.06514

Reads CF_ACCOUNT_ID and CF_API_TOKEN from the environment or from .env.
"""

import argparse
import base64
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_FILE = ROOT / "services" / "scorer" / "questions.yaml"
OUT_DIR = ROOT / "spike" / "out"

TFL_CAMERAS = "https://api.tfl.gov.uk/Place/Type/JamCam"
CF_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/clef-flash"

# Seconds into the ~9.5 s clip at which frames are taken.
FRAME_TIMES = [1.0, 3.5, 6.0, 8.5]


def load_env():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


def list_cameras():
    resp = requests.get(TFL_CAMERAS, timeout=30)
    resp.raise_for_status()
    cameras = []
    for cam in resp.json():
        props = {p["key"]: p["value"] for p in cam["additionalProperties"]}
        if "videoUrl" in props and props.get("available") == "true":
            cameras.append({"id": cam["id"], "name": cam["commonName"], "video_url": props["videoUrl"],
                            "lat": cam["lat"], "lon": cam["lon"], "view": props.get("view", "")})
    return cameras


def clip_freshness(url):
    """TfL's API timestamps go stale; the storage headers are the real truth."""
    head = requests.head(url, timeout=20)
    head.raise_for_status()
    modified = datetime.strptime(head.headers["Last-Modified"], "%a, %d %b %Y %H:%M:%S %Z")
    age_min = (datetime.now(timezone.utc) - modified.replace(tzinfo=timezone.utc)).total_seconds() / 60
    return head.headers.get("ETag", "").strip('"'), age_min


def extract_frames(clip_bytes):
    frames = []
    for t in FRAME_TIMES:
        result = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-ss", str(t), "-i", "pipe:0",
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "3", "pipe:1"],
            input=clip_bytes, capture_output=True, check=True)
        frames.append(result.stdout)
    return frames


def frame_size(jpeg):
    """Read width and height from a JPEG's SOF marker."""
    i = 2
    while i < len(jpeg):
        marker, length = jpeg[i + 1], int.from_bytes(jpeg[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(jpeg[i + 7:i + 9], "big"), int.from_bytes(jpeg[i + 5:i + 7], "big")
        i += 2 + length
    raise ValueError("no SOF marker in JPEG")


def image_tokens(width, height):
    # Clef: images over ~1 MP are scaled down, then 1 token per 32x32 patch + 3, capped at 1,024.
    scale = min(1.0, math.sqrt(1_000_000 / (width * height)))
    w, h = width * scale, height * scale
    return min(1024, math.ceil(w / 32) * math.ceil(h / 32) + 3)


def build_request(camera, frames, questions):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return {
        "model": "clef-flash",
        "state": (f"TfL traffic camera '{camera['name']}', London, {stamp}. "
                  f"{len(frames)} frames from one short clip, about 2.5 seconds apart, in order. "
                  "Judge the road shown in the images."),
        "images": [{"content_type": "image/jpeg", "base64": base64.b64encode(f).decode()} for f in frames],
        "questions": questions,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--find", help="search cameras by name")
    parser.add_argument("--camera", help="camera id, e.g. JamCams_00001.06514")
    parser.add_argument("--dry-run", action="store_true", help="build the request but do not call Clef-flash")
    args = parser.parse_args()
    load_env()

    cameras = list_cameras()
    if args.find:
        hits = [c for c in cameras if args.find.lower() in c["name"].lower()]
        for c in hits:
            print(f"{c['id']:<24} {c['name']}  ({c['view']})")
        print(f"{len(hits)} of {len(cameras)} cameras match")
        return
    if not args.camera:
        parser.error("give --find or --camera")

    camera = next((c for c in cameras if c["id"] == args.camera), None)
    if camera is None:
        sys.exit(f"camera {args.camera} not found")

    etag, age = clip_freshness(camera["video_url"])
    print(f"camera   {camera['id']}  {camera['name']}")
    print(f"clip     etag {etag[:12]}…  updated {age:.1f} min ago")

    clip = requests.get(camera["video_url"], timeout=30).content
    frames = extract_frames(clip)
    width, height = frame_size(frames[0])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(frames):
        (OUT_DIR / f"{camera['id']}_{i}.jpg").write_bytes(f)
    per_image = image_tokens(width, height)
    print(f"frames   {len(frames)} × {width}×{height}, ~{per_image} tokens each, saved to spike/out/")

    questions = yaml.safe_load(QUESTIONS_FILE.read_text())["questions"]
    body = build_request(camera, frames, questions)
    print(f"request  {len(json.dumps(body)) / 1024:.0f} KB, {len(questions)} questions, "
          f"~{per_image * len(frames)} image tokens")

    if args.dry_run:
        return
    account, token = os.environ.get("CF_ACCOUNT_ID"), os.environ.get("CF_API_TOKEN")
    if not account or not token:
        sys.exit("set CF_ACCOUNT_ID and CF_API_TOKEN in .env, or use --dry-run")

    start = time.perf_counter()
    resp = requests.post(CF_URL.format(account=account), json=body,
                         headers={"Authorization": f"Bearer {token}"}, timeout=60)
    elapsed_ms = (time.perf_counter() - start) * 1000
    print(f"call     HTTP {resp.status_code} in {elapsed_ms:.0f} ms")
    data = resp.json()
    (OUT_DIR / f"{camera['id']}_response.json").write_text(json.dumps(data, indent=2))
    if not resp.ok:
        sys.exit(json.dumps(data.get("errors", data), indent=2))

    result = data.get("result", data)
    print(f"model    {result.get('model')}")
    print(f"usage    {json.dumps(result.get('usage'))}")
    print("answers")
    for qid, answer in result.get("answers", {}).items():
        print(f"  {qid:<15} {json.dumps(answer)}")


if __name__ == "__main__":
    main()
