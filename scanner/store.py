"""State that survives between runs: the 8-week history and how productive each planner task type was.

Every file is written atomically and under a lock, so two runs on one machine cannot corrupt or
overwrite each other. A dry run reads state but never writes it."""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from .config import DATA_DIR

if os.name == "nt":
    import msvcrt
else:
    import fcntl

HISTORY_WEEKS = 8
EMPTY_RUNS_BEFORE_SKIP = 3  # a planner task type with this many empty runs in a row is skipped ...
RETRY_AFTER_DAYS = 28  # ... and tried again after this long


@contextmanager
def file_lock(path: Path):
    """Exclusive lock on `path` (a sidecar .lock file), blocking until it is free."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(path.suffix + ".lock"), "a+b") as f:
        if os.name == "nt":
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # retries for about 10 s, then raises OSError
        else:
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    """Writes to a temp file first, so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def update_json(path: Path, default, change) -> None:
    """Read-modify-write under the lock: `change(data)` returns the new content."""
    with file_lock(path):
        write_json(path, change(read_json(path, default)))


# --- history (events reported in the last 8 weeks) ------------------------------
def _history_file() -> Path:
    return DATA_DIR / "history.json"


def load_history(today: date) -> list[dict]:
    cutoff = (today - timedelta(weeks=HISTORY_WEEKS)).isoformat()
    return [h for h in read_json(_history_file(), []) if h["gemeldet_am"] >= cutoff]


def save_history(entries: list[dict], today: date) -> None:
    cutoff = (today - timedelta(weeks=HISTORY_WEEKS)).isoformat()
    update_json(_history_file(), [], lambda old: [h for h in old if h["gemeldet_am"] >= cutoff] + entries)


# --- task yield (planner tasks that never find anything are skipped for a while) ----
def _yield_file() -> Path:
    return DATA_DIR / "task_yield.json"


def skipped_types(today: date, never: set[str]) -> dict[str, str]:
    """Source types whose planner tasks found nothing in the last runs, with the reason shown in the report."""
    skipped = {}
    for gattung, s in read_json(_yield_file(), {}).items():
        if gattung in never or s["leer"] < EMPTY_RUNS_BEFORE_SKIP:
            continue
        if (today - date.fromisoformat(s["letzter_lauf"])).days < RETRY_AFTER_DAYS:
            skipped[gattung] = f"zuletzt {s['leer']} Läufe ohne Fund, nächste Prüfung ab {date.fromisoformat(s['letzter_lauf']) + timedelta(days=RETRY_AFTER_DAYS):%d.%m.%Y}"
    return skipped


def record_yield(findings_per_type: dict[str, int], today: date) -> None:
    """findings_per_type: findings of the planner tasks of this run, by source type."""
    def change(stats: dict) -> dict:
        for gattung, n in findings_per_type.items():
            stats[gattung] = {"leer": 0 if n else stats.get(gattung, {}).get("leer", 0) + 1, "letzter_lauf": today.isoformat()}
        return stats
    update_json(_yield_file(), {}, change)
