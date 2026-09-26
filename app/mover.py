"""Moving files. Everything is a rename() on the same mount (no copy), every move is
recorded in the target folder's manifest.json so it can be undone and later serves as
the eval set."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import cluster, events, ingest
from .config import Config

MANIFEST = "manifest.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _unique(dst: Path) -> Path:
    if not dst.exists():
        return dst
    stem, suf, i = dst.stem, dst.suffix, 1
    while True:
        cand = dst.with_name(f"{stem}_{i}{suf}")
        if not cand.exists():
            return cand
        i += 1


def _move_with_sidecars(src: Path, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = _unique(dst_dir / src.name)
    shutil.move(str(src), str(dst))          # rename on same fs; copy+delete across mounts
    for extra, newname in ((ingest.sidecar_path(src), dst.name + ingest.SIDECAR_SUFFIX),
                           (src.with_suffix(".xmp"), dst.stem + ".xmp")):
        if extra.exists():
            shutil.move(str(extra), str(dst_dir / newname))
    return dst


def read_manifest(folder: Path) -> dict | None:
    mp = folder / MANIFEST
    if not mp.exists():
        return None
    try:
        return json.loads(mp.read_text())
    except json.JSONDecodeError:
        return None


def write_manifest(folder: Path, data: dict) -> None:
    tmp = folder / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(folder / MANIFEST)


def target_folder(cfg: Config, pr: dict) -> Path:
    root = Path(cfg.root)
    return root / cfg.unnamed_dir / pr["name"] if pr["kind"] == "home" else root / pr["name"]


def apply(cfg: Config, pr: dict) -> dict:
    """Move a proposal's photos into its folder; uncertain ones into <folder>/_review."""
    folder = target_folder(cfg, pr)
    excluded = set(pr.get("excluded", []))
    moved = []
    for p in pr["photos"]:
        src = Path(p["path"])
        if p["path"] in excluded or not src.exists():
            continue
        dst_dir = folder / p["source"] if (cfg.subfolder_by_source and p.get("source")) else folder
        if p.get("uncertain"):
            dst_dir = dst_dir / cfg.review_dir
        dst = _move_with_sidecars(src, dst_dir)
        rec = ingest.read_sidecar(dst) or {}
        rec["cluster"] = pr["name"]
        rec["decision"] = {"by": pr["decision"]["by"], "conf": p["conf"], "kind": pr["kind"]}
        ingest.write_sidecar(dst, rec)
        if cfg.write_xmp_sidecar:
            ingest.write_xmp_keywords(dst, [f"zone/{p['zone']}", f"cluster/{pr['name']}"])
        moved.append({"src": p["path"], "dst": str(dst), "conf": p["conf"], "zone": p["zone"],
                      "source": p.get("source"), "inbox": p.get("inbox"),
                      "uncertain": bool(p.get("uncertain"))})
    manifest = {
        "name": pr["name"], "kind": pr["kind"], "start": pr["start"], "end": pr["end"],
        "proposal_id": pr["id"], "decision": pr["decision"], "applied": _now(),
        "label": None, "photos": moved,
    }
    write_manifest(folder, manifest)
    events.log("apply", proposal=pr["id"], cluster_kind=pr["kind"], name=pr["name"], n=len(moved),
               decision_id=pr["decision"].get("id"))
    return manifest


def apply_everyday(cfg: Config, min_age_days: float) -> int:
    """Move unclustered inbox photos older than min_age_days into root/YYYY/MM."""
    if cfg.everyday_layout == "leave":
        return 0
    props = cluster.load_proposals()
    clustered = {p["path"] for pr in props.values() if pr["status"] in ("pending", "ongoing", "approved")
                 for p in pr["photos"]}
    recs, _ = cluster.load_records(cfg)
    cutoff = datetime.now(timezone.utc).timestamp() - min_age_days * 86400
    n = 0
    for r in recs:
        if r["path"] in clustered or r["_t"].timestamp() > cutoff:
            continue
        sub = cfg.everyday_layout.replace("YYYY", f"{r['_t']:%Y}").replace("MM", f"{r['_t']:%m}")
        dst_dir = Path(cfg.root) / sub
        if cfg.subfolder_by_source and r.get("source"):
            dst_dir = dst_dir / r["source"]
        _move_with_sidecars(Path(r["path"]), dst_dir)
        n += 1
    if n:
        events.log("apply_everyday", n=n)
    return n


def undo(cfg: Config, folder: Path) -> int:
    """Move every photo listed in the manifest back to where it came from."""
    m = read_manifest(folder)
    if not m:
        return 0
    n = 0
    for p in m["photos"]:
        dst = Path(p["dst"])
        if dst.exists():
            _move_with_sidecars(dst, Path(p["src"]).parent)
            n += 1
    (folder / MANIFEST).unlink(missing_ok=True)
    for sub in sorted(folder.rglob("*"), key=lambda x: -len(x.parts)):
        if sub.is_dir():
            try:
                sub.rmdir()
            except OSError:
                pass
    try:
        folder.rmdir()
    except OSError:
        pass
    events.log("undo", name=m["name"], n=n, proposal=m.get("proposal_id"))
    return n


def rename(cfg: Config, folder: Path, new_name: str) -> Path:
    new_name = cluster.sanitize(new_name)
    root = Path(cfg.root)
    # a named home burst leaves _unnamed
    dst = root / new_name if folder.parent.name == cfg.unnamed_dir else folder.with_name(new_name)
    dst = _unique(dst)
    folder.rename(dst)
    m = read_manifest(dst)
    if m:
        m["label"] = new_name
        old = m["name"]
        m["name"] = new_name
        for p in m["photos"]:
            p["dst"] = str(dst / Path(p["dst"]).relative_to(folder))
        write_manifest(dst, m)
        for p in m["photos"]:
            rec = ingest.read_sidecar(Path(p["dst"]))
            if rec:
                rec["cluster"] = new_name
                ingest.write_sidecar(Path(p["dst"]), rec)
        events.log("label", old=old, new=new_name, proposal=m.get("proposal_id"),
                   decision_id=m.get("decision", {}).get("id"))
    return dst


def move_out(cfg: Config, folder: Path, photo: Path) -> None:
    """User says a photo does not belong: back to inbox, logged as a correction."""
    m = read_manifest(folder)
    entry = next((p for p in (m or {}).get("photos", []) if p["dst"] == str(photo)), None)
    rec = ingest.read_sidecar(photo) or {}
    back = Path((entry or {}).get("inbox") or rec.get("inbox") or cfg.inboxes[0]["path"])
    _move_with_sidecars(photo, back)
    if m and entry:
        m["photos"].remove(entry)
        entry["corrected"] = _now()
        m.setdefault("corrections", []).append(entry)
        write_manifest(folder, m)
    events.log("correction", name=folder.name, photo=photo.name,
               decision_id=(m or {}).get("decision", {}).get("id"), note="moved out by user")


def list_clusters(cfg: Config) -> list[dict]:
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
        m["n_review"] = sum(1 for p in m.get("photos", []) if p.get("uncertain"))
        out.append(m)
    out.sort(key=lambda m: m.get("start") or "", reverse=True)
    return out


def thumb_for(cfg: Config, photo: Path) -> Path | None:
    cand = photo.parent / cfg.thumb_pattern.format(name=photo.name)
    return cand if cand.exists() else None
