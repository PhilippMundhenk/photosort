"""Configuration: a single YAML file in the data dir, editable from the UI."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

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
    home_lat: float = 0.0
    home_lon: float = 0.0
    home_radius_km: float = 0.5            # < this: "home"
    local_radius_km: float = 20.0          # < this: "local" (day out); beyond: "away" (trip)

    # Trips
    trip_min_photos: int = 3
    trip_gap_days: float = 4.0             # gap without photos > this AND different area -> split
    trip_split_distance_km: float = 300.0  # "different area" if runs are further apart than this
    max_places_in_name: int = 4
    min_city_population: int = 20000       # below this use the state/region name instead of the village

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

    # Thumbnails: Synology @eaDir, otherwise none
    thumb_pattern: str = "@eaDir/{name}/SYNOPHOTO_THUMB_M.jpg"

    def as_dict(self) -> dict:
        return asdict(self)


def load() -> Config:
    cfg = Config()
    if CONFIG_PATH.exists():
        data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
    return cfg


def save(cfg: Config) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(cfg.as_dict(), sort_keys=False, allow_unicode=True))
    tmp.replace(CONFIG_PATH)


def update_from_form(cfg: Config, form: dict) -> Config:
    """Coerce form strings to the dataclass field types."""
    for k, v in form.items():
        if not hasattr(cfg, k):
            continue
        cur = getattr(cfg, k)
        if k == "inboxes":
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


def inbox_dirs(cfg: Config) -> list[tuple[str, Path]]:
    return [(i.get("name") or Path(i["path"]).name, Path(i["path"])) for i in cfg.inboxes]


def inboxes_text(cfg: Config) -> str:
    return "\n".join(f"{i.get('name') or Path(i['path']).name}={i['path']}" for i in cfg.inboxes)
