"""Moving files. By default everything is a rename() on the same mount (no copy); with
cfg.copy_instead_of_move the photo is copied and the original stays in the inbox, its sidecar
recording where the copy went (copied_to) so it is never proposed again. Every transfer is
recorded in the target folder's manifest.json so it can be undone and later serves as the
eval set."""
from __future__ import annotations

import contextlib
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from . import cluster, events, ingest
from .config import Config, inbox_dirs

MANIFEST = "manifest.json"


class DryRun(RuntimeError):
    """Raised by every function that would move, copy or delete a photo while dry-run is on.
    Dry-run means nothing moves, period: not on approve, not on auto-apply, not on undo. Every
    path that touches a file goes through _transfer/_delete_with_sidecars, which check it, so
    no caller can forget the check."""


class FolderInUse(OSError):
    """A folder could not be renamed because something holds a file in it (Windows only)."""


def _current(p: Path) -> dict:
    """What the worker is on right now, for the progress line (a 100 MB video over a slow mount
    takes a while and must not look stuck)."""
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    return {"file": p.name, "bytes": size, "since": time.time()}


def _guard(cfg: Config) -> None:
    if cfg.dry_run:
        raise DryRun("dry-run is on: nothing is moved, copied or deleted (switch it off in Settings)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _mode(cfg: Config) -> str:
    return "copy" if cfg.copy_instead_of_move else "move"


def _unique(dst: Path) -> Path:
    if not dst.exists():
        return dst
    stem, suf, i = dst.stem, dst.suffix, 1
    while True:
        cand = dst.with_name(f"{stem}_{i}{suf}")
        if not cand.exists():
            return cand
        i += 1


def _transfer(cfg: Config, src: Path, dst_dir: Path, copy: bool = False, name: str | None = None) -> Path:
    """Move (or copy) a photo, its record and its .xmp into dst_dir; returns the new photo path.
    `name` restores the original file name on the way back (a clash on the way in may have
    renamed it to IMG_0001_1.jpg)."""
    _guard(cfg)
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = _unique(dst_dir / (name or src.name))
    size = src.stat().st_size
    op = shutil.copy2 if copy else shutil.move      # move = rename on same fs; copy+delete across mounts
    op(str(src), str(dst))
    # a move across mounts is a copy followed by a delete; on a read-only inbox the delete can
    # fail silently (the share says yes and keeps the file) and the photo would then exist twice
    # and be indexed again as new. Verify, and undo the copy rather than leave a duplicate.
    if not dst.exists() or dst.stat().st_size != size:
        raise OSError(f"{dst} is missing or incomplete after the transfer")
    if not copy and src.exists():
        dst.unlink(missing_ok=True)
        raise PermissionError(f"{src} is still there after the move (inbox mounted read-only?); "
                              f"the copy in {dst_dir} was removed again")
    ingest.move_sidecar(src, dst, cfg, keep_source=copy)
    xmp = src.with_suffix(".xmp")
    if xmp.exists():
        op(str(xmp), str(dst_dir / (dst.stem + ".xmp")))
    return dst


def _settle(cfg: Config, dst: Path, update: dict | None = None) -> None:
    """The record of a photo that just arrived in the sorted tree: updated, or dropped when
    cfg.sidecar_cleanup says the manifest is enough."""
    if cfg.sidecar_cleanup == "after_move":
        ingest.delete_sidecar(dst, cfg)
        return
    rec = ingest.read_sidecar(dst, cfg) or {}
    rec.pop("copied_to", None)
    rec.update(update or {})
    ingest.write_sidecar(dst, rec, cfg)


def _delete_with_sidecars(cfg: Config, photo: Path) -> None:
    _guard(cfg)
    ingest.delete_sidecar(photo, cfg)
    photo.with_suffix(".xmp").unlink(missing_ok=True)
    photo.unlink(missing_ok=True)


def _mark_source(cfg: Config, src: Path, copied_to: Path | None, cluster_name: str | None) -> None:
    """In copy mode the original's record says where its copy lives (None clears the mark)."""
    rec = ingest.read_sidecar(src, cfg)
    if rec is None:
        return
    if copied_to is None:
        rec.pop("copied_to", None)
    else:
        rec["copied_to"] = str(copied_to)
    rec["cluster"] = cluster_name
    ingest.write_sidecar(src, rec, cfg)


def read_manifest(folder: Path) -> dict | None:
    mp = folder / MANIFEST
    if not mp.exists():
        return None
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_manifest(folder: Path, data: dict) -> None:
    tmp = folder / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(folder / MANIFEST)
    invalidate_clusters()


def target_folder(cfg: Config, pr: dict) -> Path:
    """Trips and day outs under the root; a home burst under _unnamed/ until it has a name, which
    it has as soon as the user renamed the proposal in review."""
    root = Path(cfg.root)
    if pr["kind"] == "home" and not pr.get("name_edited") and not pr.get("manual"):
        return root / cfg.unnamed_dir / pr["name"]
    return root / pr["name"]


def apply(cfg: Config, pr: dict, reviewed: bool = False, progress=None) -> dict:
    """Move (or copy) a proposal's photos into its folder.

    reviewed=True means a human approved the proposal in the UI: everything goes straight into
    the folder. Unreviewed (auto-applied) proposals put their uncertain photos into
    <folder>/<review_dir> so they can be checked later. progress(done, total) is called after
    every file (the UI shows it; moving hundreds of files over a share takes a while)."""
    _guard(cfg)
    folder = target_folder(cfg, pr)
    copy = cfg.copy_instead_of_move
    excluded = set(pr.get("excluded", []))
    moved: list[dict] = []
    xmp_jobs: list[tuple[Path, list[str]]] = []
    total = len(pr["photos"])
    # an earlier manifest in the folder (the same trip applied before: photos that synced late; or
    # this very apply, cut short by a crash or a failure and now continued) is extended, never replaced,
    # so undo always knows every file that went in
    earlier = read_manifest(folder) or {}

    def manifest(partial: bool) -> dict:
        m = {"name": pr["name"], "kind": pr["kind"], "start": pr["start"], "end": pr["end"],
             "proposal_id": pr["id"], "decision": pr["decision"], "applied": _now(), "mode": _mode(cfg),
             "reviewed": reviewed, "label": earlier.get("label"), "photos": earlier.get("photos", []) + moved,
             "corrections": earlier.get("corrections", [])}
        if earlier.get("photos"):
            m["start"] = min(m["start"], earlier.get("start") or m["start"])
            m["end"] = max(m["end"], earlier.get("end") or m["end"])
        if partial:
            m["partial"] = True
        return m

    recorded = {e["src"] for e in earlier.get("photos", [])}
    for i, p in enumerate(pr["photos"], 1):
        if progress:
            progress(i - 1, total, _current(Path(p["path"])))
        src = Path(p["path"])
        if p["path"] in excluded:
            continue
        dst_dir = folder / p["source"] if (cfg.subfolder_by_source and p.get("source")) else folder
        in_review = bool(p.get("uncertain")) and not reviewed
        if in_review:
            dst_dir = dst_dir / cfg.review_dir
        if not src.exists():
            # gone from the inbox already. If it sits in the folder without a manifest entry, this
            # apply was cut short (a crash between two manifest writes): record it now
            there = dst_dir / src.name
            if p["path"] not in recorded and there.exists():
                moved.append({"src": p["path"], "dst": str(there), "conf": p["conf"], "zone": p["zone"],
                              "media": p.get("media", "photo"), "source": p.get("source"), "inbox": p.get("inbox"),
                              "uncertain": bool(p.get("uncertain")), "in_review": in_review})
            continue
        try:
            dst = _transfer(cfg, src, dst_dir, copy)
        except Exception:
            if moved:                         # what did move stays undoable: a manifest for the part done
                write_manifest(folder, manifest(partial=True))
            raise
        _settle(cfg, dst, {"cluster": pr["name"],
                           "decision": {"by": pr["decision"]["by"], "conf": p["conf"], "kind": pr["kind"]}})
        if copy:
            _mark_source(cfg, src, dst, pr["name"])
        if cfg.write_xmp_sidecar:
            xmp_jobs.append((dst, [f"zone/{p['zone']}", f"cluster/{pr['name']}"]))
        moved.append({"src": p["path"], "dst": str(dst), "conf": p["conf"], "zone": p["zone"],
                      "media": p.get("media", "photo"), "source": p.get("source"), "inbox": p.get("inbox"),
                      "uncertain": bool(p.get("uncertain")), "in_review": in_review})
        if len(moved) % MANIFEST_EVERY == 0:  # a crash (power, a kill) mid-way loses at most this many entries
            write_manifest(folder, manifest(partial=True))
    wanted = [p for p in pr["photos"] if p["path"] not in excluded]
    if wanted and not moved and not earlier.get("photos") and not any(Path(p["path"]).exists() for p in wanted):
        # every single file is unreachable: the inbox is not mounted, not "already moved". Marking the
        # proposal applied here would bury the photos for good (they are never proposed again).
        raise FileNotFoundError(f"none of the {len(wanted)} files is reachable; is the inbox mounted?")
    if xmp_jobs:
        if progress:
            progress(total, total, {"file": f"keywords for {len(xmp_jobs)} files", "bytes": 0, "since": time.time()})
        ingest.write_xmp_keywords_batch(xmp_jobs)               # one exiftool start, not one per file
    if progress:
        progress(total, total, None)
    m = manifest(partial=False)
    write_manifest(folder, m)
    events.log("apply", proposal=pr["id"], cluster_kind=pr["kind"], name=pr["name"], n=len(moved),
               mode=_mode(cfg), reviewed=reviewed, decision_id=pr["decision"].get("id"))
    return m


MANIFEST_EVERY = 25


def everyday_movable(cfg: Config, min_age_days: float, recs: list[dict] | None = None) -> list[dict]:
    """Unclustered inbox photos older than min_age_days: what apply_everyday would move.
    `recs` lets a page that already has the everyday records pass them in."""
    if cfg.everyday_layout == "leave":
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - min_age_days * 86400
    recs = cluster.everyday_records(cfg) if recs is None else recs
    return [r for r in recs if r["_t"].timestamp() <= cutoff]


def apply_everyday(cfg: Config, min_age_days: float, progress=None) -> int:
    """Move (or copy) unclustered inbox photos older than min_age_days into root/YYYY/MM."""
    if cfg.everyday_layout == "leave":
        return 0
    _guard(cfg)
    todo = everyday_movable(cfg, min_age_days)
    n = 0
    for i, r in enumerate(todo):
        if progress:
            progress(i, len(todo), _current(Path(r["path"])))
        sub = cfg.everyday_layout.replace("YYYY", f"{r['_t']:%Y}").replace("MM", f"{r['_t']:%m}")
        dst_dir = Path(cfg.root) / sub
        if cfg.subfolder_by_source and r.get("source"):
            dst_dir = dst_dir / r["source"]
        dst = _transfer(cfg, Path(r["path"]), dst_dir, cfg.copy_instead_of_move)
        _settle(cfg, dst, {"cluster": sub})
        if cfg.copy_instead_of_move:
            _mark_source(cfg, Path(r["path"]), dst, sub)
        n += 1
    if progress:
        progress(len(todo), len(todo), None)
    if n:
        events.log("apply_everyday", n=n, mode=_mode(cfg))
    return n


def undo(cfg: Config, folder: Path) -> int:
    """Move every photo listed in the manifest back to where it came from (copy mode: delete
    the copies and clear the originals' marks)."""
    m = read_manifest(folder)
    if not m:
        return 0
    _guard(cfg)
    copied = m.get("mode") == "copy"
    n = 0
    for p in m["photos"]:
        dst, src = Path(p["dst"]), Path(p["src"])
        if not dst.exists():
            continue
        if copied:
            _delete_with_sidecars(cfg, dst)
            _mark_source(cfg, src, None, None)
        else:
            _transfer(cfg, dst, src.parent, name=src.name)     # back under its own name
        n += 1
    (folder / MANIFEST).unlink(missing_ok=True)
    invalidate_clusters()
    for sub in sorted(folder.rglob("*"), key=lambda x: -len(x.parts)):
        if sub.is_dir():
            with contextlib.suppress(OSError):
                sub.rmdir()
    with contextlib.suppress(OSError):
        folder.rmdir()
    events.log("undo", name=m["name"], n=n, proposal=m.get("proposal_id"), mode=m.get("mode", "move"))
    return n


def rename(cfg: Config, folder: Path, new_name: str) -> Path:
    _guard(cfg)
    new_name = cluster.sanitize(new_name)
    root = Path(cfg.root)
    # a named home burst leaves _unnamed
    dst = root / new_name if folder.parent.name == cfg.unnamed_dir else folder.with_name(new_name)
    dst = _unique(dst)
    attempt = 0
    while True:                                     # Windows: a folder with an open file (a thumbnail being
        try:                                        # generated, a video being streamed) cannot be renamed
            folder.rename(dst)
            break
        except PermissionError as e:
            attempt += 1
            if attempt == 8:
                raise FolderInUse(f"{folder.name}: a file in it is still open (thumbnail or video); try again") from e
            time.sleep(0.25)
    m = read_manifest(dst)
    if m:
        m["label"] = new_name
        old = m["name"]
        m["name"] = new_name
        for p in m["photos"]:
            p["dst"] = str(dst / Path(p["dst"]).relative_to(folder))
        write_manifest(dst, m)
        for p in m["photos"]:
            old_dst = folder / Path(p["dst"]).relative_to(dst)
            ingest.move_sidecar(old_dst, Path(p["dst"]), cfg)        # central records key on the path
            rec = ingest.read_sidecar(Path(p["dst"]), cfg)
            if rec:
                rec["cluster"] = new_name
                ingest.write_sidecar(Path(p["dst"]), rec, cfg)
            if m.get("mode") == "copy":
                _mark_source(cfg, Path(p["src"]), Path(p["dst"]), new_name)
        events.log("label", old=old, new=new_name, proposal=m.get("proposal_id"),
                   decision_id=m.get("decision", {}).get("id"))
    return dst


def move_out(cfg: Config, folder: Path, photo: Path) -> dict | None:
    """User says a photo does not belong: back to its inbox (copy mode: delete the copy, unmark
    the original), recorded as a correction in the manifest so it can be put back. Returns the
    manifest entry (with src = where the photo is now)."""
    m = read_manifest(folder)
    entry = next((p for p in (m or {}).get("photos", []) if p["dst"] == str(photo)), None)
    if not photo.exists() and not (entry and (m or {}).get("mode") == "copy"):
        return None                                       # nothing there (removed by hand): nothing to do
    rec = ingest.read_sidecar(photo, cfg) or {}
    if m and m.get("mode") == "copy" and entry:
        _delete_with_sidecars(cfg, photo)
        _mark_source(cfg, Path(entry["src"]), None, None)
    else:
        back = Path((entry or {}).get("inbox") or rec.get("inbox") or cfg.inboxes[0]["path"])
        now_at = _transfer(cfg, photo, back)
        if entry:
            entry["src"] = str(now_at)                    # where it is now (a name clash may have renamed it)
    if m and entry:
        m["photos"].remove(entry)
        entry["corrected"] = _now()
        m.setdefault("corrections", []).append(entry)
        write_manifest(folder, m)
    events.log("correction", name=folder.name, photo=photo.name,
               decision_id=(m or {}).get("decision", {}).get("id"), note="moved out by user")
    return entry


def put_back(cfg: Config, folder: Path, src: Path) -> dict | None:
    """Undo a correction: the photo returns from its inbox to the cluster folder. Returns the
    restored manifest entry, None if there is no such correction."""
    m = read_manifest(folder)
    if not m:
        return None
    entry = next((c for c in m.get("corrections", []) if c["src"] == str(src)), None)
    if entry is None:
        return None
    if not src.exists():                                  # moved on since (e.g. into the everyday tree): find it
        found = [f for f in Path(cfg.root).rglob(src.name) if f.is_file()]
        found += [f for _, inbox in inbox_dirs(cfg)
                  for f in inbox.rglob(src.name) if f.is_file()] if not found else []
        if len(found) != 1:
            raise FileNotFoundError(f"{src.name}: not found where it was left ({src}); "
                                    f"{len(found)} files of that name under the sorted tree and inboxes")
        src = found[0]
    dst_dir = Path(entry["dst"]).parent
    if m.get("mode") == "copy":
        dst = _transfer(cfg, src, dst_dir, copy=True)
        _mark_source(cfg, src, dst, m["name"])
    else:
        dst = _transfer(cfg, src, dst_dir)
    _settle(cfg, dst, {"cluster": m["name"], "decision": {"by": m["decision"].get("by", "rule"),
                                                          "conf": entry.get("conf", 1.0), "kind": m["kind"]}})
    entry["dst"] = str(dst)
    entry.pop("corrected", None)
    m["corrections"].remove(entry)
    m["photos"].append(entry)
    m["photos"].sort(key=lambda p: Path(p["dst"]).name)
    write_manifest(folder, m)
    events.log("correction_undone", name=folder.name, photo=dst.name, proposal=m.get("proposal_id"))
    return entry


_mount_cache: dict = {"key": None, "at": 0.0, "note": None}


def cross_mount_note(cfg: Config) -> str | None:
    """A sentence for the pages when an inbox and the sorted root are on different mounts:
    rename() cannot cross a mount point (even two bind mounts of the same share), so every
    move becomes a copy through the container and back over the network. 87 files took
    minutes that way; on one mount a move is instant."""
    key = (cfg.root, tuple(str(f) for _, f in inbox_dirs(cfg)))
    if _mount_cache["key"] == key and time.time() - _mount_cache["at"] < 300:
        return _mount_cache["note"]
    note = None
    try:
        root = Path(cfg.root)
        if root.exists():
            dev = root.stat().st_dev
            apart = [str(f) for _, f in inbox_dirs(cfg) if f.exists() and f.stat().st_dev != dev]
            if apart:
                note = (f"{', '.join(apart)} and {cfg.root} are on different mounts: every move is a copy "
                        f"through the container and back over the network. Mount their common parent once "
                        f"(PHOTOS_BASE in .env) so a move is a rename.")
    except OSError:
        note = None
    _mount_cache.update(key=key, at=time.time(), note=note)
    return note


_clusters_cache: dict = {"key": None, "at": 0.0, "value": []}
CLUSTERS_TTL_S = 300.0


def invalidate_clusters() -> None:
    _clusters_cache["at"] = 0.0


def list_clusters(cfg: Config) -> list[dict]:
    """Every applied cluster (folder with a manifest) under the root. Walking the whole sorted
    tree on a network share takes seconds, so the result is kept for a while and dropped by
    every function here that writes a manifest."""
    key = (cfg.root, cfg.unnamed_dir)
    if _clusters_cache["key"] == key and time.time() - _clusters_cache["at"] < CLUSTERS_TTL_S:
        return [dict(m) for m in _clusters_cache["value"]]
    out = _list_clusters(cfg)
    _clusters_cache.update(key=key, at=time.time(), value=out)
    return [dict(m) for m in out]


def _list_clusters(cfg: Config) -> list[dict]:
    root = Path(cfg.root)
    out = []
    if not root.exists():
        return out
    for mp in root.rglob(MANIFEST):
        if "@eaDir" in mp.parts:
            continue
        m = read_manifest(mp.parent)
        if not m:
            continue
        m["folder"] = str(mp.parent)
        m["rel"] = str(mp.parent.relative_to(root))
        m["unnamed"] = mp.parent.parent.name == cfg.unnamed_dir
        m["n"] = len(m.get("photos", []))
        m["n_review"] = sum(1 for p in m.get("photos", []) if p.get("in_review", p.get("uncertain")))
        out.append(m)
    out.sort(key=lambda m: m.get("start") or "", reverse=True)
    return out


def thumb_for(cfg: Config, photo: Path) -> Path | None:
    cand = photo.parent / cfg.thumb_pattern.format(name=photo.name)
    return cand if cand.exists() else None
