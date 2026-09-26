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


@lru_cache(maxsize=50000)
def _lookup(lat3: float, lon3: float) -> dict:
    # cache on 3-decimal coordinates (~100 m) so bursts of photos hit the cache
    r = reverse_geocode.get((lat3, lon3))
    return r or {}


def reverse(cfg: Config, lat: float, lon: float) -> dict:
    """Return {city, region, country, country_code}. Villages below the population threshold
    fall back to the region name so folder names stay recognisable."""
    r = _lookup(round(lat, 3), round(lon, 3))
    city = r.get("city") or ""
    region = r.get("state") or r.get("county") or ""
    pop = r.get("population") or 0
    place = city if pop >= cfg.min_city_population or not region else region
    return {
        "city": city,
        "region": region,
        "place": place,
        "country": r.get("country") or "",
        "country_code": r.get("country_code") or "",
    }
