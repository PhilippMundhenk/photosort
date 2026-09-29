"""Clustering rules (all deterministic except the home-burst judgment):

  zone per photo      home < home_radius < local < local_radius < away (local/away is a label only)
  no GPS              take the nearest photo in time that has GPS
  excursion           maximal run of photos not at home; ends at any home photo; GPS-less photos
                      inside the run ride along. A photo-less gap splits the run if it is long AND
                      the two sides are in different areas, or if it exceeds local_gap_hours while
                      either side is near home (you sleep at home, so a night ends the outing).
    trip              the run spans >= trip_min_hours (an overnight stay)  -> "YYYY-MM Places"
                      (or "YYYY-MM-DD..DD" with name_multiday_by_month off; across months always)
    day out           shorter; >= dayout_min_photos photos near home, >= trip_min_photos
                      when mostly far away                                -> "YYYY-MM-DD Place"
  home burst          photos at home closer than burst_gap_hours, well above your usual photos/day
                      -> proposal in _unnamed/ for you to name or reject
  everyday            everything else -> YYYY/MM

Output: proposals, persisted in <data>/proposals.json with their review status.
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import geo, ingest, rules
from .config import DATA_DIR, Config, inbox_dirs, tzinfo

PROPOSALS_PATH = DATA_DIR / "proposals.json"
UNCERTAIN_BELOW = 0.7


# --- persistence ----------------------------------------------------------------

_save_lock = threading.Lock()
_props_cache: dict = {"stamp": None, "text": "", "counts": None, "parsed": None}
# Every read-modify-write of the proposals: the pages' actions and the run's recompute. Without
# it a click saved while the run was between reading and writing the file was overwritten.
proposals_lock = threading.RLock()


def _props_text() -> str:
    """The proposals file, read from disk only when it changed (every open tab asks for the
    status every few seconds; a megabyte of JSON must not be re-read for each of them)."""
    try:
        st = PROPOSALS_PATH.stat()
    except OSError:
        with _save_lock:                  # the file is gone (deleted to start over): so is what it said
            _props_cache.update(stamp=None, text="", counts=None, parsed=None)
        return ""
    stamp = (st.st_mtime_ns, st.st_size)
    with _save_lock:
        if _props_cache["stamp"] != stamp:
            try:
                text = PROPOSALS_PATH.read_text(encoding="utf-8")
            except OSError:
                return ""
            _props_cache.update(stamp=stamp, text=text, counts=None, parsed=None)
        return _props_cache["text"]


def load_proposals() -> dict[str, dict]:
    """The proposals, parsed once per version of the file. Every call gets its own dict, its own
    proposal dicts and its own lists (status, name, excluded, photos list can be edited and saved
    back); the photo entries inside the lists are shared and must be treated as read-only. Parsing
    five megabytes per click was most of the cost of a click on a large library."""
    text = _props_text()
    if not text:
        return {}
    with _save_lock:
        parsed = _props_cache["parsed"]
        if parsed is None:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return {}
            _props_cache["parsed"] = parsed
    return _copy_proposals(parsed)


def _copy_proposals(parsed: dict) -> dict[str, dict]:
    return {pid: {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                  for k, v in pr.items()} for pid, pr in parsed.items()}


def status_counts() -> dict:
    """What the status poll needs, computed once per version of the file: pending, ongoing and
    approved proposals, with the approved ones' error text (failed moves)."""
    _props_text()
    with _save_lock:
        if _props_cache["counts"] is None:
            props: dict = _props_cache["parsed"] or {}
            if not props and _props_cache["text"]:
                try:
                    props = json.loads(_props_cache["text"])
                    _props_cache["parsed"] = props
                except json.JSONDecodeError:
                    props = {}
            _props_cache["counts"] = {
                "pending": sum(1 for p in props.values() if p["status"] in ("pending", "ongoing")),
                "approved": {pid: p.get("error") for pid, p in props.items() if p["status"] == "approved"},
            }
        c = _props_cache["counts"]
        return {"pending": c["pending"], "approved": dict(c["approved"])}


