"""Ingest: read EXIF via exiftool, write one JSON sidecar per photo.

Sidecar = <photo>.photosort.json, next to the file. It is the machine record and is
regenerable (delete it and the next scan recreates it). Optionally keywords are also
written to <photo>.xmp so other tools (digiKam, Lightroom) can see the zone/cluster.
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import geo
from .config import Config, inbox_dirs

SIDECAR_SUFFIX = ".photosort.json"
SIDECAR_VERSION = 1
log = logging.getLogger("photosort.ingest")


class ExifToolMissing(RuntimeError):
    """exiftool is not on PATH; the scan skips new photos instead of writing empty sidecars."""


def sidecar_path(photo: Path) -> Path:
    return photo.with_name(photo.name + SIDECAR_SUFFIX)


def is_photo(cfg: Config, p: Path) -> bool:
    return p.is_file() and p.suffix.lower().lstrip(".") in cfg.photo_extensions \
        and "@eaDir" not in p.parts and not p.name.startswith(".")


def list_photos(cfg: Config, folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if is_photo(cfg, p))


def read_sidecar(photo: Path) -> dict | None:
    sp = sidecar_path(photo)
    if not sp.exists():
        return None
    try:
        return json.loads(sp.read_text())
    except json.JSONDecodeError:
        return None


def write_sidecar(photo: Path, rec: dict) -> None:
    sp = sidecar_path(photo)
    tmp = sp.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    tmp.replace(sp)


# --- exiftool -----------------------------------------------------------------

_EXIF_TAGS = ["-DateTimeOriginal", "-CreateDate", "-MediaCreateDate", "-FileModifyDate",
              "-GPSLatitude", "-GPSLongitude", "-Model", "-Make", "-OffsetTimeOriginal"]


def exif_batch(paths: list[Path]) -> dict[str, dict]:
    """Run exiftool once over many files; returns {path: tags}."""
    if not paths:
        return {}
    out: dict[str, dict] = {}
    # exiftool handles long arg lists fine, but keep batches moderate for the T430's RAM
    for i in range(0, len(paths), 200):
        chunk = paths[i:i + 200]
        cmd = ["exiftool", "-json", "-n", "-q", "-fast2", "-c", "%.6f", *_EXIF_TAGS, *map(str, chunk)]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        except FileNotFoundError as e:
            raise ExifToolMissing("exiftool not found on PATH") from e
        if res.returncode not in (0, 1) or not res.stdout.strip():
            continue
        for item in json.loads(res.stdout):
            out[item.get("SourceFile", "")] = item
    return out


def _parse_dt(tags: dict) -> str | None:
    for key in ("DateTimeOriginal", "CreateDate", "MediaCreateDate", "FileModifyDate"):
        v = tags.get(key)
        if not v or str(v).startswith("0000"):
            continue
        s = str(v)
        # "2026:10:03 14:22:31" or with offset "+02:00"
        try:
            base = s[:19].replace(":", "-", 2)
            dt = datetime.strptime(base, "%Y-%m-%d %H:%M:%S")
            off = tags.get("OffsetTimeOriginal") or (s[19:] if len(s) > 19 else "")
            if off and off[0] in "+-" and len(off) >= 6:
                sign = 1 if off[0] == "+" else -1
                hh, mm = int(off[1:3]), int(off[4:6])
                from datetime import timedelta
                tz = timezone(sign * timedelta(hours=hh, minutes=mm))
                dt = dt.replace(tzinfo=tz)
            return dt.isoformat()
        except ValueError:
            continue
    return None


def build_record(cfg: Config, photo: Path, tags: dict) -> dict:
    lat, lon = tags.get("GPSLatitude"), tags.get("GPSLongitude")
    rec = {
        "v": SIDECAR_VERSION,
        "file": photo.name,
        "ts": _parse_dt(tags),
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
        "camera": " ".join(x for x in (tags.get("Make"), tags.get("Model")) if x) or None,
        "gps_source": "exif" if lat is not None else None,
        "dist_km": None,
        "zone": geo.ZONE_UNKNOWN,
        "place": None,
        "cluster": None,        # folder name once assigned
        "decision": None,       # {"by": "rule"|"kev", "conf": float, "note": str}
        "indexed": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    enrich_location(cfg, rec)
    return rec


def enrich_location(cfg: Config, rec: dict) -> None:
    if rec.get("lat") is None or rec.get("lon") is None:
        rec["dist_km"], rec["zone"], rec["place"] = None, geo.ZONE_UNKNOWN, None
        return
    d = geo.haversine_km(cfg.home_lat, cfg.home_lon, rec["lat"], rec["lon"]) if cfg.home_lat or cfg.home_lon else None
    rec["dist_km"] = round(d, 3) if d is not None else None
    rec["zone"] = geo.zone_for(cfg, d)
    rec["place"] = geo.reverse(cfg, rec["lat"], rec["lon"])


def scan(cfg: Config, force: bool = False) -> dict:
    """Index every photo in every inbox (recursively) lacking a sidecar. Returns stats."""
    stats = {"new": 0, "total": 0, "missing": []}
    for name, folder in inbox_dirs(cfg):
        if not folder.exists():
            stats["missing"].append(str(folder))
            continue
        photos = list_photos(cfg, folder)
        todo = [p for p in photos if force or not sidecar_path(p).exists()]
        try:
            tags = exif_batch(todo)
        except ExifToolMissing as e:
            log.error("%s: %d new photos in %s left unindexed", e, len(todo), folder)
            stats["error"] = str(e)
            todo = []
            tags = {}
        for p in todo:
            rec = build_record(cfg, p, tags.get(str(p), {}))
            rec["source"] = name
            rec["inbox"] = str(folder)
            write_sidecar(p, rec)
        # re-zone existing sidecars if home/radii changed: cheap, no exiftool needed
        for p in photos:
            if p in todo:
                continue
            rec = read_sidecar(p)
            if rec is None:
                continue
            old = (rec.get("zone"), rec.get("dist_km"), rec.get("source"))
            enrich_location(cfg, rec)
            rec.setdefault("source", name)
            rec.setdefault("inbox", str(folder))
            if (rec.get("zone"), rec.get("dist_km"), rec.get("source")) != old:
                write_sidecar(p, rec)
        stats["new"] += len(todo)
        stats["total"] += len(photos)
    return stats


def write_xmp_keywords(photo: Path, keywords: list[str]) -> None:
    """Best effort: keep an .xmp sidecar with photosort/* keywords for other tools."""
    xmp = photo.with_suffix(".xmp")
    args = ["-api", "NoDups=1"] + [f"-XMP-dc:Subject+=photosort/{k}" for k in keywords]
    if xmp.exists():
        cmd = ["exiftool", "-q", "-overwrite_original", *args, str(xmp)]
    else:
        cmd = ["exiftool", "-q", "-o", str(xmp), *args, str(photo)]
    subprocess.run(cmd, capture_output=True)
