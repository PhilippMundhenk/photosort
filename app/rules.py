"""The clustering rules, declared instead of coded (issue #2).

A ruleset is a mapping (YAML in the config under `rules`, or built from the plain settings
fields when none is set):

    homes:            where "home" is: several places (issue #3); a photo inside any of them is
                      at home, and an excursion ends at any of them
    excursions:       how runs of photos away from home are cut (gaps, distances, device merge,
                      the hours within which two devices are "in the same area" and a photo
                      without GPS borrows a neighbour's position, and the longest gap between
                      two located photos across which its position is interpolated)
    excursion_rules:  evaluated in order over every excursion run; the first whose `when`
                      matches decides: a trip, a day out (`local`), or `everyday` (no cluster)
    bursts:           how the remaining photos are grouped into bursts (a gap in hours)
    burst_rules:      the same over every burst: a home occasion, or everyday
    naming:           which places name a cluster (place_min_share of its files, at least
                      place_min_photos of them; short stops at either end are left out:
                      under stop_hours, at most stop_photos or stop_share of the files, and
                      stop_ratio times fewer than the other places), most photographed first
    review:           uncertain_below: the confidence under which a photo counts as uncertain

A rule: `name`, `kind` (trip / local / home / everyday), `when` (conditions, all must hold),
`name_template` and `ongoing_days` for the kinds that make a proposal. Conditions:

    span_hours_min / span_hours_max   the run's first to last photo
    photos_min / photos_max           number of files
    located_min                       files with a position (own or a neighbour's)
    mostly_zone                       more than half of the files in that zone (home/local/away)
    mostly_share                      what "mostly" means for mostly_zone (default 0.5)
    max_km_min / max_km_max           the farthest photo's distance from home (km)
    inbox                             a list of inbox names: the run has files from one of them
    zone                              the majority zone
    baseline_factor                   files >= your usual photos per day at home x factor (bursts)
    devices_min / videos_min          distinct devices / videos in the run
    weekday                           a list of weekday names the run starts on
    start_hour_min / end_hour_max     the hour the run starts at / ends by

Name templates: {span} (2026-08 or 2026-06-27), {places} (the places passed, then countries),
{place} (the most photographed place, else "Ausflug"), {media} ("12 Fotos, 2 Videos"), {n},
{home} (the nearest home's name).

`defaults(cfg)` is exactly what the code did before the engine (the golden test in
tests/test_rules.py holds it to that); the settings fields feed it. Custom rules replace the
rule lists they define and fall back to the defaults for the rest; in the parameter blocks
(excursions, bursts) a key left out keeps its default.
"""
from __future__ import annotations

import copy
import logging
from collections import Counter
from datetime import datetime, timedelta

import yaml

from . import geo
from .config import Config

log = logging.getLogger("photosort.rules")

KINDS = ("trip", "local", "home", "everyday")
ZONES = (geo.ZONE_HOME, geo.ZONE_LOCAL, geo.ZONE_AWAY, geo.ZONE_UNKNOWN)
CONDITIONS: dict[str, type] = {
    "span_hours_min": float, "span_hours_max": float, "photos_min": int, "photos_max": int,
    "located_min": int, "mostly_zone": str, "zone": str, "baseline_factor": float,
    "devices_min": int, "videos_min": int, "weekday": list, "start_hour_min": int, "end_hour_max": int,
    "mostly_share": float, "max_km_min": float, "max_km_max": float, "inbox": list,
}
RULE_KEYS = {"name", "kind", "when", "name_template", "ongoing_days"}
EXCURSION_KEYS = {"split_gap_days": float, "split_distance_km": float, "local_gap_hours": float, "merge_devices": bool,
                  "same_area_hours": float, "neighbour_gps_hours": float, "interpolate_gps_hours": float}
BURST_KEYS = {"gap_hours": float}
NAMING_KEYS = {"place_min_share": float, "place_min_photos": int, "stop_hours": float, "stop_photos": int,
               "stop_share": float, "stop_ratio": float}
REVIEW_KEYS = {"uncertain_below": float}
TEMPLATE_FIELDS = ("span", "places", "place", "media", "n", "home")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
BLOCKS = ("homes", "excursions", "excursion_rules", "bursts", "burst_rules", "naming", "review")


# --- the ruleset ---------------------------------------------------------------------------------------

