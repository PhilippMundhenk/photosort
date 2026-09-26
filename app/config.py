"""Configuration: a single YAML file in the data dir, editable from the UI."""
from __future__ import annotations

import copy
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

DATA_DIR = Path(os.environ.get("PHOTOSORT_DATA", "/data"))
CONFIG_PATH = DATA_DIR / "config.yaml"


@dataclass
class Config:
    # Paths (inside the container; map them via docker volumes)
    # Several input folders, each scanned recursively; "name" becomes the per-source subfolder.
    inboxes: list[dict] = field(default_factory=lambda: [
        {"path": "/photos/inbox/phone-a", "name": "phone-a"},
        {"path": "/photos/inbox/phone-b", "name": "phone-b"}])
    subfolder_by_source: bool = False      # 2026-10 Lisbon/smartphone-a/... instead of a flat folder
    root: str = "/photos/sorted"
    everyday_layout: str = "YYYY/MM"      # where non-clustered photos go; "leave" keeps them in inbox
    unnamed_dir: str = "_unnamed"          # home bursts waiting for a name, relative to root
    review_dir: str = "_review"            # uncertain photos inside a cluster folder

    # Home & zones
    # IANA zone of the home; naive EXIF times are read in it and video UTC times are converted to it.
    # Seeded from TZ; a value saved from the UI wins.
    timezone: str = field(default_factory=lambda: os.environ.get("TZ") or "UTC")
    home_lat: float = 0.0
    home_lon: float = 0.0
    home_radius_km: float = 0.5            # < this: "home"
    local_radius_km: float = 20.0          # < this: "local", beyond: "away" (zone label only; the trip /
                                           # day-out decision is by duration, see trip_min_hours)

    # Excursions: a run of photos away from home, ended by any photo at home
    trip_min_hours: float = 20.0           # run spans at least this long -> trip (multi-day); shorter -> day out
    trip_min_photos: int = 3               # located photos a trip needs
    dayout_min_photos: int = 8             # photos a day out near home needs (keeps the school run out);
                                           # mostly-away day outs need only trip_min_photos
    local_gap_hours: float = 12.0          # near home (local zone) a photo-less gap longer than this ends the outing
    trip_gap_days: float = 4.0             # gap without photos > this AND different area -> split
    trip_split_distance_km: float = 300.0  # "different area" if runs are further apart than this
    max_places_in_name: int = 4
    min_city_population: int = 1000        # below this use the state/region name instead of the village
    # Your own place names: photos within radius_km of (lat, lon) are labelled with the name
    # ("Black Forest", "Alps", "Allotment garden"). Grown from the "remember this place" box on rename.
    named_places: list[dict] = field(default_factory=list)   # [{"name", "lat", "lon", "radius_km"}]

    # Bursts (local day outs and home occasions)
    burst_gap_hours: float = 3.0           # photos closer than this belong to the same burst
    burst_min_photos: int = 12
    burst_baseline_factor: float = 4.0     # burst must exceed baseline photos/day * factor
    occasion_confidence_min: float = 0.6   # Kev confidence needed to auto-flag a home burst as occasion

    # Behaviour
    dry_run: bool = True                   # proposals only, nothing moves until approved in the UI
    copy_instead_of_move: bool = False     # copy into the sorted tree; originals stay in the inbox and
                                           # their sidecar records where the copy went (copied_to)
    auto_apply_trips: bool = False         # when not dry_run: apply trip proposals without review
    auto_apply_local: bool = False
    auto_apply_home: bool = False
    scan_interval_min: int = 10
    write_xmp_sidecar: bool = True         # also write keywords into <file>.xmp via exiftool
    photo_extensions: list[str] = field(default_factory=lambda: [
        "jpg", "jpeg", "heic", "heif", "png", "dng", "cr2", "cr3", "nef", "arw", "orf", "rw2", "mp4", "mov"])

    # Decision model: any server speaking TypeSafe's System One API (Jev, Kev, Laya via laya-server).
    # Base URL, e.g. http://laya:8000 (the app appends /v1/systemone). Empty -> rule-based fallback.
    # PHOTOSORT_KEV_URL only seeds the default; a value saved from the UI wins.
    kev_url: str = field(default_factory=lambda: os.environ.get("PHOTOSORT_KEV_URL", ""))
    kev_model: str = ""                    # "model" field of the request; empty -> server default
    kev_timeout_s: float = 20.0
    kev_batch_size: int = 20

    # Sidecars: the per-photo JSON record. "central": <data>/index/<inbox name>/<relative path>
    # (photo folders stay clean; the record is regenerable, so files moved by other tools just get
    # re-indexed); "beside": next to the photo, which follows it wherever it goes.
    sidecar_mode: str = "central"
    sidecar_name: str = "{name}.photosort.json"    # {name} = file name, {stem}, {ext} (without dot)
    sidecar_cleanup: str = "after_move"            # "after_move": no record kept for photos in the sorted
                                                   # tree (manifest.json has it all); "never": keep them

    # Thumbnails: the NAS's own (Synology @eaDir pattern) are used when present; otherwise they are
    # generated into <data>/thumbs (Pillow; exiftool preview for RAW; one ffmpeg frame for videos)
    thumb_pattern: str = "@eaDir/{name}/SYNOPHOTO_THUMB_M.jpg"
    generate_thumbnails: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


