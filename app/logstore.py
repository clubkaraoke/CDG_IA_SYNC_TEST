from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_FILE = Path(os.getenv("CDG_IA_LOG_FILE", "/runtime/events.jsonl"))
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def write_event(
    event: str,
    *,
    level: str = "info",
    request_id: str | None = None,
    job_hash: str | None = None,
    filename: str | None = None,
    message: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ts": _now(),
        "level": level,
        "event": event,
    }
    if request_id:
        row["request_id"] = request_id
    if job_hash:
        row["job_hash"] = job_hash
    if filename:
        row["filename"] = filename
    if message:
        row["message"] = message
    if details:
        row["details"] = details

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    with _LOCK:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(encoded + "\n")
    return row


def read_events(limit: int = 200) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 1000))
    try:
        with _LOCK:
            lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []

    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(out))