def homes(cfg: Config) -> list[dict]:
    """The places that count as home: the settings fields first, then `homes` (issue #3)."""
    out = []
    if cfg.home_lat or cfg.home_lon:
        out.append({"name": "home", "lat": float(cfg.home_lat), "lon": float(cfg.home_lon),
                    "radius_km": float(cfg.home_radius_km)})
    for h in cfg.homes or []:
        try:
            lat, lon = float(h["lat"]), float(h["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        out.append({"name": str(h.get("name") or f"home {len(out) + 1}"), "lat": lat, "lon": lon,
                    "radius_km": float(h.get("radius_km") or cfg.home_radius_km)})
    return out


def zone_at(cfg: Config, lat: float, lon: float,
            radius_km: float | None = None) -> tuple[float | None, str, str | None]:
    """Distance to the nearest home (None without any), the zone there, that home's name. A
    point inside any home's radius is at home; beyond every home the zone follows the distance
    to the nearest one (local / away). `radius_km`: an inbox's own radius for every home (a
    device with poor GPS, issue #6)."""
    best: tuple[float, str, bool] | None = None
    for h in homes(cfg):
        d = geo.haversine_km(h["lat"], h["lon"], lat, lon)
        if best is None or d < best[0]:
            best = (d, h["name"], d < (h["radius_km"] if radius_km is None else radius_km))
    if best is None:
        return None, geo.ZONE_UNKNOWN, None
    return best[0], geo.ZONE_HOME if best[2] else geo.zone_for(cfg, best[0]), best[1]


def defaults(cfg: Config) -> dict:
    """The rules the code applied before the engine, with the settings fields as parameters.
    With more than one home, a home occasion's name says which home (issue #3)."""
    hs = homes(cfg)
    occasion = "{span} ({media}) @ {home}" if len(hs) > 1 else "{span} ({media})"
    return {
        "homes": hs,
        "excursions": {"split_gap_days": float(cfg.trip_gap_days),
                       "split_distance_km": float(cfg.trip_split_distance_km),
                       "local_gap_hours": float(cfg.local_gap_hours), "merge_devices": True,
                       "same_area_hours": 24.0, "neighbour_gps_hours": 48.0,
                       "interpolate_gps_hours": float(cfg.gps_interpolation_hours)},
        "excursion_rules": [
            {"name": "trip", "kind": "trip", "when": {"span_hours_min": float(cfg.trip_min_hours),
                                                      "located_min": int(cfg.trip_min_photos)},
             "name_template": "{span} {places}", "ongoing_days": float(cfg.trip_gap_days)},
            {"name": "long run without positions", "kind": "everyday",
             "when": {"span_hours_min": float(cfg.trip_min_hours)}},
            {"name": "day out far away", "kind": "local",
             "when": {"mostly_zone": geo.ZONE_AWAY, "photos_min": int(cfg.trip_min_photos)},
             "name_template": "{span} {place}", "ongoing_days": 1.0},
            {"name": "far away with too few photos", "kind": "everyday", "when": {"mostly_zone": geo.ZONE_AWAY}},
            {"name": "day out near home", "kind": "local", "when": {"photos_min": int(cfg.dayout_min_photos)},
             "name_template": "{span} {place}", "ongoing_days": 1.0},
        ],
        "bursts": {"gap_hours": float(cfg.burst_gap_hours)},
        # a place names a cluster when it has at least place_min_photos files (small clusters:
        # any) and place_min_share of them; a photo whose position is borrowed or missing is
        # "uncertain" (parked in _review when a cluster is moved without asking) below this
        "naming": {"place_min_share": 0.03, "place_min_photos": 2, "stop_hours": 3.0, "stop_photos": 5,
                   "stop_share": 0.05, "stop_ratio": 3.0},
        "review": {"uncertain_below": 0.7},
        "burst_rules": [
            {"name": "home occasion", "kind": "home",
             "when": {"zone": geo.ZONE_HOME, "photos_min": int(cfg.burst_min_photos),
                      "baseline_factor": float(cfg.burst_baseline_factor)},
             "name_template": occasion},
        ],
    }


