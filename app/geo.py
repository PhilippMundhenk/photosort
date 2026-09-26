"""Geo helpers: distance, zones, offline reverse geocoding."""
from __future__ import annotations

import math
from functools import lru_cache

import reverse_geocode  # pure python, offline, bundled city list

from .config import Config

ZONE_HOME, ZONE_LOCAL, ZONE_AWAY, ZONE_UNKNOWN = "home", "local", "away", "unknown"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def zone_for(cfg: Config, dist_km: float | None) -> str:
    if dist_km is None:
        return ZONE_UNKNOWN
    if dist_km < cfg.home_radius_km:
        return ZONE_HOME
    if dist_km < cfg.local_radius_km:
        return ZONE_LOCAL
    return ZONE_AWAY


def _town_radius_km(population: int) -> float:
    """How far from its centre a town still 'is' that town: 2 km for 50k people, scaling with
    the square root (Lisbon ~6 km, a 5k village ~1 km), clamped to 1-15 km."""
    return max(1.0, min(15.0, 2.0 * (max(population, 1) / 50_000) ** 0.5))


@lru_cache(maxsize=50000)
def _lookup(lat3: float, lon3: float) -> dict:
    """Nearest geonames place, except that a bigger town whose radius covers the spot wins:
    the geonames list contains city districts as separate 'cities' (central Lisbon resolves to
    'Intendente'), and a district must be labelled with its city, while a small town next to a
    big one must keep its own name. Cached on 3-decimal coordinates (~100 m)."""
    try:
        data = reverse_geocode.GeocodeData()
        _, idx = data._tree.query([(lat3, lon3)], k=15)
        cands = [dict(data._locations[i]) for i in idx[0] if i < len(data._locations)]
        for c in cands:
            c["country"] = data._countries.get(c["country_code"], "")
            c["distance_km"] = haversine_km(lat3, lon3, c["latitude"], c["longitude"])
    except Exception:  # noqa: BLE001  (library internals changed: fall back to the public API)
        r = reverse_geocode.get((lat3, lon3))
        return dict(r) if r else {}
    if not cands:
        return {}
    covering = [c for c in cands if c["distance_km"] <= _town_radius_km(c.get("population") or 0)]
    if covering:
        return max(covering, key=lambda c: c.get("population") or 0)
    return min(cands, key=lambda c: c["distance_km"])


def named_place(cfg: Config, lat: float, lon: float) -> str | None:
    """The user's own name for this spot, if inside one of the configured circles (nearest wins)."""
    best, best_d = None, None
    for p in cfg.named_places:
        d = haversine_km(lat, lon, p["lat"], p["lon"])
        if d <= p["radius_km"] and (best_d is None or d < best_d):
            best, best_d = p["name"], d
    return best


def reverse(cfg: Config, lat: float, lon: float) -> dict:
    """Return {city, region, place, country, country_code}. `place` is the label used in folder
    names: a user-named place if the spot is inside one, else the town (villages below the
    population threshold fall back to the region)."""
    r = _lookup(round(lat, 3), round(lon, 3))
    city = r.get("city") or ""
    region = r.get("state") or r.get("county") or ""
    pop = r.get("population") or 0
    place = named_place(cfg, lat, lon) or (city if pop >= cfg.min_city_population or not region else region)
    return {
        "city": city,
        "region": region,
        "place": place,
        "country": r.get("country") or "",
        "country_code": r.get("country_code") or "",
    }


def circle_for(points: list[tuple[float, float]], min_km: float = 0.5, max_km: float = 60.0) -> tuple | None:
    """Centre and radius (km) covering the points: the mean position and the distance to the
    farthest point (95th percentile when there are many, so one stray GPS fix does not blow it up)."""
    pts = [(la, lo) for la, lo in points if la is not None and lo is not None]
    if not pts:
        return None
    lat = sum(p[0] for p in pts) / len(pts)
    lon = sum(p[1] for p in pts) / len(pts)
    dists = sorted(haversine_km(lat, lon, la, lo) for la, lo in pts)
    r = dists[min(len(dists) - 1, int(len(dists) * 0.95))] if len(dists) >= 20 else dists[-1]
    return lat, lon, round(min(max_km, max(min_km, r * 1.1)), 3)


def remember_place(cfg: Config, name: str, points: list[tuple[float, float]]) -> dict | None:
    """Add (or replace) a named place covering the points; returns the entry. Saves nothing."""
    name = name.strip()
    c = circle_for(points)
    if not name or c is None:
        return None
    entry = {"name": name, "lat": round(c[0], 5), "lon": round(c[1], 5), "radius_km": c[2]}
    cfg.named_places = [p for p in cfg.named_places if p["name"] != name] + [entry]
    return entry


def detect_home(recs: list[dict]) -> dict | None:
    """Where photos are taken on the most distinct days: the ~100 m cell with the most days
    (ties: most photos). Returns {lat, lon, days, photos} or None without GPS."""
    days: dict[tuple, set] = {}
    counts: dict[tuple, int] = {}
    for r in recs:
        if r.get("lat") is None or r.get("lon") is None or not r.get("ts"):
            continue
        cell = (round(r["lat"], 3), round(r["lon"], 3))
        days.setdefault(cell, set()).add(str(r["ts"])[:10])
        counts[cell] = counts.get(cell, 0) + 1
    if not counts:
        return None
    best = max(counts, key=lambda c: (len(days[c]), counts[c]))
    return {"lat": best[0], "lon": best[1], "days": len(days[best]), "photos": counts[best]}
