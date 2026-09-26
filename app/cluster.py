"""Clustering rules (all deterministic except the home-burst judgment):

  zone per photo      home < home_radius < local < local_radius < away (local/away is a label only)
  no GPS              take the nearest photo in time that has GPS
  excursion           maximal run of photos not at home; ends at any home photo; GPS-less photos
                      inside the run ride along. A photo-less gap splits the run only if it is
                      long AND (the two sides are in different areas OR one side is near home).
    trip              the run spans >= trip_min_hours (an overnight stay)  -> "YYYY-MM-DD..DD Places"
    day out           shorter, with >= dayout_min_photos photos           -> "YYYY-MM-DD Place"
  home burst          photos at home closer than burst_gap_hours, above baseline -> Kev: occasion / busy_day
  everyday            everything else -> YYYY/MM

Output: proposals, persisted in <data>/proposals.json with their review status.
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import geo, ingest
from .config import DATA_DIR, Config, inbox_dirs, tzinfo
from .kev import Decider

PROPOSALS_PATH = DATA_DIR / "proposals.json"
UNCERTAIN_BELOW = 0.7


# --- persistence ----------------------------------------------------------------

def load_proposals() -> dict[str, dict]:
    if PROPOSALS_PATH.exists():
        try:
            return json.loads(PROPOSALS_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_proposals(props: dict[str, dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROPOSALS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(props, ensure_ascii=False, indent=1))
    tmp.replace(PROPOSALS_PATH)


# --- records -------------------------------------------------------------------

def _dt(s: str, tz=None) -> datetime:
    """Sidecar timestamp -> aware datetime. Naive values are local wall time in the home zone."""
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=tz or timezone.utc)


def load_records(cfg: Config) -> tuple[list[dict], list[dict]]:
    """Sidecars from all inboxes, merged into one timeline. Returns (records, skipped_without_ts).
    Originals already copied into the sorted tree (sidecar `copied_to`, copy mode) are left out."""
    recs, skipped = [], []
    tz = tzinfo(cfg)
    for name, folder in inbox_dirs(cfg):
        if not folder.exists():
            continue
        for p in ingest.list_photos(cfg, folder):
            rec = ingest.read_sidecar(p, cfg)
            if rec is None:
                continue
            if rec.get("copied_to"):          # copy mode: the original was already sorted
                continue
            rec = dict(rec)
            rec["path"] = str(p)
            rec.setdefault("source", name)
            rec.setdefault("inbox", str(folder))
            if not rec.get("ts"):
                skipped.append(rec)
                continue
            rec["_t"] = _dt(rec["ts"], tz)
            recs.append(rec)
    recs.sort(key=lambda r: r["_t"])
    return recs, skipped


def fill_gps_from_neighbours(cfg: Config, recs: list[dict], max_hours: float = 48) -> None:
    """Photos without GPS inherit position/zone from the closest GPS'd photo in time."""
    with_gps = [i for i, r in enumerate(recs) if r.get("lat") is not None]
    if not with_gps:
        return
    import bisect
    times = [recs[i]["_t"] for i in with_gps]
    for r in recs:
        if r.get("lat") is not None:
            continue
        k = bisect.bisect_left(times, r["_t"])
        cands = [with_gps[j] for j in (k - 1, k) if 0 <= j < len(with_gps)]
        best = min(cands, key=lambda j: abs((recs[j]["_t"] - r["_t"]).total_seconds()))
        if abs((recs[best]["_t"] - r["_t"]).total_seconds()) > max_hours * 3600:
            continue
        src = recs[best]
        r.update({"lat": src["lat"], "lon": src["lon"], "gps_source": f"neighbour:{src['file']}"})
        ingest.enrich_location(cfg, r)


# --- excursions (trips and day outs) ---------------------------------------------