def effective(cfg: Config) -> dict:
    """The ruleset in force: the custom blocks over the defaults; invalid custom rules are
    ignored (Settings refuses to save them, a hand-edited file is logged)."""
    base = defaults(cfg)
    custom = cfg.rules if isinstance(cfg.rules, dict) else {}
    if not custom:
        return base
    merged = {}
    for k in BLOCKS:
        if k not in custom:
            merged[k] = base[k]
        elif isinstance(base[k], dict) and isinstance(custom[k], dict):   # a parameter block: what is left
            merged[k] = {**base[k], **copy.deepcopy(custom[k])}           # out keeps its default
        else:
            merged[k] = copy.deepcopy(custom[k])
    problems = validate(merged)
    if problems:
        log.error("custom rules ignored: %s", "; ".join(problems))
        return base
    return merged


def parse(text: str) -> dict:
    """YAML from the settings page into a ruleset (only the blocks it defines). Raises
    ValueError with something a person can act on."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"not valid YAML: {e}") from None
    if not isinstance(data, dict):
        raise ValueError("the rules must be a mapping with the blocks " + ", ".join(BLOCKS))
    unknown = sorted(set(data) - set(BLOCKS))
    if unknown:
        raise ValueError(f"unknown block(s): {', '.join(unknown)} (known: {', '.join(BLOCKS)})")
    problems = validate({**{k: None for k in BLOCKS}, **data})
    if problems:
        raise ValueError("; ".join(problems))
    return data


def to_yaml(rs: dict) -> str:
    return yaml.safe_dump({k: rs[k] for k in BLOCKS if k in rs}, sort_keys=False, allow_unicode=True)


def validate(rs: dict) -> list[str]:
    """Every problem in a ruleset (blocks set to None are skipped: they fall back to defaults)."""
    out: list[str] = []
    h = rs.get("homes")
    if h is not None:
        if not isinstance(h, list):
            out.append("homes must be a list")
        else:
            for i, home in enumerate(h):
                if not isinstance(home, dict) or not all(isinstance(home.get(k), (int, float)) for k in ("lat", "lon")):
                    out.append(f"homes[{i}] needs lat and lon")
    ex = rs.get("excursions")
    if ex is not None:
        out += _check_block("excursions", ex, EXCURSION_KEYS)
    for name, keys in (("bursts", BURST_KEYS), ("naming", NAMING_KEYS), ("review", REVIEW_KEYS)):
        if rs.get(name) is not None:
            out += _check_block(name, rs[name], keys)
    for block, allowed in (("excursion_rules", ("trip", "local", "everyday")),
                           ("burst_rules", ("home", "local", "everyday"))):
        rules = rs.get(block)
        if rules is None:
            continue
        if not isinstance(rules, list):
            out.append(f"{block} must be a list of rules")
            continue
        for i, rule in enumerate(rules):
            out += _check_rule(f"{block}[{i}]", rule, allowed)
    return out


def _check_block(name: str, block, keys: dict) -> list[str]:
    if not isinstance(block, dict):
        return [f"{name} must be a mapping"]
    out = []
    for k, v in block.items():
        if k not in keys:
            out.append(f"{name}: unknown key {k} (known: {', '.join(keys)})")
        elif keys[k] is bool and not isinstance(v, bool):
            out.append(f"{name}.{k} must be true or false")
        elif keys[k] is float and (isinstance(v, bool) or not isinstance(v, (int, float))):
            out.append(f"{name}.{k} must be a number")
    return out


def _check_rule(where: str, rule, kinds: tuple) -> list[str]:
    if not isinstance(rule, dict):
        return [f"{where} must be a mapping"]
    out = []
    for k in rule:
        if k not in RULE_KEYS:
            out.append(f"{where}: unknown key {k} (known: {', '.join(sorted(RULE_KEYS))})")
    if rule.get("kind") not in kinds:
        out.append(f"{where}: kind must be one of {', '.join(kinds)}")
    when = rule.get("when", {})
    if not isinstance(when, dict):
        out.append(f"{where}: when must be a mapping of conditions")
    else:
        for k, v in when.items():
            t = CONDITIONS.get(k)
            if t is None:
                out.append(f"{where}: unknown condition {k} (known: {', '.join(CONDITIONS)})")
            elif t is str and v not in ZONES:
                out.append(f"{where}: {k} must be one of {', '.join(ZONES)}")
            elif k == "weekday" and (not isinstance(v, list) or any(d not in WEEKDAYS for d in v)):
                out.append(f"{where}: weekday must be a list of weekday names")
            elif k == "inbox" and (not isinstance(v, list) or not all(isinstance(d, str) for d in v)):
                out.append(f"{where}: inbox must be a list of inbox names")
            elif t in (int, float) and (isinstance(v, bool) or not isinstance(v, (int, float))):
                out.append(f"{where}: {k} must be a number")
    tpl = rule.get("name_template")
    if tpl is not None:
        if not isinstance(tpl, str):
            out.append(f"{where}: name_template must be text")
        else:
            try:
                tpl.format(**{f: "" for f in TEMPLATE_FIELDS})
            except (KeyError, IndexError, ValueError) as e:
                out.append(f"{where}: name_template can use {{{'}, {'.join(TEMPLATE_FIELDS)}}} only ({e})")
    od = rule.get("ongoing_days")
    if od is not None and (isinstance(od, bool) or not isinstance(od, (int, float))):
        out.append(f"{where}: ongoing_days must be a number")
    return out


# --- evaluation ----------------------------------------------------------------------------------------

def features(run: list[dict], baseline: float | None = None) -> dict:
    """What the conditions look at, computed once per run."""
    from . import cluster  # local: cluster imports this module
    a, b = cluster._span(run)
    zones = Counter(r["zone"] for r in run)
    return {
        "span_hours": (b - a).total_seconds() / 3600, "photos": len(run),
        "located": sum(n for z, n in zones.items() if z != geo.ZONE_UNKNOWN),
        "zones": zones, "zone": zones.most_common(1)[0][0],
        "devices": None, "videos": sum(1 for r in run if r.get("media") == "video"),
        "max_km": max((r["dist_km"] for r in run if r.get("dist_km") is not None), default=None),
        "inboxes": sorted({r.get("source") for r in run if r.get("source")}),
        "weekday": a.strftime("%A"), "start_hour": a.hour, "end_hour": b.hour,
        "baseline": baseline, "start": a, "end": b, "_run": run,
    }


def holds(k: str, v, f: dict, when: dict | None = None) -> bool:
    """One condition of a rule against a run's features (an unknown key holds). `when`: the
    rule's other conditions (mostly_share sets what "mostly" means for mostly_zone)."""
    from . import cluster
    share = float((when or {}).get("mostly_share", 0.5))
    checks = {
        "span_hours_min": lambda: f["span_hours"] >= v, "span_hours_max": lambda: f["span_hours"] < v,
        "photos_min": lambda: f["photos"] >= v, "photos_max": lambda: f["photos"] <= v,
        "located_min": lambda: f["located"] >= v,
        "mostly_zone": lambda: f["zones"].get(v, 0) > f["photos"] * share, "zone": lambda: f["zone"] == v,
        "max_km_min": lambda: f["max_km"] is not None and f["max_km"] >= v,
        "max_km_max": lambda: f["max_km"] is not None and f["max_km"] < v,
        "inbox": lambda: any(i in v for i in f["inboxes"]),
        "baseline_factor": lambda: f["photos"] >= (f["baseline"] or 0.0) * v,
        "videos_min": lambda: f["videos"] >= v, "weekday": lambda: f["weekday"] in v,
        "start_hour_min": lambda: f["start_hour"] >= v, "end_hour_max": lambda: f["end_hour"] <= v,
    }
    if k == "devices_min":
        if f["devices"] is None:
            f["devices"] = cluster.count_devices(f["_run"])
        return f["devices"] >= v
    check = checks.get(k)
    return check is None or bool(check())


