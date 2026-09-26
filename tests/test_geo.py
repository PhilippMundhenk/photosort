from app import config, geo
from tests.synth import HOME, LISBON, LUDWIGSBURG, SEVILLE


def test_haversine_known_distances():
    assert geo.haversine_km(*HOME, *HOME) == 0
    d = geo.haversine_km(*LISBON, *SEVILLE)
    assert 300 < d < 320                      # Lisbon - Seville is ~310 km
    assert 1800 < geo.haversine_km(*HOME, *LISBON) < 1900
    assert geo.haversine_km(*HOME, *LUDWIGSBURG) < 10


def test_zone_thresholds():
    cfg = config.Config(home_radius_km=0.5, local_radius_km=20)
    assert geo.zone_for(cfg, None) == geo.ZONE_UNKNOWN
    assert geo.zone_for(cfg, 0.0) == geo.ZONE_HOME
    assert geo.zone_for(cfg, 0.499) == geo.ZONE_HOME
    assert geo.zone_for(cfg, 0.5) == geo.ZONE_LOCAL
    assert geo.zone_for(cfg, 19.99) == geo.ZONE_LOCAL
    assert geo.zone_for(cfg, 20.0) == geo.ZONE_AWAY


def test_reverse_geocode_cities():
    cfg = config.Config()
    lis = geo.reverse(cfg, *LISBON)
    assert lis["place"] == "Lisbon" and lis["country"] == "Portugal" and lis["country_code"] == "PT"
    sev = geo.reverse(cfg, *SEVILLE)
    assert sev["country"] == "Spain" and sev["place"]
    lb = geo.reverse(cfg, *LUDWIGSBURG)
    assert lb["place"] == "Ludwigsburg" and lb["country"] == "Germany"


def test_reverse_small_places_fall_back_to_region():
    big = geo.reverse(config.Config(min_city_population=1), *HOME)
    huge = geo.reverse(config.Config(min_city_population=10_000_000), *HOME)
    assert big["city"] == huge["city"]
    assert big["place"] == big["city"]
    assert huge["place"] == huge["region"] and huge["region"]


def test_reverse_result_is_cached_per_100m():
    geo._lookup.cache_clear()
    cfg = config.Config()
    geo.reverse(cfg, 38.72001, -9.14001)
    geo.reverse(cfg, 38.72004, -9.14004)      # rounds to the same 3-decimal key
    info = geo._lookup.cache_info()
    assert info.hits == 1 and info.misses == 1