def find_excursions(cfg: Config, recs: list[dict]) -> list[list[dict]]:
    """Maximal runs of photos away from home (local or away zone), ended by any photo at home.
    GPS-less photos ride along inside a run but never start or end one."""
    runs, cur = [], []
    for r in recs:
        z = r["zone"]
        if z == geo.ZONE_HOME:
            if cur:
                runs.append(cur)
            cur = []
        elif z in (geo.ZONE_AWAY, geo.ZONE_LOCAL) or cur:
            cur.append(r)
    if cur:
        runs.append(cur)
    # drop leading/trailing GPS-less photos, then split on long gaps between different areas
    out = []
    for run in runs:
        while run and run[0]["zone"] == geo.ZONE_UNKNOWN:
            run.pop(0)
        while run and run[-1]["zone"] == geo.ZONE_UNKNOWN:
            run.pop()
        if not run:
            continue
        part = [run[0]]
        for prev, nxt in zip(run, run[1:], strict=False):
            gap_days = (nxt["_t"] - prev["_t"]).total_seconds() / 86400
            far = False
            if prev.get("lat") is not None and nxt.get("lat") is not None:     # GPS-less photos never split
                far = geo.haversine_km(prev["lat"], prev["lon"], nxt["lat"], nxt["lon"]) > cfg.trip_split_distance_km
            near_home = geo.ZONE_LOCAL in (prev["zone"], nxt["zone"])   # you sleep at home: no 2-week trip 10 km away
            if gap_days > cfg.trip_gap_days and (far or near_home):
                out.append(part)
                part = []
            part.append(nxt)
        out.append(part)
    return out


def excursion_kind(cfg: Config, run: list[dict]) -> str | None:
    """'trip' (spans >= trip_min_hours, enough located photos), 'local' (a day out with enough
    photos) or None (too small: everyday)."""
    a, b = _span(run)
    located = sum(1 for r in run if r["zone"] != geo.ZONE_UNKNOWN)
    if (b - a) >= timedelta(hours=cfg.trip_min_hours):
        return "trip" if located >= cfg.trip_min_photos else None
    return "local" if len(run) >= cfg.dayout_min_photos else None


def _span(recs: list[dict]) -> tuple[datetime, datetime]:
    return recs[0]["_t"], recs[-1]["_t"]


def span_label(a: datetime, b: datetime) -> str:
    if a.date() == b.date():
        return a.strftime("%Y-%m-%d")
    if a.year == b.year and a.month == b.month:
        return f"{a:%Y-%m-%d}..{b:%d}"
    if a.year == b.year:
        return f"{a:%Y-%m-%d}..{b:%m-%d}"
    return f"{a:%Y-%m-%d}..{b:%Y-%m-%d}"


def places_label(cfg: Config, recs: list[dict]) -> str:
    """Places in order of first appearance, minus ones that only a couple of photos mention
    (a photo at a motorway stop must not name the trip)."""
    def ordered(key):
        counts = Counter((r.get("place") or {}).get(key) for r in recs if r["zone"] != geo.ZONE_HOME)
        counts.pop(None, None)
        counts.pop("", None)
        keep = {v for v, n in counts.items() if (n >= 2 or len(recs) < 20) and n >= 0.03 * len(recs)} or set(counts)
        seen, out = set(), []
        for r in recs:
            v = (r.get("place") or {}).get(key)
            if v in keep and v not in seen:
                seen.add(v)
                out.append(v)
        return out
    cities = ordered("place")
    if 0 < len(cities) <= cfg.max_places_in_name:
        return ", ".join(cities)
    countries = ordered("country")
    if 0 < len(countries) <= cfg.max_places_in_name:
        return ", ".join(countries)
    return "Multiple" if countries else "Unknown"


def sanitize(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "-", name).strip(" .")


def _gps_conf(r: dict) -> float:
    """How sure we are the photo belongs where its position says: own GPS, a neighbour's, none."""
    src = r.get("gps_source") or ""
    return 1.0 if src == "exif" else 0.8 if src.startswith("neighbour") else 0.6


def _photo_entry(r: dict, conf: float) -> dict:
    return {"path": r["path"], "file": r["file"], "ts": r["ts"], "zone": r["zone"],
            "lat": r.get("lat"), "lon": r.get("lon"),
            "media": r.get("media") or ("video" if ingest.is_video(Path(r["path"])) else "photo"),
            "source": r.get("source"), "inbox": r.get("inbox"),
            "place": (r.get("place") or {}).get("place"), "gps_source": r.get("gps_source"),
            "conf": round(conf, 2), "uncertain": conf < UNCERTAIN_BELOW}


def _pid(kind: str, recs: list[dict]) -> str:
    h = hashlib.sha1(f"{kind}|{recs[0]['file']}|{recs[0]['ts']}|{recs[-1]['file']}".encode()).hexdigest()
    return f"{kind[:1]}{h[:10]}"


