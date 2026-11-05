import json
from pathlib import Path

import yaml

import app
from conftest import FakeRedis

ROOT = Path(__file__).resolve().parent.parent


def test_media_url():
    assert app.media_url("JamCams_00001.06514", "mp4") == \
        "https://s3-eu-west-1.amazonaws.com/jamcams.tfl.gov.uk/00001.06514.mp4"


def test_frame_path_accepts_etags_and_rejects_traversal():
    assert app.FRAME_PATH.match("/api/frame/7f3a-91c2/3").groups() == ("7f3a-91c2", "3")
    assert app.FRAME_PATH.match("/api/frame/../secret/1") is None
    assert app.FRAME_PATH.match("/api/frame/abc/x") is None


def test_live_states_skips_cameras_without_state(monkeypatch):
    r = FakeRedis()
    r.set("live:a", json.dumps({"state": "done"}))
    monkeypatch.setattr(app, "rds", r)
    assert app.live_states(["a", "b"]) == {"a": {"state": "done"}}
    assert app.live_states([]) == {}


def test_questions_schema():
    spec = yaml.safe_load((ROOT / "services/scorer/questions.yaml").read_text())
    assert isinstance(spec["version"], int)
    for group in ("questions", "frame_questions"):
        for qid, q in spec[group].items():
            assert q["type"] in {"noul", "choice", "score"}, qid
            assert q.get("instructions"), qid
            if q["type"] in {"choice", "score"}:
                assert q.get("criteria") or q.get("options"), qid
    assert set(spec["frame_questions"]) <= set(spec["questions"])


def test_scorer_deployment_leaves_replicas_to_keda():
    docs = [d for d in yaml.safe_load_all((ROOT / "deploy/k8s/scorer.yaml").read_text()) if d]
    deployment = next(d for d in docs if d["kind"] == "Deployment")
    assert "replicas" not in deployment["spec"]


def test_every_manifest_parses_and_uses_loaded_images():
    for path in sorted((ROOT / "deploy").rglob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if not doc:
                continue
            assert doc.get("kind") and doc.get("apiVersion"), path
            text = json.dumps(doc)
            if "streetcam/" in text:
                assert '"imagePullPolicy": "Never"' in text, path


def test_frame_times_match_between_config_and_code():
    config = next(d for d in yaml.safe_load_all((ROOT / "deploy/k8s/config.yaml").read_text())
                  if d and d["kind"] == "ConfigMap")
    assert [float(t) for t in config["data"]["FRAME_TIMES"].split(",")] == app.FRAME_TIMES
