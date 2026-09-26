"""Ingest: read EXIF via exiftool, write one JSON sidecar per photo.

Sidecar = <photo>.photosort.json, next to the file. It is the machine record and is
regenerable (delete it and the next scan recreates it). Optionally keywords are also
written to <photo>.xmp so other tools (digiKam, Lightroom) can see the zone/cluster.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import geo
from .config import Config, inbox_dirs, tzinfo

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

# Photos: EXIF DateTimeOriginal is local wall time, OffsetTimeOriginal (if any) its zone.
# Videos (QuickTime/MP4, iPhone and Android): CreateDate/MediaCreateDate are UTC by spec;
# iPhones also write Keys:CreationDate with the local offset, which is exact. GPS comes from
# EXIF or from QuickTime Keys:GPSCoordinates; exiftool folds both into GPSLatitude/Longitude.
_EXIF_TAGS = ["-DateTimeOriginal", "-OffsetTimeOriginal", "-Keys:CreationDate", "-CreateDate",
              "-MediaCreateDate", "-FileModifyDate", "-MIMEType",
              "-GPSLatitude", "-GPSLongitude", "-Make", "-Model", "-AndroidManufacturer", "-AndroidModel"]
VIDEO_EXTENSIONS = {"mp4", "mov", "m4v", "3gp", "mkv", "avi", "webm", "mts", "m2ts"}


def exif_batch(paths: list[Path]) -> dict[str, dict]:
    """Run exiftool once over many files; returns {path: tags}.

    Photos are read with -fast2 (header only). Videos are not: phones write the moov atom with
    all metadata *after* the media data, and -fast2 stops before it on large files, returning
    no date and no GPS at all."""
    if not paths:
        return {}
    out: dict[str, dict] = {}
    groups = ((["-fast2"], [p for p in paths if not is_video(p)]), ([], [p for p in paths if is_video(p)]))
    for fast, group in groups:
        # exiftool handles long arg lists fine, but keep batches moderate for the T430's RAM
        for i in range(0, len(group), 200):
            chunk = group[i:i + 200]
            cmd = ["exiftool", "-json", "-n", "-q", *fast, "-c", "%.6f", *_EXIF_TAGS, *map(str, chunk)]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            except FileNotFoundError as e:
                raise ExifToolMissing("exiftool not found on PATH") from e
            if res.returncode not in (0, 1) or not res.stdout.strip():
                continue
            for item in json.loads(res.stdout):
                out[item.get("SourceFile", "")] = item
    return out


# 20260101_000025.jpg, IMG_20260101_000025.jpg, PXL_20260101_000025123.mp4, VID_20260101_000025.mp4
_NAME_TS = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})(?!\d{3,}\d)")


def _name_dt(name: str) -> datetime | None:
    """Phones name files after the local capture time; last resort before the file mtime."""
    m = _NAME_TS.search(name)
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups()))
    except ValueError:
        return None


def is_video(path: Path, tags: dict | None = None) -> bool:
    mime = str((tags or {}).get("MIMEType") or "")
    return mime.startswith("video/") or path.suffix.lower().lstrip(".") in VIDEO_EXTENSIONS


def _exif_dt(s: str, offset: str = "") -> datetime | None:
    """'2026:10:03 14:22:31', optionally followed by (or given) '+02:00'/'Z'. Naive if no offset."""
    try:
        dt = datetime.strptime(s[:19].replace(":", "-", 2), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    off = offset or s[19:].strip()
    if off == "Z":
        return dt.replace(tzinfo=timezone.utc)
    if off and off[0] in "+-" and len(off) >= 6 and off[1:3].isdigit() and off[4:6].isdigit():
        sign = 1 if off[0] == "+" else -1
        return dt.replace(tzinfo=timezone(sign * timedelta(hours=int(off[1:3]), minutes=int(off[4:6]))))
    return dt


def _parse_dt(tags: dict, video: bool = False, tz=None, name: str = "") -> str | None:
    """Best timestamp from the tags, as ISO text. Photos keep their (naive) local wall time plus
    the EXIF offset when there is one. Videos are always returned zone-aware, in the home zone:
    Keys:CreationDate (iPhone, has an offset) wins; QuickTime CreateDate/MediaCreateDate are
    UTC by spec (Android, iPhone) and are converted. A capture time in the file name beats the
    file modification time, which copies and syncs rewrite."""
    tz = tz or timezone.utc
    order = (("CreationDate", "local"), ("CreateDate", "utc"), ("MediaCreateDate", "utc"),
             ("@name", "local"), ("FileModifyDate", "local")) if video else \
            (("DateTimeOriginal", "local"), ("CreateDate", "local"), ("MediaCreateDate", "local"),
             ("@name", "local"), ("FileModifyDate", "local"))
    for key, kind in order:
        if key == "@name":
            dt = _name_dt(name)
            if dt is None:
                continue
        else:
            v = tags.get(key)
            if not v or str(v).startswith("0000"):
                continue
            offset = str(tags.get("OffsetTimeOriginal") or "") if key == "DateTimeOriginal" else ""
            dt = _exif_dt(str(v), offset)
            if dt is None:
                continue
        if video:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc if kind == "utc" else tz)
            dt = dt.astimezone(tz)
        return dt.isoformat()
    return None


def _camera(tags: dict, source: str | None) -> tuple[str | None, str | None]:
    """(device name, where it came from). EXIF Make/Model (photos, iPhone videos), the Android
    QuickTime keys, else the inbox name: one inbox is one device in this setup."""
    for keys, how in ((("Make", "Model"), "exif"), (("AndroidManufacturer", "AndroidModel"), "exif")):
        name = " ".join(str(tags[k]).strip() for k in keys if tags.get(k))
        if name:
            return name, how
    return (source, "inbox") if source else (None, None)


def build_record(cfg: Config, photo: Path, tags: dict, source: str | None = None) -> dict:
    lat, lon = tags.get("GPSLatitude"), tags.get("GPSLongitude")
    video = is_video(photo, tags)
    camera, camera_source = _camera(tags, source)
    rec = {
        "v": SIDECAR_VERSION,
        "file": photo.name,
        "media": "video" if video else "photo",
        "ts": _parse_dt(tags, video=video, tz=tzinfo(cfg), name=photo.name),
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
        "camera": camera,
        "camera_source": camera_source,
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
            rec = build_record(cfg, p, tags.get(str(p), {}), source=name)
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
    # remove-then-add per keyword: exiftool's NoDups only dedupes within the values being written,
    # so a plain += would append the same keyword again on every run
    args = []
    for k in keywords:
        args += [f"-XMP-dc:Subject-=photosort/{k}", f"-XMP-dc:Subject+=photosort/{k}"]
    if xmp.exists():
        cmd = ["exiftool", "-q", "-overwrite_original", *args, str(xmp)]
    else:
        cmd = ["exiftool", "-q", "-o", str(xmp), *args, str(photo)]
    subprocess.run(cmd, capture_output=True)