def trip_proposal(cfg: Config, run: list[dict], now: datetime) -> dict:
    a, b = _span(run)
    photos = [_photo_entry(r, _gps_conf(r)) for r in run]
    ongoing = (now - b) < timedelta(days=cfg.trip_gap_days)     # the home photo may not have synced yet
    return {
        "id": _pid("trip", run), "kind": "trip",
        "name": sanitize(f"{span_label(a, b)} {places_label(cfg, run)}"),
        "start": a.isoformat(), "end": b.isoformat(), "photos": photos,
        "n": len(photos), "n_uncertain": sum(p["uncertain"] for p in photos),
        "status": "ongoing" if ongoing else "pending",
        "decision": {"by": "rule", "conf": 1.0,
                     "note": f"{(b - a).total_seconds() / 3600:.0f} h away from home, ended by a home photo"},
    }


# --- bursts --------------------------------------------------------------------

def group_bursts(cfg: Config, recs: list[dict]) -> list[list[dict]]:
    bursts, cur = [], []
    for r in recs:
        if cur and (r["_t"] - cur[-1]["_t"]).total_seconds() > cfg.burst_gap_hours * 3600:
            bursts.append(cur)
            cur = []
        cur.append(r)
    if cur:
        bursts.append(cur)
    return bursts


def home_baseline(recs: list[dict]) -> float:
    per_day = Counter(r["_t"].date() for r in recs if r["zone"] == geo.ZONE_HOME)
    return statistics.median(per_day.values()) if per_day else 1.0


def local_proposal(cfg: Config, run: list[dict], now: datetime | None = None) -> dict:
    a, b = _span(run)
    place = Counter((r.get("place") or {}).get("place") for r in run if r["zone"] != geo.ZONE_HOME and r.get("place"))
    name = place.most_common(1)[0][0] if place else "Ausflug"
    photos = [_photo_entry(r, _gps_conf(r)) for r in run]
    ongoing = now is not None and (now - b) < timedelta(days=1)
    return {"id": _pid("local", run), "kind": "local", "name": sanitize(f"{span_label(a, b)} {name}"),
            "start": a.isoformat(), "end": b.isoformat(), "photos": photos, "n": len(photos),
            "n_uncertain": sum(p["uncertain"] for p in photos), "status": "ongoing" if ongoing else "pending",
            "decision": {"by": "rule", "conf": 1.0,
                         "note": f"{(b - a).total_seconds() / 3600:.1f} h away from home, {len(run)} photos"}}


# The one question the decision model is asked today (see docs/DESIGN.md, section 2).
HOME_BURST_OPTIONS = ["occasion", "busy_day"]
HOME_BURST_INSTRUCTIONS = ("A dense burst of photos was taken at home; below is its metadata summary "
                           "(no image content). Is this a special occasion worth its own album, or just "
                           "an ordinary day with many photos?")
HOME_BURST_CRITERIA = {
    "occasion": "a special event at home: birthday, party, visitors, celebration, family gathering; "
                "typically several people photographing, many photos within a few hours",
    "busy_day": "an ordinary day with unusually many photos: documenting things, kids playing, "
                "cooking, repairs, screenshots; nothing that deserves its own album",
}


def count_devices(recs: list[dict]) -> int:
    """Distinct devices. A record whose device name is only the inbox name (no metadata, e.g. an
    Android video) counts as the inbox's metadata-named device when that inbox has exactly one."""
    named: dict[str | None, set[str]] = {}
    for r in recs:
        if r.get("camera") and r.get("camera_source") != "inbox":
            named.setdefault(r.get("source"), set()).add(r["camera"])
    keys = set()
    for r in recs:
        cam, src = r.get("camera"), r.get("source")
        if cam and r.get("camera_source") != "inbox":
            keys.add(("cam", cam))
        elif len(named.get(src, ())) == 1:
            keys.add(("cam", next(iter(named[src]))))
        else:
            keys.add(("inbox", src))
    return len(keys)


def media_label(recs: list[dict]) -> str:
    videos = sum(1 for r in recs if r.get("media") == "video")
    photos = len(recs) - videos
    parts = [f"{photos} Fotos"] if photos or not videos else []
    if videos:
        parts.append(f"{videos} Videos")
    return ", ".join(parts)