def tzinfo(cfg: Config) -> ZoneInfo:
    try:
        return ZoneInfo(cfg.timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


_cache: dict = {"key": None, "cfg": None}


def load() -> Config:
    """Config from YAML. Cached on the file's mtime/size (it is read per request and per
    sidecar); every caller gets its own copy, so mutating it never leaks without save()."""
    try:
        st = CONFIG_PATH.stat()
        key = (str(CONFIG_PATH), st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None and _cache["key"] == key:
        return copy.deepcopy(_cache["cfg"])
    cfg = Config()
    if key is not None:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        _cache.update(key=key, cfg=copy.deepcopy(cfg))
    return cfg


def save(cfg: Config) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(cfg.as_dict(), sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    _cache["key"] = None


def update_from_form(cfg: Config, form: dict) -> Config:
    """Coerce form strings to the dataclass field types."""
    for k, v in form.items():
        if not hasattr(cfg, k):
            continue
        cur = getattr(cfg, k)
        if k == "named_places":
            setattr(cfg, k, parse_named_places(str(v)))
        elif k == "inboxes":
            # one per line: "name=/path" or just "/path"
            items = []
            for line in str(v).splitlines():
                line = line.strip()
                if not line:
                    continue
                name, _, path = line.partition("=") if "=" in line else ("", "", line)
                path = path.strip() or line
                items.append({"path": path, "name": (name.strip() or Path(path).name)})
            setattr(cfg, k, items)
        elif isinstance(cur, bool):
            setattr(cfg, k, str(v).lower() in ("1", "true", "on", "yes"))
        elif isinstance(cur, int):
            setattr(cfg, k, int(v))
        elif isinstance(cur, float):
            setattr(cfg, k, float(v))
        elif isinstance(cur, list):
            setattr(cfg, k, [s.strip().lstrip(".").lower() for s in str(v).split(",") if s.strip()])
        else:
            setattr(cfg, k, str(v).strip())
    # checkboxes that were unticked are absent from the form
    for k, cur in cfg.as_dict().items():
        if isinstance(cur, bool) and k not in form:
            setattr(cfg, k, False)
    return cfg


def parse_named_places(text: str) -> list[dict]:
    """One per line: 'Name = lat, lon, radius_km' (radius optional, default 2 km)."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        name, _, rest = line.partition("=")
        nums = [x.strip() for x in rest.replace(";", ",").split(",") if x.strip()]
        try:
            lat, lon = float(nums[0]), float(nums[1])
            radius = float(nums[2]) if len(nums) > 2 else 2.0
        except (IndexError, ValueError):
            continue
        if name.strip():
            out.append({"name": name.strip(), "lat": lat, "lon": lon, "radius_km": max(0.05, radius)})
    return out


def named_places_text(cfg: Config) -> str:
    return "\n".join(f"{p['name']} = {p['lat']:.5f}, {p['lon']:.5f}, {p['radius_km']:g}" for p in cfg.named_places)


def inbox_dirs(cfg: Config) -> list[tuple[str, Path]]:
    return [(i.get("name") or Path(i["path"]).name, Path(i["path"])) for i in cfg.inboxes]


def inboxes_text(cfg: Config) -> str:
    return "\n".join(f"{i.get('name') or Path(i['path']).name}={i['path']}" for i in cfg.inboxes)
