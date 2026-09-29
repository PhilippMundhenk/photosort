"""Ingest: read EXIF via exiftool, write one JSON record ("sidecar") per photo.

The record is the machine's memory of a photo (timestamp, GPS, place, zone, cluster,
decision) and is regenerable: delete it and the next scan recreates it. Where it lives is
configurable (cfg.sidecar_mode): "central" keeps all records under <data>/index/, mirroring
the inbox name and the photo's relative path, so photo folders stay clean; "beside" puts it
next to the photo (<photo>.photosort.json by default, cfg.sidecar_name) so it follows the
file wherever it goes. Optionally keywords are also written to <photo>.xmp so other tools
(digiKam, Lightroom) can see the zone/cluster.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as _config
from . import geo
from .config import DATA_DIR, Config, inbox_dirs, tzinfo

SIDECAR_SUFFIX = ".photosort.json"
SIDECAR_VERSION = 2          # 2: ts_source, offsets from GPSDateTime, 0/0 GPS ignored (records < 2 are re-indexed)
INDEX_DIR = DATA_DIR / "index"
log = logging.getLogger("photosort.ingest")

# Change tracking for the record cache in cluster.load_records (reading a thousand small files
# through a bind mount costs seconds, so the cache must not be thrown away for one changed
# record): every write/delete names its photo, scans name the files that appeared or vanished,
# migrations and purges invalidate everything.
generation = 0
changed: dict = {"all": True, "paths": set()}


def _bump(path: Path | None = None) -> None:
    global generation
    generation += 1
    if path is None:
        changed["all"] = True
    else:
        changed["paths"].add(str(path))


_known: dict[str, set[str]] = {}          # inbox folder -> photo paths seen by the last scan or load


def known_files(folder: Path, photos: list[Path]) -> None:
    """Record which files a full load saw, so the following scan only marks differences."""
    _known[str(folder)] = {str(p) for p in photos}


class ExifToolMissing(RuntimeError):
    """exiftool is not on PATH; the scan skips new photos instead of writing empty sidecars."""


# --- where a photo's record lives -------------------------------------------------------------

def sidecar_name(cfg: Config, photo: Path) -> str:
    try:
        name = cfg.sidecar_name.format(name=photo.name, stem=photo.stem, ext=photo.suffix.lstrip("."))
    except (KeyError, IndexError, ValueError):
        name = photo.name + SIDECAR_SUFFIX
    return name if name and name != photo.name else photo.name + SIDECAR_SUFFIX


def _anchor(cfg: Config, photo: Path) -> tuple[str, Path] | None:
    """(inbox name or 'sorted', path relative to it) for a photo under a known folder."""
    roots = [*inbox_dirs(cfg), ("sorted", Path(cfg.root))]
    for name, folder in roots:
        try:
            return name, photo.relative_to(folder)
        except ValueError:
            continue
    return None


def sidecar_path(photo: Path, cfg: Config | None = None, mode: str | None = None) -> Path:
    cfg = cfg or _config.load()
    mode = mode or cfg.sidecar_mode
    if mode == "beside":
        return photo.with_name(sidecar_name(cfg, photo))
    anchor = _anchor(cfg, photo)
    if anchor:
        name, rel = anchor
        return INDEX_DIR / name / rel.parent / sidecar_name(cfg, photo)
    h = hashlib.sha1(str(photo).encode("utf-8", "surrogateescape")).hexdigest()
    return INDEX_DIR / "_other" / h[:2] / (h[2:14] + "_" + sidecar_name(cfg, photo))


def is_photo(cfg: Config, p: Path) -> bool:
    return p.is_file() and p.suffix.lower().lstrip(".") in cfg.photo_extensions \
        and "@eaDir" not in p.parts and not p.name.startswith(".")


def list_photos(cfg: Config, folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if is_photo(cfg, p))


def read_sidecar(photo: Path, cfg: Config | None = None) -> dict | None:
    sp = sidecar_path(photo, cfg)
    if not sp.exists():
        return None
    try:
        return json.loads(sp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_sidecar(photo: Path, rec: dict, cfg: Config | None = None) -> None:
    sp = sidecar_path(photo, cfg)
    sp.parent.mkdir(parents=True, exist_ok=True)
    rec = {**rec, "path": str(photo)}                  # lets a central index find orphans
    tmp = sp.with_name(sp.name + ".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(sp)
    _bump(photo)


def delete_sidecar(photo: Path, cfg: Config | None = None) -> bool:
    sp = sidecar_path(photo, cfg)
    if not sp.exists():
        return False
    sp.unlink()
    _prune_empty(sp.parent)
    _bump(photo)
    return True


def move_sidecar(src: Path, dst: Path, cfg: Config | None = None, keep_source: bool = False) -> bool:
    """The record follows the photo from src to dst (copy mode keeps the source's)."""
    rec = read_sidecar(src, cfg)
    if rec is None:
        return False
    write_sidecar(dst, rec, cfg)
    if not keep_source:
        delete_sidecar(src, cfg)
    return True


def _prune_empty(folder: Path) -> None:
    """Remove now-empty index folders up to the index root (never photo folders)."""
    try:
        while folder != INDEX_DIR and folder.is_relative_to(INDEX_DIR) and not any(folder.iterdir()):
            folder.rmdir()
            folder = folder.parent
    except OSError:
        pass


def _record_files(cfg: Config) -> dict[str, Path]:
    """Every record file under the index and the photo folders, by the photo path it names.
    (Records written before September 2026 have no path and are found by name instead.)"""
    found: dict[str, Path] = {}
    folders = [INDEX_DIR, *(f for _, f in inbox_dirs(cfg)), Path(cfg.root)]
    for folder in folders:
        if not folder.exists():
            continue
        for f in folder.rglob("*.json"):
            if f.name == "manifest.json" or not f.is_file():
                continue
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(rec, dict) and rec.get("path"):
                found.setdefault(rec["path"], f)
    return found


def migrate_sidecars(cfg: Config) -> dict:
    """Move every record to the configured location (beside <-> central, or a renamed
    pattern): for each photo under an inbox or the sorted root, its record found anywhere
    else (by the path stored inside it, or by the default name) is moved. Returns counts."""
    stats = {"moved": 0, "kept": 0, "photos": 0}
    by_path = _record_files(cfg)
    default = Config(**{**cfg.as_dict(), "sidecar_name": "{name}.photosort.json"})
    for _, folder in [*inbox_dirs(cfg), ("sorted", Path(cfg.root))]:
        if not folder.exists():
            continue
        for p in list_photos(cfg, folder):
            stats["photos"] += 1
            target = sidecar_path(p, cfg)
            if target.exists():
                stats["kept"] += 1
                continue
            candidates = [by_path.get(str(p))] + [sidecar_path(p, c, mode=m) for c in (cfg, default)
                                                  for m in ("beside", "central")]
            for cand in candidates:
                if cand and cand != target and cand.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    cand.replace(target)
                    _prune_empty(cand.parent)
                    stats["moved"] += 1
                    break
    _bump()
    return stats


def purge_sidecars(cfg: Config, sorted_tree: bool = True, orphans: bool = True) -> dict:
    """Delete records that are no longer needed: those of photos in the sorted tree (if
    sorted_tree) and central records whose photo no longer exists (orphans)."""
    stats = {"sorted": 0, "orphans": 0}
    root = Path(cfg.root)
    if sorted_tree and root.exists():
        for p in list_photos(cfg, root):
            if delete_sidecar(p, cfg):
                stats["sorted"] += 1
    if orphans and INDEX_DIR.exists():
        for f in list(INDEX_DIR.rglob("*")):
            if not f.is_file() or f.suffix != ".json":
                continue
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            path = rec.get("path")
            if path and not Path(path).exists():
                f.unlink()
                _prune_empty(f.parent)
                stats["orphans"] += 1
    _bump()
    return stats


# --- exiftool -----------------------------------------------------------------

# Photos: EXIF DateTimeOriginal is local wall time, OffsetTimeOriginal (if any) its zone.
# Videos (QuickTime/MP4, iPhone and Android): CreateDate/MediaCreateDate are UTC by spec;
# iPhones also write Keys:CreationDate with the local offset, which is exact. GPS comes from
# EXIF or from QuickTime Keys:GPSCoordinates; exiftool folds both into GPSLatitude/Longitude.
_EXIF_TAGS = ["-DateTimeOriginal", "-OffsetTimeOriginal", "-GPSDateTime", "-Keys:CreationDate", "-CreateDate",
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


def _num(v) -> float | None:
    """A number from an exiftool value, or None: malformed GPS tags come back as '' or text."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None      # NaN/inf: no position


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
        delta = timedelta(hours=int(off[1:3]), minutes=int(off[4:6]))
        if delta <= timedelta(hours=14):                       # real zones end at +14:00; beyond is a corrupt tag
            return dt.replace(tzinfo=timezone(sign * delta))
    return dt


def _gps_offset(local: datetime, tags: dict):
    """The camera's UTC offset from the GPS clock: a photo taken at 13:58 local with a GPS fix at
    12:58Z was taken at +01:00. Rounded to 15 min; None without a usable GPS time."""
    v = tags.get("GPSDateTime")
    if not v:
        return None
    utc = _exif_dt(str(v))
    if utc is None:
        return None
    utc = utc.replace(tzinfo=None)
    seconds = (local - utc).total_seconds()
    off = round(seconds / 900) * 900                       # real zones are multiples of 15 min
    if abs(off) > 14 * 3600 or abs(seconds - off) > 120:   # more than 2 min drift: the clocks disagree, no offset
        return None
    return timezone(timedelta(seconds=off))


def _parse_dt(tags: dict, video: bool = False, tz=None, name: str = "") -> tuple[str | None, str | None]:
    """Best timestamp from the tags, as (ISO text, ts_source).

    Photos: local wall time; zone-aware when the EXIF offset is present ("exif+offset") or when
    the GPS clock reveals the offset ("gps"); otherwise naive ("exif", read as home-zone time).
    Videos: always zone-aware. Keys:CreationDate (iPhone, has an offset) wins ("exif+offset");
    QuickTime CreateDate/MediaCreateDate are UTC by spec and are converted to the home zone
    ("utc-home"); clustering later re-expresses such a video in the offset of the nearest photo
    whose offset is known, so a clip shot abroad shows local time. A capture time in the file
    name ("name") beats the file modification time ("mtime"), which copies and syncs rewrite."""
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
            source = "name"
        else:
            v = tags.get(key)
            if not v or str(v).startswith("0000"):
                continue
            offset = str(tags.get("OffsetTimeOriginal") or "") if key == "DateTimeOriginal" else ""
            dt = _exif_dt(str(v), offset)
            if dt is None:
                continue
            source = "mtime" if key == "FileModifyDate" else "exif+offset" if dt.tzinfo else "exif"
            if dt.tzinfo is None and not video and key in ("DateTimeOriginal", "CreateDate"):
                gps_tz = _gps_offset(dt, tags)
                if gps_tz is not None:
                    dt, source = dt.replace(tzinfo=gps_tz), "gps"
        if video:
            if kind == "utc":                                  # QuickTime clock: UTC whether or not it says "Z"
                dt = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(tz)
                source = "utc-home"
            elif dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
        return dt.isoformat(), source
    return None, None


def _camera(tags: dict, source: str | None) -> tuple[str | None, str | None]:
    """(device name, where it came from). EXIF Make/Model (photos, iPhone videos), the Android
    QuickTime keys, else the inbox name: one inbox is one device in this setup."""
    for keys, how in ((("Make", "Model"), "exif"), (("AndroidManufacturer", "AndroidModel"), "exif")):
        name = " ".join(str(tags[k]).strip() for k in keys if tags.get(k))
        if name:
            return name, how
    return (source, "inbox") if source else (None, None)


def build_record(cfg: Config, photo: Path, tags: dict, source: str | None = None) -> dict:
    lat, lon = _num(tags.get("GPSLatitude")), _num(tags.get("GPSLongitude"))
    if lat is None or lon is None or (lat == 0.0 and lon == 0.0) or abs(lat) > 90 or abs(lon) > 180:
        lat = lon = None                                   # "0 0": a phone without a fix, not the Gulf of Guinea;
                                                           # out of range: a corrupt tag, not a position
    video = is_video(photo, tags)
    camera, camera_source = _camera(tags, source)
    ts, ts_source = _parse_dt(tags, video=video, tz=tzinfo(cfg), name=photo.name)
    rec = {
        "v": SIDECAR_VERSION,
        "file": photo.name,
        "media": "video" if video else "photo",
        "ts": ts,
        "ts_source": ts_source,
        "lat": lat,
        "lon": lon,
        "camera": camera,
        "camera_source": camera_source,
        "gps_source": "exif" if lat is not None else None,
        "dist_km": None,
        "zone": geo.ZONE_UNKNOWN,
        "place": None,
        "cluster": None,        # folder name once assigned
        "decision": None,       # {"by": "rule"|"user", "conf": float, "kind": str}
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


def _needs_index(p: Path, cfg: Config) -> bool:
    rec = read_sidecar(p, cfg)
    return rec is None or int(rec.get("v") or 0) < SIDECAR_VERSION


SCAN_CHUNK = 200
ZONING_PATH = DATA_DIR / "zoning.json"


def zoning_key(cfg: Config) -> str:
    """Everything a stored record's zone, distance and place depend on. Re-zoning every record
    (read, geocode, compare) on every run cost twenty thousand reads and lookups per ten
    minutes on a large library; it is only needed when one of these changed."""
    parts = [SIDECAR_VERSION, cfg.home_lat, cfg.home_lon, cfg.home_radius_km, cfg.local_radius_km,
             cfg.named_places, cfg.min_city_population, cfg.sidecar_mode, cfg.sidecar_name]
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _zoned_with() -> str | None:
    try:
        return json.loads(ZONING_PATH.read_text(encoding="utf-8")).get("key")
    except (OSError, ValueError, AttributeError):
        return None


def scan(cfg: Config, force: bool = False, progress=None) -> dict:
    """Index every photo in every inbox (recursively) lacking a sidecar. Returns stats.
    progress(done, total) is called per chunk of new files (a first scan of a big library
    takes long; the UI shows where it is)."""
    stats = {"new": 0, "total": 0, "missing": []}
    key = zoning_key(cfg)
    rezone = force or _zoned_with() != key
    complete = True
    plan: list[tuple[str, Path, list[Path], list[Path]]] = []
    for name, folder in inbox_dirs(cfg):
        if not folder.exists():
            stats["missing"].append(str(folder))
            if _known.pop(str(folder), None) is not None:  # the share dropped out: its records leave the
                _bump()                                     # cache too, so nothing is proposed or approved
            continue                                        # from photos nobody can reach right now
        photos = list_photos(cfg, folder)
        plan.append((name, folder, photos, [p for p in photos if force or _needs_index(p, cfg)]))
    total_new = sum(len(todo) for _, _, _, todo in plan)
    done = 0
    if progress:
        progress(0, total_new)
    for name, folder, photos, todo in plan:
        indexed: list[Path] = []
        for i in range(0, len(todo), SCAN_CHUNK):
            chunk = todo[i:i + SCAN_CHUNK]
            try:
                tags = exif_batch(chunk)
            except ExifToolMissing as e:
                log.error("%s: %d new photos in %s left unindexed", e, len(todo) - i, folder)
                stats["error"] = str(e)
                complete = False
                break
            for p in chunk:
                try:
                    rec = build_record(cfg, p, tags.get(str(p), {}), source=name)
                except Exception as e:  # noqa: BLE001  (one odd file must never abort the scan)
                    log.exception("cannot index %s: %s", p, e)
                    stats.setdefault("failed", []).append(str(p))
                    continue
                rec["source"] = name
                rec["inbox"] = str(folder)
                write_sidecar(p, rec, cfg)
                indexed.append(p)
            done += len(chunk)
            if progress:
                progress(done, total_new)
        todo = indexed
        # re-zone existing sidecars if home/radii/places changed: no exiftool needed
        for p in (photos if rezone else ()):
            if p in todo:
                continue
            rec = read_sidecar(p, cfg)
            if rec is None:
                continue
            old = (rec.get("zone"), rec.get("dist_km"), rec.get("source"), rec.get("place"))
            enrich_location(cfg, rec)
            rec.setdefault("source", name)
            rec.setdefault("inbox", str(folder))
            if (rec.get("zone"), rec.get("dist_km"), rec.get("source"), rec.get("place")) != old:
                write_sidecar(p, rec, cfg)
        stats["new"] += len(todo)
        stats["total"] += len(photos)
        now = {str(p) for p in photos}
        before = _known.get(str(folder))
        if before is not None:
            for gone in before - now:          # a file removed by another tool: drop it from the cache
                _bump(Path(gone))
        elif not todo:
            _bump()                            # first scan in this process: the cache may predate it
        _known[str(folder)] = now
    if rezone and complete and not stats["missing"]:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ZONING_PATH.write_text(json.dumps({"key": key}), encoding="utf-8")
    stats["rezoned"] = rezone
    return stats


def _xmp_args(photo: Path, keywords: list[str]) -> list[str]:
    xmp = photo.with_suffix(".xmp")
    # remove-then-add per keyword: exiftool's NoDups only dedupes within the values being written,
    # so a plain += would append the same keyword again on every run
    args = []
    for k in keywords:
        args += [f"-XMP-dc:Subject-=photosort/{k}", f"-XMP-dc:Subject+=photosort/{k}"]
    if xmp.exists():
        return ["-q", "-overwrite_original", *args, str(xmp)]
    return ["-q", "-o", str(xmp), *args, str(photo)]


def write_xmp_keywords(photo: Path, keywords: list[str]) -> None:
    """Best effort: keep an .xmp sidecar with photosort/* keywords for other tools."""
    write_xmp_keywords_batch([(photo, keywords)])


def write_xmp_keywords_batch(items: list[tuple[Path, list[str]]]) -> None:
    """One exiftool process for many photos (-execute separates the jobs): starting exiftool
    costs a good part of a second, and one start per moved file made a move of 87 files take
    minutes over a share."""
    if not items:
        return
    for i in range(0, len(items), 500):
        chunk = items[i:i + 500]
        cmd = ["exiftool"]
        for j, (photo, keywords) in enumerate(chunk):
            if j:
                cmd.append("-execute")
            cmd += _xmp_args(photo, keywords)
        try:
            subprocess.run(cmd, capture_output=True)
        except FileNotFoundError:
            return
