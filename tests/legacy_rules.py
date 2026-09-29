"""The clustering rules as they were before the rule engine (frozen on 2026-09-29, from
app/cluster.py at commit addbf09): the golden test in test_rules.py runs these and the engine
over the same libraries and requires identical proposals. Do not edit; a behaviour change of
the default ruleset is a deliberate decision that replaces this file."""
from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from app import geo, ingest
from app.cluster import UNCERTAIN_BELOW, sanitize
from app.config import Config


def proposals(cfg: Config, recs: list[dict], now: datetime, baseline: float) -> dict[str, dict]:
    """What _run_locked built from the automatic records (no manual clusters, no carry-over)."""
    new: dict[str, dict] = {}
    taken: set[str] = set()
    for run_ in find_excursions(cfg, recs):
        kind = excursion_kind(cfg, run_)
        if kind is None:
            continue
        pr = trip_proposal(cfg, run_, now) if kind == "trip" else local_proposal(cfg, run_, now)
        new[pr["id"]] = pr
        taken.update(p["path"] for p in pr["photos"])
    rest = [r for r in recs if r["path"] not in taken]
    threshold = max(cfg.burst_min_photos, baseline * cfg.burst_baseline_factor)
    for burst in group_bursts(cfg, rest):
        zones = Counter(r["zone"] for r in burst)
        major = zones.most_common(1)[0][0]
        if len(burst) < cfg.burst_min_photos:
            continue
        if major == geo.ZONE_HOME and len(burst) >= threshold:
            pr = home_proposal(cfg, burst, threshold)
        else:
            continue
        new[pr["id"]] = pr
        taken.update(p["path"] for p in pr["photos"])
    return new


def find_excursions(cfg: Config, recs: list[dict]) -> list[list[dict]]:
    """Maximal runs of photos away from home (local or away zone), ended by any photo at home.
    GPS-less photos ride along inside a run but never start or end one.

    Each device (`source`, the inbox) is followed on its own timeline: a photo taken at home
    with one phone must not end the other phone's trip, and two phones 10 000 km apart at the
    same time are two excursions, not one. Runs of different devices that overlap in time and
    are in the same area (some photos within a day of each other closer than
    trip_split_distance_km) are one excursion: the family trip with two phones."""
    by_source: dict[str | None, list[dict]] = {}
    for r in recs:
        by_source.setdefault(r.get("source"), []).append(r)
    runs: list[list[dict]] = []
    for group in by_source.values():
        runs.extend(_device_excursions(cfg, group))
    if len(by_source) <= 1:
        return runs
    return _merge_device_runs(cfg, runs)


def _same_area(cfg: Config, a: list[dict], b: list[dict], hours: float = 24) -> bool:
    """Some located photo of a is within `hours` of a located photo of b and closer than
    trip_split_distance_km."""
    import bisect
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
                    geo.haversine_km(r["lat"], r["lon"], o["lat"], o["lon"]) <= cfg.trip_split_distance_km:
                return True
    return False


def _merge_device_runs(cfg: Config, runs: list[list[dict]]) -> list[list[dict]]:
    slack = timedelta(hours=cfg.local_gap_hours)
    merged: list[list[dict]] = []
    for run in sorted(runs, key=lambda run: run[0]["_t"]):
        target = next((m for m in merged if run[0]["_t"] <= m[-1]["_t"] + slack and _same_area(cfg, run, m)), None)
        if target is None:
            merged.append(list(run))
        else:
            target.extend(run)
            target.sort(key=lambda r: r["_t"])
    merged.sort(key=lambda run: run[0]["_t"])
    return merged


def _device_excursions(cfg: Config, recs: list[dict]) -> list[list[dict]]:
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
                far = geo.haversine_km(prev["lat"], prev["lon"], nxt["lat"], nxt["lon"]) > cfg.trip_split_distance_km
            # near home you sleep at home: a gap over a night ends the outing even without a home photo
            near_home = geo.ZONE_LOCAL in (prev["zone"], nxt["zone"])
            if (gap_days > cfg.trip_gap_days and far) or (near_home and gap_days * 24 > cfg.local_gap_hours):
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


def excursion_kind(cfg: Config, run: list[dict]) -> str | None:
    """'trip' (spans >= trip_min_hours, enough located photos), 'local' (a day out with enough
    photos) or None (too small: everyday)."""
    a, b = _span(run)
    located = sum(1 for r in run if r["zone"] != geo.ZONE_UNKNOWN)
    if (b - a) >= timedelta(hours=cfg.trip_min_hours):
        return "trip" if located >= cfg.trip_min_photos else None
    # a day out far away is an outing even with three photos; near home it needs more
    # (the school run and the supermarket must stay everyday)
    away = sum(1 for r in run if r["zone"] == geo.ZONE_AWAY)
    need = cfg.trip_min_photos if away > len(run) / 2 else cfg.dayout_min_photos
    return "local" if len(run) >= need else None


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
        "name": sanitize(f"{span_label(a, b, cfg.name_multiday_by_month)} {places_label(cfg, run)}"),
        "start": a.isoformat(), "end": b.isoformat(), "photos": photos,
        "n": len(photos), "n_uncertain": sum(p["uncertain"] for p in photos),
        "status": "ongoing" if ongoing else "pending",
        "decision": {"by": "rule", "conf": 1.0,
                     "note": f"{(b - a).total_seconds() / 3600:.0f} h away from home, ended by a home photo"},
    }


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


def local_proposal(cfg: Config, run: list[dict], now: datetime | None = None) -> dict:
    a, b = _span(run)
    place = Counter((r.get("place") or {}).get("place") for r in run if r["zone"] != geo.ZONE_HOME and r.get("place"))
    name = place.most_common(1)[0][0] if place else "Ausflug"
    photos = [_photo_entry(r, _gps_conf(r)) for r in run]
    ongoing = now is not None and (now - b) < timedelta(days=1)
    return {"id": _pid("local", run), "kind": "local",
            "name": sanitize(f"{span_label(a, b, cfg.name_multiday_by_month)} {name}"),
            "start": a.isoformat(), "end": b.isoformat(), "photos": photos, "n": len(photos),
            "n_uncertain": sum(p["uncertain"] for p in photos), "status": "ongoing" if ongoing else "pending",
            "decision": {"by": "rule", "conf": 1.0,
                         "note": f"{(b - a).total_seconds() / 3600:.1f} h away from home, {len(run)} photos"}}


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


def home_proposal(cfg: Config, burst: list[dict], threshold: float) -> dict:
    """A dense burst at home. Metadata cannot tell a birthday from a burst of shots of the same
    thing (neither could a decision model, see docs/DESIGN.md section 10), so every burst well
    above your usual day is proposed and you name or reject it."""
    a, b = _span(burst)
    photos = [_photo_entry(r, 1.0) for r in burst]
    st = home_state(cfg, burst, threshold)
    note = (f"{st['photos']} files in {st['duration_h']} h at home, {st['burst_ratio']}x your usual day, "
            f"{st['devices']} device{'s' if st['devices'] != 1 else ''}")
    return {"id": _pid("home", burst), "kind": "home",
            "name": sanitize(f"{span_label(a, b, cfg.name_multiday_by_month)} ({media_label(burst)})"),
            "start": a.isoformat(), "end": b.isoformat(), "photos": photos, "n": len(photos),
            "n_uncertain": 0, "status": "pending",
            "decision": {"by": "rule", "conf": 1.0, "note": note, "state": st}}