def matches(rule: dict, f: dict) -> bool:
    when = rule.get("when") or {}
    return all(holds(k, v, f, when) for k, v in when.items())


def pick(rules: list[dict], f: dict) -> dict | None:
    """The first rule whose conditions hold, or None (everyday)."""
    for rule in rules:
        if matches(rule, f):
            return None if rule["kind"] == "everyday" else rule
    return None


def threshold(rule: dict, baseline: float) -> float:
    """What a burst must reach under a rule: the larger of its photo minimum and the baseline
    multiple (the review UI shows a burst's ratio to it)."""
    when = rule.get("when") or {}
    return max(float(when.get("photos_min", 0)), baseline * float(when.get("baseline_factor", 0)))


def render_name(cfg: Config, rule: dict, run: list[dict], f: dict) -> str:
    from . import cluster
    tpl = rule.get("name_template") or "{span}"
    place = Counter((r.get("place") or {}).get("place") for r in run if r["zone"] != geo.ZONE_HOME and r.get("place"))
    home = Counter(r.get("home") for r in run if r.get("home")).most_common(1)
    fields = {
        "span": cluster.span_label(f["start"], f["end"], cfg.name_multiday_by_month),
        "places": cluster.places_label(cfg, run),
        "place": place.most_common(1)[0][0] if place else "Ausflug",
        "media": cluster.media_label(run), "n": len(run),
        "home": home[0][0] if home else "home",
    }
    return cluster.sanitize(tpl.format(**fields))


