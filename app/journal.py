"""The transaction journal: one line per file operation, grouped into batches (one batch per user
or worker action), and the way back. Any batch, or one file of it, can be reverted as far as the
files still are where the batch left them; what cannot be reverted is reported, never guessed.

Manifests remain the per-folder view; the journal is the source of truth for "what happened to
this file" and for revert. Losing it costs history, never photos. Nothing is ever pruned.
"""
from __future__ import annotations

import json
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA_DIR, Config

JOURNAL_PATH = DATA_DIR / "journal.jsonl"
_lock = threading.Lock()
_local = threading.local()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append(entry: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock, JOURNAL_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


@contextmanager
def batch(action: str, **meta):
    """One action = one batch; file operations inside carry its id. A batch opened inside another
    joins the outer one (an undo that moves files is one batch, not one per file)."""
    outer = getattr(_local, "batch", None)
    if outer:
        yield outer
        return
    bid = uuid.uuid4().hex[:12]
    _local.batch = bid
    _append({"ts": _now(), "batch": bid, "op": "begin", "action": action, **meta})
    try:
        yield bid
    finally:
        _append({"ts": _now(), "batch": bid, "op": "end"})
        _local.batch = None


def record(op: str, src: Path | str, dst: Path | str | None = None, **extra) -> None:
    """A file operation: move, copy, delete or rename_dir. Outside any action it becomes a batch
    of its own, so nothing that touches a file is ever missing from the journal."""
    entry = {"ts": _now(), "op": op, "src": str(src), "dst": str(dst) if dst is not None else None, **extra}
    bid = getattr(_local, "batch", None)
    if bid:
        _append({**entry, "batch": bid})
        return
    bid = uuid.uuid4().hex[:12]
    _append({"ts": entry["ts"], "batch": bid, "op": "begin", "action": extra.get("action", "file")})
    _append({**entry, "batch": bid})
    _append({"ts": entry["ts"], "batch": bid, "op": "end"})


def read_all() -> list[dict]:
    if not JOURNAL_PATH.exists():
        return []
    out = []
    with JOURNAL_PATH.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def batches(limit: int = 200, entries: list[dict] | None = None) -> list[dict]:
    """The batches, newest first: id, action, time, meta, ops, whether a later batch reverted it."""
    entries = read_all() if entries is None else entries
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for e in entries:
        bid = e.get("batch")
        if not bid:
            continue
        b = by_id.get(bid)
        if b is None:
            b = by_id[bid] = {"batch": bid, "action": "file", "ts": e["ts"], "ops": [], "meta": {}, "reverted_by": None}
            order.append(bid)
        if e["op"] == "begin":
            b["action"] = e.get("action", "file")
            b["ts"] = e["ts"]
            b["meta"] = {k: v for k, v in e.items() if k not in ("ts", "batch", "op", "action")}
        elif e["op"] != "end":
            b["ops"].append(e)
    for b in by_id.values():
        if b["action"] == "revert" and b["meta"].get("of") in by_id and not b["meta"].get("only"):
            by_id[b["meta"]["of"]]["reverted_by"] = b["batch"]
        b["n"] = len(b["ops"])
    return [by_id[bid] for bid in reversed(order)][:limit]


def get(batch_id: str) -> dict | None:
    return next((b for b in batches(limit=10 ** 9) if b["batch"] == batch_id), None)


def trace(name: str) -> list[dict]:
    """Every operation that touched a file of that name (or path), newest first, each with the
    action it belonged to; the first entry's `now` is where the file is at the moment."""
    needle = name.strip()
    if not needle:
        return []
    out = []
    for b in batches(limit=10 ** 9):
        for e in b["ops"]:
            if needle in (e.get("src") or "") or needle in (e.get("dst") or ""):
                out.append({**e, "action": b["action"], "meta": b["meta"]})
    for e in out:
        for cand in (e.get("dst"), e.get("src")):
            if cand and Path(cand).exists():
                e["now"] = cand
                break
        else:
            e["now"] = None
    return out


def revert(cfg: Config, batch_id: str, only: str | None = None) -> dict:
    """Undo a batch: its operations in reverse order, each only if the file still is where the
    batch left it (a moved-on file is reported, never guessed). `only` restricts it to one file.
    Runs as a batch of its own, so a revert can be reverted."""
    from . import mover  # local: mover imports this module

    b = get(batch_id)
    if b is None:
        raise KeyError(batch_id)
    mover._guard(cfg)
    done: list[dict] = []
    skipped: list[dict] = []
    with batch("revert", of=batch_id, reverted_action=b["action"], **({"only": only} if only else {}),
               **{k: v for k, v in b["meta"].items() if k in ("proposal", "name", "kind", "folder")}) as bid:
        for op in reversed(b["ops"]):
            if only and only not in (op.get("src"), op.get("dst")):
                continue
            kind = op["op"]
            try:
                if kind == "move":
                    src, dst = Path(op["src"]), Path(op["dst"])
                    if not dst.exists():
                        skipped.append({**op, "why": "the file is not there any more (moved on or deleted)"})
                        continue
                    back = mover._transfer(cfg, dst, src.parent, name=src.name)
                    mover.manifest_forget(cfg, dst)
                    done.append({**op, "back": str(back), "renamed": back.name != src.name})
                elif kind == "copy":
                    dst = Path(op["dst"])
                    if not dst.exists():
                        skipped.append({**op, "why": "the copy is not there any more"})
                        continue
                    mover._delete_with_sidecars(cfg, dst)
                    mover._mark_source(cfg, Path(op["src"]), None, None)
                    mover.manifest_forget(cfg, dst)
                    done.append(op)
                elif kind == "rename_dir":
                    src, dst = Path(op["src"]), Path(op["dst"])
                    if not dst.is_dir():
                        skipped.append({**op, "why": "the folder is not there any more"})
                        continue
                    if src.exists():
                        skipped.append({**op, "why": "something else is at the old place now"})
                        continue
                    mover.relocate(cfg, dst, src)
                    done.append(op)
                else:                                                   # a deletion cannot be undone
                    skipped.append({**op, "why": "a deleted copy cannot be restored"})
            except OSError as e:
                skipped.append({**op, "why": f"{type(e).__name__}: {e}"})
        _bookkeeping(cfg, b, done, skipped, only, mover)
    return {"batch": bid, "reverted": len(done), "skipped": skipped, "done": done}


def _bookkeeping(cfg: Config, b: dict, done: list[dict], skipped: list[dict], only: str | None, mover) -> None:
    """After the files: the proposal's status and the folder's manifest follow."""
    from . import cluster
    pid = b["meta"].get("proposal")
    if b["action"] == "apply" and pid and not only and done and not skipped:
        with cluster.proposals_lock:                                    # everything back: like an undo
            props = cluster.load_proposals()
            if pid in props:
                props[pid]["status"] = "rejected"
                cluster.save_proposals(props)
    if b["action"] in ("undo", "revert") and done:
        # the files are back in their cluster folder (a reverted undo, or a reverted revert of an
        # apply): give the folder its manifest again, from what the reverted batch moved out
        folder = Path(b["meta"].get("folder", ""))
        restored = [{"src": op["dst"], "dst": op["src"], "conf": 1.0, "zone": "?", "media": "photo"}
                    for op in done if op["op"] == "move" and Path(op["src"]).is_relative_to(cfg.root)]
        if str(folder) != "." and restored:
            m = mover.read_manifest(folder) or {"name": b["meta"].get("name") or folder.name,
                                                "kind": b["meta"].get("kind") or "trip", "proposal_id": pid,
                                                "decision": {"by": "user", "conf": 1.0}, "start": "", "end": "",
                                                "mode": "move", "reviewed": True, "label": None, "photos": []}
            have = {p["dst"] for p in m.get("photos", [])}
            m["photos"] = m.get("photos", []) + [r for r in restored if r["dst"] not in have]
            m["applied"] = _now()
            mover.write_manifest(folder, m)
        if pid and not only and not skipped:
            with cluster.proposals_lock:
                props = cluster.load_proposals()
                if pid in props:
                    props[pid]["status"] = "applied"
                    cluster.save_proposals(props)