def home_state(cfg: Config, burst: list[dict], threshold: float) -> dict:
    a, b = _span(burst)
    return {
        "photos": len(burst),
        "videos": sum(1 for r in burst if r.get("media") == "video"),
        "duration_h": round((b - a).total_seconds() / 3600, 1),
        "weekday": a.strftime("%A"),
        "start_hour": a.hour, "end_hour": b.hour,
        "devices": count_devices(burst),
        "burst_ratio": round(len(burst) / threshold, 2),
        "date": a.strftime("%Y-%m-%d"),
    }


def home_proposal(cfg: Config, burst: list[dict], decision: dict) -> dict:
    a, b = _span(burst)
    photos = [_photo_entry(r, 1.0) for r in burst]
    return {"id": _pid("home", burst), "kind": "home",
            "name": sanitize(f"{span_label(a, b)} ({media_label(burst)})"),
            "start": a.isoformat(), "end": b.isoformat(), "photos": photos, "n": len(photos),
            "n_uncertain": 0, "status": "pending",
            "decision": {"by": decision["by"], "conf": round(decision["conf"], 3), "id": decision["id"],
                         "answer": decision["answer"], "note": "home burst judged by " + decision["by"]}}


# --- manual clusters (Everyday page) ---------------------------------------------

def _span_fields(pr: dict) -> None:
    ts = sorted(p["ts"] for p in pr["photos"])
    pr["start"], pr["end"], pr["n"] = ts[0], ts[-1], len(pr["photos"])
    pr["n_uncertain"] = sum(p.get("uncertain", False) for p in pr["photos"])


def create_manual(kind: str, name: str, recs: list[dict]) -> dict:
    """A proposal the user assembled by hand. `paths` is the source of truth across runs."""
    if kind not in ("trip", "local", "home") or not recs:
        raise ValueError("kind must be trip/local/home and at least one photo is needed")
    recs = sorted(recs, key=lambda r: r["_t"])
    a, b = _span(recs)
    if kind == "home":
        where = f"({media_label(recs)})"
    else:                                             # most common place, home photos included
        places = Counter((r.get("place") or {}).get("place") for r in recs if r.get("place"))
        where = places.most_common(1)[0][0] if places else "Unknown"
    default = f"{span_label(a, b)} {where}"
    pr = {"id": "m" + uuid.uuid4().hex[:10], "kind": kind, "manual": True,
          "name": sanitize(name.strip() or default), "name_edited": bool(name.strip()),
          "paths": [r["path"] for r in recs], "photos": [_photo_entry(r, 1.0) for r in recs],
          "status": "pending", "excluded": [],
          "decision": {"by": "user", "conf": 1.0, "note": "assembled by hand"}}
    _span_fields(pr)
    return pr


def add_to_proposal(pr: dict, recs: list[dict]) -> int:
    """Attach photos to an existing proposal; remembered in `added` (or `paths` for manual ones)
    so the next automatic run keeps them. Returns how many were new."""
    have = {p["path"] for p in pr["photos"]}
    new = [r for r in recs if r["path"] not in have]
    for r in new:
        pr["photos"].append(_photo_entry(r, 1.0))
    key = "paths" if pr.get("manual") else "added"
    pr[key] = sorted(set(pr.get(key, [])) | {r["path"] for r in new})
    pr["photos"].sort(key=lambda p: p["ts"])
    _span_fields(pr)
    return len(new)


def everyday_records(cfg: Config, props: dict | None = None) -> list[dict]:
    """Records in no live proposal (pending/ongoing/approved/applied): what the Everyday page shows."""
    props = load_proposals() if props is None else props
    taken = {p["path"] for pr in props.values() if pr["status"] in ("pending", "ongoing", "approved", "applied")
             for p in pr["photos"]}
    recs, _ = load_records(cfg)
    fill_gps_from_neighbours(cfg, recs)
    return [r for r in recs if r["path"] not in taken]


# --- driver --------------------------------------------------------------------

