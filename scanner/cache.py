"""Tiny on-disk cache for pages that rarely change (robots.txt, list pages).

Every request we do not send is one that cannot trip a hoster's rate limit. It
also means a run that was cut off half-way picks up where it stopped.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

import os
from pathlib import Path

from .config import DATA_DIR

CACHE_DIR = Path(os.getenv("SCANNER_CACHE_DIR") or DATA_DIR / "cache")


def _path(url: str):
    return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".json")


def get(url: str, max_age_hours: float) -> str | None:
    p = _path(url)
    try:
        entry = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - entry["fetched_at"] > max_age_hours * 3600:
        return None
    return entry["text"]


def put(url: str, text: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _path(url).write_text(json.dumps({"url": url, "fetched_at": time.time(), "text": text}), "utf-8")


# --- request ledger -----------------------------------------------------------
# The shared municipal hoster closes a server to an address that sent it roughly 30
# requests within an hour, whatever the pace. The ledger counts our requests per server
# across runs, so a second run or a test shortly after the first cannot push us over.
_ledger_lock = threading.Lock()


def _ledger() -> dict[str, list[float]]:
    try:
        return json.loads((CACHE_DIR / "requests.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def requests_in_window(server: str, window_seconds: int = 3600) -> int:
    cutoff = time.time() - window_seconds
    return sum(t > cutoff for t in _ledger().get(server, []))


def record_request(server: str, window_seconds: int = 3600) -> None:
    with _ledger_lock:
        cutoff = time.time() - window_seconds
        ledger = {k: [t for t in v if t > cutoff] for k, v in _ledger().items()}
        ledger.setdefault(server, []).append(time.time())
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / "requests.json").write_text(json.dumps(ledger), "utf-8")