def ongoing(rule: dict, f: dict, now: datetime | None) -> bool:
    days = rule.get("ongoing_days")
    return now is not None and days is not None and (now - f["end"]) < timedelta(days=float(days))


# --- the why panel (issue #6) ---------------------------------------------------------------------------

FACT_OF = {"span_hours_min": "span_hours", "span_hours_max": "span_hours", "photos_min": "photos",
           "photos_max": "photos", "located_min": "located", "mostly_zone": "zones", "zone": "zone",
           "baseline_factor": "baseline", "devices_min": "devices", "videos_min": "videos",
           "weekday": "weekday", "start_hour_min": "start_hour", "end_hour_max": "end_hour",
           "mostly_share": "zones", "max_km_min": "max_km", "max_km_max": "max_km", "inbox": "inboxes"}
CORE_FACTS = ("span_hours", "photos", "located", "zones", "devices", "videos")


def failed(rule: dict, f: dict) -> dict:
    """The conditions of a rule the run does not meet, with their values."""
    when = rule.get("when") or {}
    return {k: v for k, v in when.items() if not holds(k, v, f, when)}


def explain(block: list[dict], rule: dict, f: dict) -> dict:
    """What the why panel shows, stored with the proposal so the page computes nothing: the
    rule's conditions and those the run misses (a wrapper may build a proposal its rule does not
    match), the rules checked before it with the conditions that ruled each out, and the run's
    values those conditions look at, rounded. A few numbers per proposal."""
    from . import cluster
    idx = next((i for i, r in enumerate(block) if r is rule or r == rule), 0)
    before = []
    for r in block[:idx]:
        entry = {"rule": r.get("name"), "failed": failed(r, f)}
        if "mostly_share" in (r.get("when") or {}):           # how "mostly" was meant, for the words
            entry["share"] = r["when"]["mostly_share"]
        before.append(entry)
    if f["devices"] is None:
        f["devices"] = cluster.count_devices(f["_run"])
    keys = set(CORE_FACTS) | {FACT_OF[k] for r in [*block[:idx], rule] for k in (r.get("when") or {}) if k in FACT_OF}
    facts = {}
    for k in sorted(keys):
        v = f[k]
        facts[k] = dict(v) if k == "zones" else round(v, 2) if isinstance(v, float) else v
    return {"when": dict(rule.get("when") or {}), "failed": sorted(failed(rule, f)), "before": before,
            "facts": facts}


ZONE_TEXT = {geo.ZONE_HOME: "at home", geo.ZONE_LOCAL: "near home", geo.ZONE_AWAY: "far from home",
             geo.ZONE_UNKNOWN: "without a position"}


def _num(x) -> str:
    if x is None:
        return "?"
    return f"{x:.1f}".rstrip("0").rstrip(".") if isinstance(x, float) else str(x)


def _plural(n, word: str) -> str:
    return f"{_num(n)} {word}{'' if n == 1 else 's'}"