def run(cfg: Config, decider: Decider | None = None) -> dict:
    """Recompute proposals from the inbox, keeping the status of ones already reviewed."""
    decider = decider or Decider(cfg)
    now = datetime.now(timezone.utc)
    old = load_proposals()
    recs, skipped = load_records(cfg)
    fill_gps_from_neighbours(cfg, recs)

    # photos the user rejected from a cluster stay out of clustering (they become everyday)
    rejected = {p["path"] for pr in old.values() if pr["status"] == "rejected" for p in pr["photos"]}
    recs = [r for r in recs if r["path"] not in rejected]
    by_path = {r["path"]: r for r in recs}

    new: dict[str, dict] = {}
    taken: set[str] = set()

    # photos the user placed by hand (manual clusters, additions) are never re-clustered
    live = ("pending", "ongoing", "approved")
    manual = {pid: pr for pid, pr in old.items() if pr.get("manual") and pr["status"] in live}
    additions = {pid: [p for p in pr.get("added", []) if p in by_path]
                 for pid, pr in old.items() if pr.get("added") and pr["status"] in live}
    for pid, pr in manual.items():
        present = [by_path[p] for p in pr["paths"] if p in by_path]
        if not present:
            continue                                    # all photos gone (moved elsewhere): drop it
        pr = dict(pr, photos=[_photo_entry(r, 1.0) for r in present], paths=[r["path"] for r in present])
        _span_fields(pr)
        new[pid] = pr
        taken.update(pr["paths"])
    for paths in additions.values():
        taken.update(paths)
    auto_recs = [r for r in recs if r["path"] not in taken]

    for run_ in find_excursions(cfg, auto_recs):
        kind = excursion_kind(cfg, run_)
        if kind is None:
            continue
        pr = trip_proposal(cfg, run_, now) if kind == "trip" else local_proposal(cfg, run_, now)
        new[pr["id"]] = pr
        taken.update(p["path"] for p in pr["photos"])

    rest = [r for r in auto_recs if r["path"] not in taken]
    baseline = home_baseline(recs)
    threshold = max(cfg.burst_min_photos, baseline * cfg.burst_baseline_factor)
    for burst in group_bursts(cfg, rest):
        zones = Counter(r["zone"] for r in burst)
        major = zones.most_common(1)[0][0]
        if len(burst) < cfg.burst_min_photos:
            continue
        if major == geo.ZONE_HOME and len(burst) >= threshold:
            pid = _pid("home", burst)
            prev = old.get(pid)
            if prev and prev.get("decision", {}).get("id"):
                decision = {"id": prev["decision"]["id"], "by": prev["decision"]["by"],
                            "conf": prev["decision"]["conf"], "answer": prev["decision"]["answer"]}
            else:
                decision = decider.choice("home_burst", home_state(cfg, burst, threshold),
                                          HOME_BURST_OPTIONS, HOME_BURST_INSTRUCTIONS, HOME_BURST_CRITERIA)
            if decision["answer"] != "occasion" or decision["conf"] < cfg.occasion_confidence_min:
                continue
            pr = home_proposal(cfg, burst, decision)
        else:
            continue
        new[pr["id"]] = pr
        taken.update(p["path"] for p in pr["photos"])

    # photos added by hand to automatic proposals
    for pid, paths in additions.items():
        if pid in new and not new[pid].get("manual"):
            add_to_proposal(new[pid], [by_path[p] for p in paths])
    # carry over review state: status, an edited name, and toggled-out photos
    for pid, pr in new.items():
        prev = old.get(pid)
        if pr.get("manual"):
            continue                                    # already carries its own state
        if prev and prev["status"] in ("approved", "rejected", "applied"):
            pr["status"] = prev["status"]
            pr["name"] = prev.get("name", pr["name"])
        elif prev and prev.get("name_edited"):
            pr["name"], pr["name_edited"] = prev["name"], True
        have = {p["path"] for p in pr["photos"]}
        pr["excluded"] = sorted(p for p in (prev or {}).get("excluded", []) if p in have)
    # keep applied/rejected proposals whose photos are gone from the inbox (history)
    for pid, pr in old.items():
        if pid not in new and pr["status"] in ("applied", "rejected"):
            new[pid] = pr
    save_proposals(new)

    everyday = [r for r in recs if r["path"] not in taken]
    return {"proposals": len([p for p in new.values() if p["status"] in ("pending", "ongoing")]),
            "photos": len(recs), "everyday": len(everyday), "no_timestamp": len(skipped),
            "baseline": baseline, "threshold": threshold}
