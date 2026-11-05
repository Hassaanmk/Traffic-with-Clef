"""Shared test setup: import paths for the three services and an in-memory Redis stand-in."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for service in ("grabber", "scorer", "dashboard"):
    sys.path.insert(0, str(ROOT / "services" / service))

# The dashboard connects lazily, but reads REDIS_URL at import time.
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


class FakeRedis:
    """Just the Redis commands the services use, kept in plain dicts and lists."""

    def __init__(self):
        self.kv, self.hashes, self.lists, self.published, self.ttl = {}, {}, {}, [], {}

    # strings
    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, ex=None):
        self.kv[key] = value
        if ex:
            self.ttl[key] = ex

    def mget(self, keys):
        return [self.kv.get(k) for k in keys]

    def incrbyfloat(self, key, amount):
        self.kv[key] = str(float(self.kv.get(key, 0)) + amount)

    def expire(self, key, seconds):
        self.ttl[key] = seconds

    # hashes
    def hget(self, name, key):
        return self.hashes.get(name, {}).get(key)

    def hset(self, name, key, value):
        self.hashes.setdefault(name, {})[key] = value

    # lists
    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    def lrange(self, key, start, end):
        items = self.lists.get(key, [])
        return items[start:] if end == -1 else items[start:end + 1]

    def lrem(self, key, count, value):
        items = self.lists.get(key, [])
        if value in items:
            items.remove(value)
            return 1
        return 0

    def llen(self, key):
        return len(self.lists.get(key, []))

    # pub/sub
    def publish(self, channel, message):
        self.published.append((channel, message))

    def pipeline(self):
        return _Pipeline(self)


class _Pipeline:
    def __init__(self, r):
        self.r, self.ops = r, []

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.ops.append((name, args, kwargs))

    def execute(self):
        return [getattr(self.r, name)(*args, **kwargs) for name, args, kwargs in self.ops]
