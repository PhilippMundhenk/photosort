"""Clustering rules (all deterministic except the home-burst judgment):

  zone per photo      home < home_radius < local < local_radius < away
  no GPS              take the nearest photo in time that has GPS
  trip                maximal run of away photos; ends at any home photo; local/unknown photos
                      inside the run ride along (transit). A photo-less gap splits the run only
                      if it is long AND the two sides are in different areas.
  local burst         photos closer than burst_gap_hours, majority zone local -> "YYYY-MM-DD Place"
  home burst          same grouping, majority home, above baseline -> Kev: occasion / busy_day
  everyday            everything else -> YYYY/MM

Output: proposals, persisted in <data>/proposals.json with their review status.
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import geo, ingest
from .config import DATA_DIR, Config, inbox_dirs
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

def _dt(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def load_records(cfg: Config) -> tuple[list[dict], list[dict]]:
    """Sidecars from all inboxes, merged into one timeline. Returns (records, skipped_without_ts)."""
    recs, skipped = [], []
    for name, folder in inbox_dirs(cfg):
        if not folder.exists():
            continue
        for p in ingest.list_photos(cfg, folder):
            rec = ingest.read_sidecar(p)
            if rec is None:
                continue
            rec = dict(rec)
            rec["path"] = str(p)
            rec.setdefault("source", name)
            rec.setdefault("inbox", str(folder))
            if not rec.get("ts"):
                skipped.append(rec)
                continue
            rec["_t"] = _dt(rec["ts"])
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
    for i, r in enumerate(recs):
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


# --- trips ---------------------------------------------------------------------

def find_trip_runs(cfg: Config, recs: list[dict]) -> list[list[dict]]:
    runs, cur = [], []
    for r in recs:
        z = r["zone"]
        if z == geo.ZONE_HOME:
            if cur:
                runs.append(cur)
            cur = []
        elif z == geo.ZONE_AWAY:
            cur.append(r)
        elif cur:                       # local/unknown inside an open run: transit
            cur.append(r)
    if cur:
        runs.append(cur)
    # drop leading/trailing non-away photos, then split on long gaps between different areas
    out = []
    for run in runs:
        while run and run[0]["zone"] != geo.ZONE_AWAY:
            run.pop(0)
        while run and run[-1]["zone"] != geo.ZONE_AWAY:
            run.pop()
        if not run:
            continue
        part = [run[0]]
        for prev, nxt in zip(run, run[1:]):
            gap_days = (nxt["_t"] - prev["_t"]).total_seconds() / 86400
            far = geo.haversine_km(prev["lat"], prev["lon"], nxt["lat"], nxt["lon"]) > cfg.trip_split_distance_km
            if gap_days > cfg.trip_gap_days and far:
                out.append(part)
                part = []
            part.append(nxt)
        out.append(part)
    return [r for r in out if sum(1 for x in r if x["zone"] == geo.ZONE_AWAY) >= cfg.trip_min_photos]


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
    def ordered(key):
        seen, out = set(), []
        for r in recs:
            if r["zone"] != geo.ZONE_AWAY or not r.get("place"):
                continue
            v = r["place"].get(key)
            if v and v not in seen:
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


def _photo_entry(r: dict, conf: float) -> dict:
    return {"path": r["path"], "file": r["file"], "ts": r["ts"], "zone": r["zone"],
            "source": r.get("source"), "inbox": r.get("inbox"),
            "place": (r.get("place") or {}).get("place"), "gps_source": r.get("gps_source"),
            "conf": round(conf, 2), "uncertain": conf < UNCERTAIN_BELOW}


def _pid(kind: str, recs: list[dict]) -> str:
    h = hashlib.sha1(f"{kind}|{recs[0]['file']}|{recs[0]['ts']}|{recs[-1]['file']}".encode()).hexdigest()
    return f"{kind[:1]}{h[:10]}"


def trip_proposal(cfg: Config, run: list[dict], now: datetime) -> dict:
    a, b = _span(run)
    photos = []
    for r in run:
        if r["zone"] == geo.ZONE_AWAY:
            conf = 1.0 if (r.get("gps_source") == "exif") else 0.8
        else:
            conf = 0.6                       # transit / no-GPS photo riding along
        photos.append(_photo_entry(r, conf))
    ongoing = (now - b) < timedelta(days=cfg.trip_gap_days)
    return {
        "id": _pid("trip", run), "kind": "trip",
        "name": sanitize(f"{span_label(a, b)} {places_label(cfg, run)}"),
        "start": a.isoformat(), "end": b.isoformat(), "photos": photos,
        "n": len(photos), "n_uncertain": sum(p["uncertain"] for p in photos),
        "status": "ongoing" if ongoing else "pending",
        "decision": {"by": "rule", "conf": 1.0, "note": "away-run terminated by home photo"},
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


def local_proposal(cfg: Config, burst: list[dict]) -> dict:
    a, b = _span(burst)
    place = Counter((r.get("place") or {}).get("place") for r in burst if r["zone"] == geo.ZONE_LOCAL and r.get("place"))
    name = place.most_common(1)[0][0] if place else "Ausflug"
    photos = [_photo_entry(r, 1.0 if r["zone"] == geo.ZONE_LOCAL else 0.6) for r in burst]
    return {"id": _pid("local", burst), "kind": "local", "name": sanitize(f"{span_label(a, b)} {name}"),
            "start": a.isoformat(), "end": b.isoformat(), "photos": photos, "n": len(photos),
            "n_uncertain": sum(p["uncertain"] for p in photos), "status": "pending",
            "decision": {"by": "rule", "conf": 1.0, "note": f"local burst, {len(burst)} photos"}}


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


def home_state(cfg: Config, burst: list[dict], threshold: float) -> dict:
    a, b = _span(burst)
    return {
        "photos": len(burst),
        "duration_h": round((b - a).total_seconds() / 3600, 1),
        "weekday": a.strftime("%A"),
        "start_hour": a.hour, "end_hour": b.hour,
        "devices": len({r.get("camera") for r in burst}),
        "burst_ratio": round(len(burst) / threshold, 2),
        "date": a.strftime("%Y-%m-%d"),
    }


def home_proposal(cfg: Config, burst: list[dict], decision: dict) -> dict:
    a, b = _span(burst)
    photos = [_photo_entry(r, 1.0) for r in burst]
    return {"id": _pid("home", burst), "kind": "home",
            "name": sanitize(f"{span_label(a, b)} ({len(burst)} Fotos)"),
            "start": a.isoformat(), "end": b.isoformat(), "photos": photos, "n": len(photos),
            "n_uncertain": 0, "status": "pending",
            "decision": {"by": decision["by"], "conf": round(decision["conf"], 3), "id": decision["id"],
                         "answer": decision["answer"], "note": "home burst judged by " + decision["by"]}}


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

    new: dict[str, dict] = {}
    taken: set[str] = set()

    for run_ in find_trip_runs(cfg, recs):
        pr = trip_proposal(cfg, run_, now)
        new[pr["id"]] = pr
        taken.update(p["path"] for p in pr["photos"])

    rest = [r for r in recs if r["path"] not in taken]
    baseline = home_baseline(recs)
    threshold = max(cfg.burst_min_photos, baseline * cfg.burst_baseline_factor)
    for burst in group_bursts(cfg, rest):
        zones = Counter(r["zone"] for r in burst)
        major = zones.most_common(1)[0][0]
        if len(burst) < cfg.burst_min_photos:
            continue
        if major == geo.ZONE_LOCAL:
            pr = local_proposal(cfg, burst)
        elif major == geo.ZONE_HOME and len(burst) >= threshold:
            pid = _pid("home", burst)
            prev = old.get(pid)
            if prev and prev.get("decision", {}).get("id"):
                decision = {"id": prev["decision"]["id"], "by": prev["decision"]["by"],
                            "conf": prev["decision"]["conf"], "answer": prev["decision"]["answer"]}
            else:
                decision = decider.choice("home_burst", home_state(cfg, burst, threshold), HOME_BURST_OPTIONS,
                                          HOME_BURST_INSTRUCTIONS, HOME_BURST_CRITERIA)
            if decision["answer"] != "occasion" or decision["conf"] < cfg.occasion_confidence_min:
                continue
            pr = home_proposal(cfg, burst, decision)
        else:
            continue
        new[pr["id"]] = pr
        taken.update(p["path"] for p in pr["photos"])

    # carry over review state
    for pid, pr in new.items():
        prev = old.get(pid)
        if prev and prev["status"] in ("approved", "rejected", "applied"):
            pr["status"] = prev["status"]
            pr["name"] = prev.get("name", pr["name"])
            pr["excluded"] = prev.get("excluded", [])
        elif prev and prev.get("name_edited"):
            pr["name"], pr["name_edited"] = prev["name"], True
            pr["excluded"] = prev.get("excluded", [])
        else:
            pr.setdefault("excluded", [])
    # keep applied/rejected proposals whose photos are gone from the inbox (history)
    for pid, pr in old.items():
        if pid not in new and pr["status"] in ("applied", "rejected"):
            new[pid] = pr
    save_proposals(new)

    everyday = [r for r in recs if r["path"] not in taken]
    return {"proposals": len([p for p in new.values() if p["status"] in ("pending", "ongoing")]),
            "photos": len(recs), "everyday": len(everyday), "no_timestamp": len(skipped),
            "baseline": baseline, "threshold": threshold}