def condition_text(k: str, v, facts: dict, when: dict | None = None) -> tuple[str, str]:
    """A condition in plain words, and the run's value for it."""
    g = facts.get
    share = float((when or {}).get("mostly_share", 0.5))
    most = "more than half" if share == 0.5 else f"more than {_num(round(share * 100, 1))} %"
    texts = {
        "span_hours_min": lambda: (f"lasts at least {_num(v)} h", f"{_num(g('span_hours'))} h"),
        "span_hours_max": lambda: (f"lasts less than {_num(v)} h", f"{_num(g('span_hours'))} h"),
        "photos_min": lambda: (f"at least {_plural(v, 'file')}", _plural(g("photos"), "file")),
        "photos_max": lambda: (f"at most {_plural(v, 'file')}", _plural(g("photos"), "file")),
        "located_min": lambda: (f"at least {_num(v)} with a position", f"{_num(g('located'))} with a position"),
        "mostly_zone": lambda: (f"{most} of the files {ZONE_TEXT.get(v, v)}",
                                f"{(g('zones') or {}).get(v, 0)} of {_num(g('photos'))}"),
        "zone": lambda: (f"most files {ZONE_TEXT.get(v, v)}", f"most {ZONE_TEXT.get(g('zone'), '?')}"),
        "baseline_factor": lambda: (
            f"at least {_num(v)}x your usual day at home"
            + (f" ({_num(float(v) * g('baseline'))} files)" if g("baseline") is not None else ""),
            f"{_plural(g('photos'), 'file')}, usual day {_num(g('baseline'))}"),
        "devices_min": lambda: (f"at least {_plural(v, 'device')}", _plural(g("devices"), "device")),
        "videos_min": lambda: (f"at least {_plural(v, 'video')}", _plural(g("videos"), "video")),
        "weekday": lambda: (f"starts on {', '.join(v)}", f"starts on {g('weekday', '?')}"),
        "start_hour_min": lambda: (f"starts at {_num(v)}:00 or later", f"starts at {_num(g('start_hour'))}:xx"),
        "end_hour_max": lambda: (f"ends by {_num(v)}:00", f"ends at {_num(g('end_hour'))}:xx"),
        "mostly_share": lambda: (f"“mostly” means {most}", "rule setting"),
        "max_km_min": lambda: (f"reaches at least {_num(v)} km from home", f"farthest {_num(g('max_km'))} km"),
        "max_km_max": lambda: (f"stays within {_num(v)} km of home", f"farthest {_num(g('max_km'))} km"),
        "inbox": lambda: (f"has files from {', '.join(v)}", f"files from {', '.join(g('inboxes') or []) or '?'}"),
    }
    text = texts.get(k)
    return text() if text else (f"{k} {v}", "?")


def describe(decision: dict | None) -> dict | None:
    """The why panel in plain words, from what the proposal stored (None: nothing to show: a
    proposal made by hand, or one stored before the panel existed, which the next run rebuilds)."""
    decision = decision or {}
    why, note = decision.get("why") or {}, decision.get("note")
    if not why:
        return None
    facts = why.get("facts") or {}
    missed = set(why.get("failed") or ())
    out = {"rule": decision.get("rule"), "note": note, "conditions": [], "before": [], "run": None, "gaps": []}
    when = why.get("when") or {}
    for k, v in when.items():
        need, have = condition_text(k, v, facts, when)
        out["conditions"].append({"need": need, "have": have, "ok": k not in missed})
    for b in why.get("before") or ():
        out["before"].append({"rule": b.get("rule"), "missed": [
            "{}: {}".format(*condition_text(k, v, facts, {"mostly_share": b.get("share", 0.5)}))
            for k, v in (b.get("failed") or {}).items()]})
    if facts:
        zones = facts.get("zones") or {}
        parts = [f"{_num(facts.get('span_hours'))} h", _plural(facts.get("photos"), "file")]
        if facts.get("videos"):
            parts.append(f"{_plural(facts['videos'], 'video')} among them")
        if facts.get("devices") is not None:
            parts.append(f"from {_plural(facts['devices'], 'device')}")
        where = ", ".join(f"{n} {ZONE_TEXT.get(z, z)}" for z, n in sorted(zones.items(), key=lambda zn: -zn[1]))
        out["run"] = ", ".join(parts) + (f"; {where}" if where else "")
    gaps = why.get("gaps") or {}
    if "longest_h" in gaps:
        km = gaps.get("longest_km")
        out["gaps"].append(f"Longest pause between two photos: {_num(gaps['longest_h'])} h"
                           + (f", {_num(km)} km apart" if km is not None else ""))
    if gaps.get("step_km") is not None:
        out["gaps"].append(f"Biggest step between two photos in a row: {_num(gaps['step_km'])} km")
    if "split_gap_days" in gaps:
        out["gaps"].append(f"A run away from home is cut at a pause of more than {_num(gaps['split_gap_days'])} days "
                           f"between photos over {_num(gaps.get('split_distance_km'))} km apart, or of more than "
                           f"{_num(gaps.get('local_gap_hours'))} h near home (you sleep at home)")
    if "gap_hours" in gaps:
        out["gaps"].append(f"A burst ends at a pause of more than {_num(gaps['gap_hours'])} h between two photos")
    return out
