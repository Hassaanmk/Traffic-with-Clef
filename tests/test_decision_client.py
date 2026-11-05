import pytest

import decision_client
from decision_client import DecisionError, WorkersAIClient


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.text = status, body or {}, str(body)

    def json(self):
        return self._body


OK = Resp(200, {"result": {"answers": {"wet": {"type": "noul", "noul": 0.2}},
                           "usage": {"input_tokens": 440}, "model": "clef-flash"}})


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(decision_client.time, "sleep", lambda s: None)
    return WorkersAIClient("acct", "token")


def script(monkeypatch, client, responses):
    calls = iter(responses)
    monkeypatch.setattr(type(client), "session", property(lambda self: self))
    client.post = lambda *a, **k: next(calls)


def test_retries_429_then_succeeds(monkeypatch, client):
    script(monkeypatch, client, [Resp(429), Resp(503), OK])
    result, latency_ms = client.decide("state", [b"jpg"], {})
    assert result["usage"]["input_tokens"] == 440
    assert latency_ms >= 0


def test_gives_up_after_retries(monkeypatch, client):
    script(monkeypatch, client, [Resp(429)] * 3)
    with pytest.raises(DecisionError) as exc:
        client.decide("state", [b"jpg"], {}, retries=2)
    assert exc.value.status == 429 and exc.value.retryable


def test_bad_request_is_not_retried(monkeypatch, client):
    script(monkeypatch, client, [Resp(400), OK])
    with pytest.raises(DecisionError) as exc:
        client.decide("state", [b"jpg"], {})
    assert exc.value.status == 400 and not exc.value.retryable


def test_response_without_answers_is_an_error(monkeypatch, client):
    script(monkeypatch, client, [Resp(200, {"result": {}})])
    with pytest.raises(DecisionError):
        client.decide("state", [b"jpg"], {})


def test_images_are_sent_as_base64_jpeg(monkeypatch, client):
    sent = {}
    monkeypatch.setattr(type(client), "session", property(lambda self: self))
    client.post = lambda url, json, headers, timeout: sent.update(json) or OK
    client.decide("state", [b"\xff\xd8"], {"wet": {}})
    assert sent["images"] == [{"content_type": "image/jpeg", "base64": "/9g="}]
    assert sent["model"] == "clef-flash"
