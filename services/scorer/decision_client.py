"""Clef-flash client.

Workers AI is the only backend today. A self-hosted backend can be added later
by implementing the same decide() method.
"""

import base64
import time

import requests

CF_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/clef-flash"


class DecisionError(Exception):
    def __init__(self, message, status=None, retryable=True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class WorkersAIClient:
    def __init__(self, account_id, api_token, session=None, timeout=60):
        self.url = CF_URL.format(account=account_id)
        self.headers = {"Authorization": f"Bearer {api_token}"}
        self.session = session or requests.Session()
        self.timeout = timeout

    def decide(self, state, images, questions):
        """Ask Clef-flash the questions about the JPEG images.

        Returns (result, latency_ms), where result holds `answers`, `model` and `usage`.
        """
        body = {
            "model": "clef-flash",
            "state": state,
            "images": [{"content_type": "image/jpeg", "base64": base64.b64encode(img).decode()}
                       for img in images],
            "questions": questions,
        }
        start = time.perf_counter()
        try:
            resp = self.session.post(self.url, json=body, headers=self.headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise DecisionError(f"request failed: {exc}") from exc
        latency_ms = round((time.perf_counter() - start) * 1000)

        if resp.status_code != 200:
            # 429 and 5xx are worth retrying; 4xx means the request itself is wrong.
            retryable = resp.status_code == 429 or resp.status_code >= 500
            raise DecisionError(f"HTTP {resp.status_code}: {resp.text[:300]}", resp.status_code, retryable)

        data = resp.json()
        result = data.get("result", data)
        if "answers" not in result:
            raise DecisionError(f"no answers in response: {str(data)[:300]}", resp.status_code, False)
        return result, latency_ms
