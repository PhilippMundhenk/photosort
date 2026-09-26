"""Append-only decision/review log: <data>/events.jsonl. Losing it costs history, never photos."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import DATA_DIR

EVENTS_PATH = DATA_DIR / "events.jsonl"


def log(kind: str, **fields) -> dict:
    ev = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind, **fields}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with EVENTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def read(limit: int = 500, kind: str | None = None) -> list[dict]:
    if not EVENTS_PATH.exists():
        return []
    rows = []
    with EVENTS_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if kind and ev.get("kind") != kind:
                continue
            rows.append(ev)
    return rows[-limit:][::-1]

