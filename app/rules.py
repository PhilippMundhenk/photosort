"""The clustering rules, declared instead of coded (issue #2).

A ruleset is a mapping (YAML in the config under `rules`, or built from the plain settings
fields when none is set):

    homes:            where "home" is: several places (issue #3); a photo inside any of them is
                      at home, and an excursion ends at any of them
    excursions:       how runs of photos away from home are cut (gaps, distances, device merge)
    excursion_rules:  evaluated in order over every excursion run; the first whose `when`
                      matches decides: a trip, a day out (`local`), or `everyday` (no cluster)
    bursts:           how the remaining photos are grouped into bursts (a gap in hours)
    burst_rules:      the same over every burst: a home occasion, or everyday

A rule: `name`, `kind` (trip / local / home / everyday), `when` (conditions, all must hold),
`name_template` and `ongoing_days` for the kinds that make a proposal. Conditions:

    span_hours_min / span_hours_max   the run's first to last photo
    photos_min / photos_max           number of files
    located_min                       files with a position (own or a neighbour's)
    mostly_zone                       more than half of the files in that zone (home/local/away)
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
blocks they define and fall back to the defaults for the rest.
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
}
RULE_KEYS = {"name", "kind", "when", "name_template", "ongoing_days"}
EXCURSION_KEYS = {"split_gap_days": float, "split_distance_km": float, "local_gap_hours": float, "merge_devices": bool}
BURST_KEYS = {"gap_hours": float}
TEMPLATE_FIELDS = ("span", "places", "place", "media", "n", "home")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
BLOCKS = ("homes", "excursions", "excursion_rules", "bursts", "burst_rules")


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


def zone_at(cfg: Config, lat: float, lon: float) -> tuple[float | None, str, str | None]:
    """Distance to the nearest home (None without any), the zone there, that home's name. A
    point inside any home's radius is at home; beyond every home the zone follows the distance
    to the nearest one (local / away)."""
    best: tuple[float, str, bool] | None = None
    for h in homes(cfg):
        d = geo.haversine_km(h["lat"], h["lon"], lat, lon)
        if best is None or d < best[0]:
            best = (d, h["name"], d < h["radius_km"])
    if best is None:
        return None, geo.ZONE_UNKNOWN, None
    return best[0], geo.ZONE_HOME if best[2] else geo.zone_for(cfg, best[0]), best[1]


def defaults(cfg: Config) -> dict:
    """The rules the code applied before the engine, with the settings fields as parameters."""
    return {
        "homes": homes(cfg),
        "excursions": {"split_gap_days": float(cfg.trip_gap_days),
                       "split_distance_km": float(cfg.trip_split_distance_km),
                       "local_gap_hours": float(cfg.local_gap_hours), "merge_devices": True},
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
        "burst_rules": [
            {"name": "home occasion", "kind": "home",
             "when": {"zone": geo.ZONE_HOME, "photos_min": int(cfg.burst_min_photos),
                      "baseline_factor": float(cfg.burst_baseline_factor)},
             "name_template": "{span} ({media})"},
        ],
    }


def effective(cfg: Config) -> dict:
    """The ruleset in force: the custom blocks over the defaults; invalid custom rules are
    ignored (Settings refuses to save them, a hand-edited file is logged)."""
    base = defaults(cfg)
    custom = cfg.rules if isinstance(cfg.rules, dict) else {}
    if not custom:
        return base
    merged = {k: copy.deepcopy(custom[k]) if k in custom else base[k] for k in BLOCKS}
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
    b = rs.get("bursts")
    if b is not None:
        out += _check_block("bursts", b, BURST_KEYS)
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
            elif t is list and (not isinstance(v, list) or any(d not in WEEKDAYS for d in v)):
                out.append(f"{where}: weekday must be a list of weekday names")
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
        "weekday": a.strftime("%A"), "start_hour": a.hour, "end_hour": b.hour,
        "baseline": baseline, "start": a, "end": b, "_run": run,
    }


def matches(rule: dict, f: dict) -> bool:
    from . import cluster
    for k, v in (rule.get("when") or {}).items():
        if k == "span_hours_min" and not f["span_hours"] >= v:
            return False
        if k == "span_hours_max" and not f["span_hours"] < v:
            return False
        if k == "photos_min" and not f["photos"] >= v:
            return False
        if k == "photos_max" and not f["photos"] <= v:
            return False
        if k == "located_min" and not f["located"] >= v:
            return False
        if k == "mostly_zone" and not f["zones"].get(v, 0) > f["photos"] / 2:
            return False
        if k == "zone" and f["zone"] != v:
            return False
        if k == "baseline_factor" and not f["photos"] >= (f["baseline"] or 0.0) * v:
            return False
        if k == "devices_min":
            if f["devices"] is None:
                f["devices"] = cluster.count_devices(f["_run"])
            if not f["devices"] >= v:
                return False
        if k == "videos_min" and not f["videos"] >= v:
            return False
        if k == "weekday" and f["weekday"] not in v:
            return False
        if k == "start_hour_min" and not f["start_hour"] >= v:
            return False
        if k == "end_hour_max" and not f["end_hour"] <= v:
            return False
    return True


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