def save_proposals(props: dict[str, dict]) -> None:
    """Atomic replace; serialized, with a per-writer temp file (the apply worker and a request
    may save at the same moment, and Windows refuses to replace a file another thread holds)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(props, ensure_ascii=False, separators=(",", ":"))   # compact: read by machines only
    with _save_lock:
        tmp = PROPOSALS_PATH.with_name(f"proposals.{threading.get_ident()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(PROPOSALS_PATH)
        try:
            st = PROPOSALS_PATH.stat()
            _props_cache.update(stamp=(st.st_mtime_ns, st.st_size), text=text, counts=None,
                                parsed=_copy_proposals(props))
        except OSError:
            _props_cache.update(stamp=None, text="", counts=None, parsed=None)


# --- records -------------------------------------------------------------------

def _dt(s: str, tz=None) -> datetime:
    """Sidecar timestamp -> aware datetime. Naive values are local wall time in the home zone."""
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=tz or timezone.utc)


_records_cache: dict = {"key": None, "recs": None, "skipped": None, "filled": None, "built": 0.0}
busy = False                    # set by the pipeline while it scans: page requests then accept a
REFRESH_WHILE_BUSY_S = 5.0      # cache that is up to this many seconds old instead of re-patching


def _records_key(cfg: Config) -> tuple:
    return (json.dumps(cfg.inboxes, sort_keys=True), cfg.sidecar_mode, cfg.sidecar_name,
            cfg.timezone, cfg.home_lat, cfg.home_lon, cfg.home_radius_km, cfg.local_radius_km,
            tuple(cfg.photo_extensions))


def _record_for(cfg: Config, path: str) -> dict | None:
    """One record as load_records would build it, or None if it is gone or not sortable."""
    p = Path(path)
    hit = next(((n, f) for n, f in inbox_dirs(cfg) if p.is_relative_to(f)), None)
    if hit is None:                           # a record in the sorted tree: never part of the timeline
        return None                           # (and no stat on the share to find that out)
    if not p.exists():
        return None
    name, folder = hit
    rec = ingest.read_sidecar(p, cfg)
    if rec is None or rec.get("copied_to"):
        return None
    rec = dict(rec)
    rec["path"] = path
    rec.setdefault("source", name)
    rec.setdefault("inbox", str(folder))
    if rec.get("ts"):
        rec["_t"] = _dt(rec["ts"], tzinfo(cfg))
    return rec


def load_records(cfg: Config) -> tuple[list[dict], list[dict]]:
    """Sidecars from all inboxes, merged into one timeline. Returns (records, skipped_without_ts).
    Originals already copied into the sorted tree (sidecar `copied_to`, copy mode) are left out.

    Cached: a full read only when the config changed or ingest says everything changed; a
    changed record (ingest.changed paths: written, moved, deleted, vanished) is re-read on its
    own. Callers get copies."""
    with _records_lock:                       # one thread rebuilds or patches; the others wait for it
        key = _records_key(cfg)
        ch = ingest.changed
        stale_ok = (busy and _records_cache["key"] == key and not ch["all"]
                    and time.time() - _records_cache["built"] < REFRESH_WHILE_BUSY_S)
        if _records_cache["key"] != key or ch["all"]:
            ch["all"], ch["paths"], ch["gone"] = False, set(), set()   # cleared first: a record written
            recs, skipped = _load_records(cfg)      # while the load runs stays noted, patched in next time
            _records_cache.update(key=key, recs=recs, skipped=skipped, filled=None, built=time.time())
        elif ch["gone"] and not ch["paths"]:
            # photos moved away (a cluster applied, everyday photos moved): one pass over the cache,
            # nothing read from the share; the GPS fill of the others stays valid
            gone = set(ch["gone"])
            ch["gone"] = set()
            for k in ("recs", "skipped", "filled"):
                if _records_cache[k] is not None:
                    _records_cache[k] = [r for r in _records_cache[k] if r["path"] not in gone]
            _records_cache["built"] = time.time()
        elif ch["paths"] and not stale_ok:
            paths = set(ch["paths"]) | set(ch["gone"])
            ch["paths"], ch["gone"] = set(), set()
            recs = [r for r in _records_cache["recs"] if r["path"] not in paths]
            skipped = [r for r in _records_cache["skipped"] if r["path"] not in paths]
            for path in paths:
                rec = _record_for(cfg, path)
                if rec is None:
                    continue
                (recs if rec.get("ts") else skipped).append(rec)
            recs.sort(key=lambda r: (r["_t"], r["path"]))   # same second: by path, so order and ids never flip
            borrow_video_offsets(recs)
            _records_cache.update(recs=recs, skipped=skipped, filled=None, built=time.time())
        return [dict(r) for r in _records_cache["recs"]], [dict(r) for r in _records_cache["skipped"]]


_records_lock = threading.RLock()


def records_filled(cfg: Config) -> list[dict]:
    """load_records plus the neighbour GPS fill, cached the same way. The fill runs in one
    thread at a time: with several tabs polling during a scan, every request used to fill its
    own copy of twenty thousand records, and the server looked dead."""
    with _records_lock:                   # load and fill in one go, from the cache itself: a thread that
        load_records(cfg)                 # loaded its copy a moment ago (the startup warm-up) must not
        if _records_cache["filled"] is None:      # fill and cache a list a newer record is missing from
            recs = [dict(r) for r in _records_cache["recs"]]
            fill_gps_from_neighbours(cfg, recs)
            _records_cache["filled"] = recs
        return [dict(r) for r in _records_cache["filled"]]


def _load_records(cfg: Config) -> tuple[list[dict], list[dict]]:
    recs, skipped = [], []
    tz = tzinfo(cfg)
    for name, folder in inbox_dirs(cfg):
        if not folder.exists():
            continue
        photos = ingest.list_photos(cfg, folder)
        ingest.known_files(folder, photos)        # the next scan diffs against this instead of reloading
        for p in photos:
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
    recs.sort(key=lambda r: (r["_t"], r["path"]))   # same second: by path, so order and ids never flip
    borrow_video_offsets(recs)
    return recs, skipped


def fill_gps_from_neighbours(cfg: Config, recs: list[dict], max_hours: float | None = None) -> None:
    """Photos without GPS inherit position/zone from the closest GPS'd photo in time, taken by
    the same device (`source`): while one phone is abroad, a screenshot on the other phone at
    home must not be placed abroad. Only a device that never records GPS at all borrows from
    the other devices."""
    import bisect
    if max_hours is None:
        max_hours = rules.effective(cfg)["excursions"].get("neighbour_gps_hours", 48.0)
    located = [i for i, r in enumerate(recs) if r.get("lat") is not None]
    if not located:
        return
    groups: dict[str | None, list[int]] = {None: located}
    for i in located:
        if recs[i].get("source") is not None:
            groups.setdefault(recs[i]["source"], []).append(i)
    times = {k: [recs[i]["_t"] for i in v] for k, v in groups.items()}

    def nearest(key, t):
        idx = groups[key]                                   # the key was checked against `groups`
        k = bisect.bisect_left(times[key], t)
        cands = [idx[j] for j in (k - 1, k) if 0 <= j < len(idx)]
        best = min(cands, key=lambda j: abs((recs[j]["_t"] - t).total_seconds()))
        return best if abs((recs[best]["_t"] - t).total_seconds()) <= max_hours * 3600 else None

    for r in recs:
        if r.get("lat") is not None:
            continue
        key = r.get("source") if r.get("source") in groups else None
        best = nearest(key, r["_t"])
        if best is None:
            continue
        src = recs[best]
        r.update({"lat": src["lat"], "lon": src["lon"], "gps_source": f"neighbour:{src['file']}"})
        ingest.enrich_location(cfg, r)


def borrow_video_offsets(recs: list[dict], max_hours: float = 48) -> None:
    """A video's time is UTC converted to the home zone (ts_source "utc-home"). Abroad that is
    hours off from the photos next to it. Re-express the same instant in the UTC offset of the
    nearest photo whose offset is known (EXIF offset or GPS clock); the instant never changes."""
    known = [i for i, r in enumerate(recs)
             if r.get("ts_source") in ("exif+offset", "gps") and r["_t"].utcoffset() is not None]
    if not known:
        return
    import bisect
    times = [recs[i]["_t"] for i in known]
    for r in recs:
        if r.get("ts_source") != "utc-home":
            continue
        k = bisect.bisect_left(times, r["_t"])
        cands = [known[j] for j in (k - 1, k) if 0 <= j < len(known)]
        best = min(cands, key=lambda j: abs((recs[j]["_t"] - r["_t"]).total_seconds()))
        if abs((recs[best]["_t"] - r["_t"]).total_seconds()) > max_hours * 3600:
            continue
        r["_t"] = r["_t"].astimezone(recs[best]["_t"].tzinfo)
        r["ts"] = r["_t"].isoformat()


# --- excursions (trips and day outs) ---------------------------------------------

def find_excursions(cfg: Config, recs: list[dict], rs: dict | None = None) -> list[list[dict]]:
    """Maximal runs of photos away from home (local or away zone), ended by any photo at home.
    GPS-less photos ride along inside a run but never start or end one.

    Each device (`source`, the inbox) is followed on its own timeline: a photo taken at home
    with one phone must not end the other phone's trip, and two phones 10 000 km apart at the
    same time are two excursions, not one. Runs of different devices that overlap in time and
    are in the same area (some photos within a day of each other closer than
    trip_split_distance_km) are one excursion: the family trip with two phones."""
    ex = (rs or rules.effective(cfg))["excursions"]
    by_source: dict[str | None, list[dict]] = {}
    for r in recs:
        by_source.setdefault(r.get("source"), []).append(r)
    runs: list[list[dict]] = []
    for group in by_source.values():
        runs.extend(_device_excursions(cfg, group, ex))
    if len(by_source) <= 1 or not ex.get("merge_devices", True):
        return sorted(runs, key=lambda run: run[0]["_t"])
    return _merge_device_runs(cfg, runs, ex)


def _same_area(cfg: Config, a: list[dict], b: list[dict], hours: float = 24, dist_km: float | None = None) -> bool:
    """Some located photo of a is within `hours` of a located photo of b and closer than
    dist_km (the excursions' split distance)."""
    import bisect
    dist_km = cfg.trip_split_distance_km if dist_km is None else dist_km
    lb = [r for r in b if r.get("lat") is not None]
    if not lb:
        return False
    tb = [r["_t"] for r in lb]
    for r in a:
        if r.get("lat") is None:
            continue
        k = bisect.bisect_left(tb, r["_t"])
        for j in range(max(0, k - 3), min(len(lb), k + 3)):
            o = lb[j]
            if abs((o["_t"] - r["_t"]).total_seconds()) <= hours * 3600 and \
                    geo.haversine_km(r["lat"], r["lon"], o["lat"], o["lon"]) <= dist_km:
                return True
    return False


def _merge_device_runs(cfg: Config, runs: list[list[dict]], ex: dict | None = None) -> list[list[dict]]:
    ex = ex or rules.effective(cfg)["excursions"]
    slack = timedelta(hours=ex["local_gap_hours"])
    merged: list[list[dict]] = []
    for run in sorted(runs, key=lambda run: run[0]["_t"]):
        target = next((m for m in merged if run[0]["_t"] <= m[-1]["_t"] + slack
                       and _same_area(cfg, run, m, ex.get("same_area_hours", 24.0), ex["split_distance_km"])), None)
        if target is None:
            merged.append(list(run))
        else:
            target.extend(run)
            target.sort(key=lambda r: r["_t"])
    merged.sort(key=lambda run: run[0]["_t"])
    return merged


def _device_excursions(cfg: Config, recs: list[dict], ex: dict | None = None) -> list[list[dict]]:
    ex = ex or rules.effective(cfg)["excursions"]
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
    # split on long gaps between different areas; every part is then trimmed of GPS-less photos
    # at its ends (they ride along inside, they never open or close an excursion) and a part
    # without any located photo is dropped: a day out cannot consist of photos without a position
    out = []
    for run in runs:
        part = [run[0]]
        for prev, nxt in zip(run, run[1:], strict=False):
            gap_days = (nxt["_t"] - prev["_t"]).total_seconds() / 86400
            far = False
            if prev.get("lat") is not None and nxt.get("lat") is not None:     # GPS-less photos never split
                far = geo.haversine_km(prev["lat"], prev["lon"], nxt["lat"], nxt["lon"]) > ex["split_distance_km"]
            # near home you sleep at home: a gap over a night ends the outing even without a home photo
            near_home = geo.ZONE_LOCAL in (prev["zone"], nxt["zone"])
            if (gap_days > ex["split_gap_days"] and far) or (near_home and gap_days * 24 > ex["local_gap_hours"]):
                out.append(part)
                part = []
            part.append(nxt)
        out.append(part)
    trimmed = []
    for part in out:
        while part and part[0]["zone"] == geo.ZONE_UNKNOWN:
            part.pop(0)
        while part and part[-1]["zone"] == geo.ZONE_UNKNOWN:
            part.pop()
        if part:
            trimmed.append(part)
    return trimmed


def excursion_kind(cfg: Config, run: list[dict], rs: dict | None = None) -> str | None:
    """'trip', 'local' (a day out) or None (everyday): the first excursion rule that matches."""
    rule = rules.pick((rs or rules.effective(cfg))["excursion_rules"], rules.features(run))
    return rule["kind"] if rule else None


def _rule_of_kind(cfg: Config, block: str, kind: str, run: list[dict]) -> dict:
    """The rule of a kind that matches the run, else the first of that kind (the wrappers
    trip_proposal / local_proposal / home_proposal build a proposal of a given kind whatever
    the run looks like), else the default one."""
    rs = rules.effective(cfg)
    of_kind = [r for r in rs[block] if r["kind"] == kind]
    f = rules.features(run)
    hit = next((r for r in of_kind if rules.matches(r, f)), None)
    if hit is not None:
        return hit
    if of_kind:
        return of_kind[0]
    return next(r for r in rules.defaults(cfg)[block] if r["kind"] == kind)   # not in a custom set


def excursion_proposal(cfg: Config, rule: dict, run: list[dict], now: datetime | None,
                       f: dict | None = None) -> dict:
    """A trip or day-out proposal for a run under the rule that matched it."""
    f = f or rules.features(run)
    a, b = f["start"], f["end"]
    photos = [_photo_entry(r, _gps_conf(r)) for r in run]
    hours = f["span_hours"]
    note = (f"{hours:.0f} h away from home, ended by a home photo" if rule["kind"] == "trip"
            else f"{hours:.1f} h away from home, {len(run)} photos")
    return {
        "id": _pid(rule["kind"], run), "kind": rule["kind"],
        "name": rules.render_name(cfg, rule, run, f),
        "start": a.isoformat(), "end": b.isoformat(), "photos": photos,
        "n": len(photos), "n_uncertain": sum(p["uncertain"] for p in photos),
        "status": "ongoing" if rules.ongoing(rule, f, now) else "pending",
        "decision": {"by": "rule", "conf": 1.0, "note": note, "rule": rule.get("name")},
    }


def _span(recs: list[dict]) -> tuple[datetime, datetime]:
    return recs[0]["_t"], recs[-1]["_t"]


def span_label(a: datetime, b: datetime, month_only: bool = True) -> str:
    """One day -> 2026-08-08; several days in one month -> 2026-08 (or 2026-08-08..29 when
    month_only is off); across months -> 2026-06-28..07-03; across years -> full dates."""
    if a.date() == b.date():
        return a.strftime("%Y-%m-%d")
    if a.year == b.year and a.month == b.month:
        return f"{a:%Y-%m}" if month_only else f"{a:%Y-%m-%d}..{b:%d}"
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
    """A folder name: no path separators or characters Windows/SMB refuse, no control characters,
    single spaces, nothing leading or trailing that a file system would drop."""
    name = re.sub(r"[\x00-\x1f\x7f]+", " ", name)
    name = re.sub(r'[\\/:*?"<>|]+', "-", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:              # ext4/SMB: 255 bytes; leave room for "_1"
        while len(name.encode("utf-8")) > MAX_NAME_BYTES:
            name = name[:-1]
        name = name.strip(" .")
    return name


MAX_NAME_BYTES = 200


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
    return excursion_proposal(cfg, _rule_of_kind(cfg, "excursion_rules", "trip", run), run, now)


# --- bursts --------------------------------------------------------------------

def group_bursts(cfg: Config, recs: list[dict], gap_hours: float | None = None) -> list[list[dict]]:
    gap_hours = rules.effective(cfg)["bursts"]["gap_hours"] if gap_hours is None else gap_hours
    bursts, cur = [], []
    for r in recs:
        if cur and (r["_t"] - cur[-1]["_t"]).total_seconds() > gap_hours * 3600:
            bursts.append(cur)
            cur = []
        cur.append(r)
    if cur:
        bursts.append(cur)
    return bursts


BASELINE_PATH = DATA_DIR / "baseline.json"


def home_baseline(recs: list[dict], persist: bool = True) -> float:
    """Median photos per day at home. The per-day counts are remembered in data/baseline.json
    (highest count seen per day), so the baseline does not drift down once everyday photos have
    been moved out of the inbox and only the bursts remain."""
    per_day = {str(d): n for d, n in Counter(r["_t"].date() for r in recs if r["zone"] == geo.ZONE_HOME).items()}
    seen: dict[str, int] = {}
    if persist and BASELINE_PATH.exists():
        try:
            seen = {k: int(v) for k, v in json.loads(BASELINE_PATH.read_text(encoding="utf-8")).items()}
        except (json.JSONDecodeError, OSError, ValueError):
            seen = {}
    for day, n in per_day.items():
        seen[day] = max(seen.get(day, 0), n)
    if persist and seen != {}:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(json.dumps(seen, sort_keys=True), encoding="utf-8")
    return statistics.median(seen.values()) if seen else 1.0


def local_proposal(cfg: Config, run: list[dict], now: datetime | None = None) -> dict:
    return excursion_proposal(cfg, _rule_of_kind(cfg, "excursion_rules", "local", run), run, now)


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


def home_proposal(cfg: Config, burst: list[dict], threshold: float, rule: dict | None = None) -> dict:
    """A dense burst at home. Metadata cannot tell a birthday from a burst of shots of the same
    thing (neither could a decision model, see docs/DESIGN.md section 10), so every burst well
    above your usual day is proposed and you name or reject it."""
    rule = rule or _rule_of_kind(cfg, "burst_rules", "home", burst)
    f = rules.features(burst)
    a, b = f["start"], f["end"]
    photos = [_photo_entry(r, 1.0) for r in burst]
    st = home_state(cfg, burst, threshold)
    note = (f"{st['photos']} files in {st['duration_h']} h at home, {st['burst_ratio']}x your usual day, "
            f"{st['devices']} device{'s' if st['devices'] != 1 else ''}")
    return {"id": _pid("home", burst), "kind": "home",
            "name": rules.render_name(cfg, rule, burst, f),
            "start": a.isoformat(), "end": b.isoformat(), "photos": photos, "n": len(photos),
            "n_uncertain": 0, "status": "pending",
            "decision": {"by": "rule", "conf": 1.0, "note": note, "state": st, "rule": rule.get("name")}}


def automatic_proposals(cfg: Config, recs: list[dict], now: datetime, baseline: float,
                        rs: dict | None = None) -> tuple[dict[str, dict], float]:
    """The proposals the rules make from records nobody placed by hand: excursions first (the
    first matching excursion rule decides), then bursts of what is left (the first matching
    burst rule). Returns them and the threshold of the first home-burst rule (shown in stats)."""
    rs = rs or rules.effective(cfg)
    new: dict[str, dict] = {}
    taken: set[str] = set()
    for run_ in find_excursions(cfg, recs, rs):
        f = rules.features(run_)
        rule = rules.pick(rs["excursion_rules"], f)
        if rule is None:
            continue
        pr = excursion_proposal(cfg, rule, run_, now, f)
        new[pr["id"]] = pr
        taken.update(p["path"] for p in pr["photos"])
    rest = [r for r in recs if r["path"] not in taken]
    home_rule = next((r for r in rs["burst_rules"] if r["kind"] == "home"), None)
    threshold = rules.threshold(home_rule, baseline) if home_rule else 0.0
    for burst in group_bursts(cfg, rest, rs["bursts"]["gap_hours"]):
        f = rules.features(burst, baseline)
        rule = rules.pick(rs["burst_rules"], f)
        if rule is None:
            continue
        if rule["kind"] == "home":
            pr = home_proposal(cfg, burst, rules.threshold(rule, baseline), rule)
        else:                                               # a custom rule: a burst as a day out or a trip
            pr = excursion_proposal(cfg, rule, burst, now, f)
        new[pr["id"]] = pr
        taken.update(p["path"] for p in pr["photos"])
    return new, threshold


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
    default = f"{span_label(a, b, Config().name_multiday_by_month)} {where}"
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
             for p in pr["photos"] if p["path"] not in set(pr.get("excluded", []))}
    return [r for r in records_filled(cfg) if r["path"] not in taken]


# --- driver --------------------------------------------------------------------

def _reconcile_corrections(cfg: Config, props: dict) -> None:
    """Photos removed from an applied cluster in the UI live in the inbox again; their manifest
    lists them under corrections. Keep them excluded in the proposal so they count as everyday
    (also repairs removals made before the exclusion was recorded)."""
    from . import mover  # local import: mover imports cluster
    for pr in props.values():
        if pr["status"] != "applied":
            continue
        m = mover.read_manifest(mover.target_folder(cfg, pr))
        if not m:
            continue
        removed = {c["src"] for c in m.get("corrections", [])}
        kept = {p["dst"] for p in m.get("photos", [])}
        ex = {p for p in pr.get("excluded", []) if p not in kept} | removed
        pr["excluded"] = sorted(ex)


def _keep_identities(old: dict, new: dict) -> None:
    """A proposal's id is a hash of its first and last photo, so a photo that syncs late and
    lands at either end gives the same excursion a new id, and with it the review state (toggled
    photos, an edited name, an approval) was lost on the next run. A new proposal that mostly
    consists of the photos of an old live proposal keeps the old id."""
    live = {pid: pr for pid, pr in old.items()
            if pr["status"] in ("pending", "ongoing", "approved") and not pr.get("manual")}
    if not live:
        return
    owner = {p["path"]: pid for pid, pr in live.items() for p in pr["photos"]}
    claimed: set[str] = {pid for pid in new if pid in old}
    # the biggest new proposal claims first: an old one split in two keeps its id on the larger half
    for pid in sorted(new, key=lambda k: -len(new[k]["photos"])):
        pr = new[pid]
        if pid in old or pr.get("manual"):
            continue
        hits = Counter(owner[p["path"]] for p in pr["photos"] if p["path"] in owner)
        for old_pid, n in hits.most_common():
            if old_pid in claimed:
                continue
            if n >= 0.5 * len(live[old_pid]["photos"]):          # at least half of the old one is in here
                pr["id"] = old_pid
                new[old_pid] = new.pop(pid)
                claimed.add(old_pid)
            break


def run(cfg: Config) -> dict:
    """Recompute proposals from the inbox, keeping the status of ones already reviewed."""
    global busy
    busy = False                                  # the pipeline itself always sees fresh records
    now = datetime.now(timezone.utc)
    _, skipped = load_records(cfg)                # reading the records may take seconds: outside the lock
    recs = records_filled(cfg)
    with proposals_lock:                          # from reading the file to writing it: no click is lost
        return _run_locked(cfg, now, skipped, recs)


def _run_locked(cfg: Config, now: datetime, skipped: list[dict], recs: list[dict]) -> dict:
    old = load_proposals()
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

    baseline = home_baseline(recs)
    made, threshold = automatic_proposals(cfg, auto_recs, now, baseline)
    new.update(made)
    for pr in made.values():
        taken.update(p["path"] for p in pr["photos"])

    _keep_identities(old, new)
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
            pr["name_edited"] = bool(prev.get("name_edited"))   # a named burst stays out of _unnamed/
            pr["auto"] = bool(prev.get("auto"))                 # (a run between approve and move lost both)
            if prev.get("error"):
                pr["error"] = prev["error"]
        elif prev and prev.get("name_edited"):
            pr["name"], pr["name_edited"] = prev["name"], True
        have = {p["path"] for p in pr["photos"]}
        pr["excluded"] = sorted(p for p in (prev or {}).get("excluded", []) if p in have)
    # keep applied/rejected proposals whose photos are gone from the inbox (history)
    for pid, pr in old.items():
        if pid not in new and pr["status"] in ("applied", "rejected"):
            new[pid] = pr
    _reconcile_corrections(cfg, new)
    save_proposals(new)

    everyday = [r for r in recs if r["path"] not in taken]
    return {"proposals": len([p for p in new.values() if p["status"] in ("pending", "ongoing")]),
            "photos": len(recs), "everyday": len(everyday), "no_timestamp": len(skipped),
            "baseline": baseline, "threshold": threshold}
