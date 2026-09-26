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
    assert geo.reverse(config.Config(), 47.861, 8.034)["place"] == "Bernau"     # 1993 people, default 1000
    assert geo.reverse(config.Config(min_city_population=20000), 47.861, 8.034)["place"] == "Baden-Wurttemberg"
    big = geo.reverse(config.Config(min_city_population=1), *HOME)
    huge = geo.reverse(config.Config(min_city_population=10_000_000), *HOME)
    assert big["city"] == huge["city"]
    assert big["place"] == big["city"]
    assert huge["place"] == huge["region"] and huge["region"]


def test_unknown_population_keeps_the_place_name(monkeypatch):
    cfg = config.Config(min_city_population=1000)
    monkeypatch.setattr(geo, "_lookup", lambda la, lo: {"city": "Hamlet", "state": "Baden-Wurttemberg",
                                                         "population": 0, "country": "Germany", "country_code": "DE"})
    assert geo.reverse(cfg, 48.0, 9.0)["place"] == "Hamlet"
    monkeypatch.setattr(geo, "_lookup", lambda la, lo: {"city": "Tiny", "state": "Baden-Wurttemberg",
                                                         "population": 400, "country": "Germany", "country_code": "DE"})
    assert geo.reverse(cfg, 48.0, 9.0)["place"] == "Baden-Wurttemberg"


def test_named_places_win_and_nearest_wins():
    cfg = config.Config(named_places=[{"name": "Harz", "lat": 51.77, "lon": 10.65, "radius_km": 30},
                                      {"name": "Brocken", "lat": 51.80, "lon": 10.62, "radius_km": 2}])
    assert geo.named_place(cfg, 51.80, 10.62) == "Brocken"            # inside both, nearer one
    assert geo.named_place(cfg, 51.70, 10.80) == "Harz"
    assert geo.named_place(cfg, *LISBON) is None
    assert geo.reverse(cfg, 51.70, 10.80)["place"] == "Harz"
    assert geo.reverse(cfg, 51.70, 10.80)["country"] == "Germany"      # the rest still comes from the geocoder


def test_circle_and_remember_place():
    assert geo.circle_for([]) is None
    lat, lon, r = geo.circle_for([(48.0, 9.0), (48.0, 9.1)])
    assert abs(lat - 48.0) < 1e-6 and abs(lon - 9.05) < 1e-6 and 3.9 < r < 4.3
    assert geo.circle_for([(48.0, 9.0)])[2] == 0.5                      # minimum radius
    pts = [(48.0 + i * 0.0001, 9.0) for i in range(100)] + [(49.0, 9.0)]   # one stray fix 111 km away
    assert geo.circle_for(pts)[2] < 2                                    # 95th percentile ignores it
    cfg = config.Config()
    e = geo.remember_place(cfg, " Harz ", [(51.7, 10.6), (51.8, 10.7), (None, None)])
    assert e["name"] == "Harz" and cfg.named_places == [e] and 5 < e["radius_km"] < 10
    geo.remember_place(cfg, "Harz", [(51.75, 10.65)])
    assert len(cfg.named_places) == 1 and cfg.named_places[0]["radius_km"] == 0.5   # replaced
    assert geo.remember_place(cfg, "", [(1, 1)]) is None


def test_detect_home_prefers_most_distinct_days():
    def r(lat, lon, day):
        return {"lat": lat, "lon": lon, "ts": f"2026-06-{day:02d}T10:00:00"}
    recs = [r(48.945, 9.1536, d) for d in range(1, 11)]                 # 10 photos on 10 days
    recs += [r(38.72, -9.14, 15)] * 50                                  # 50 holiday photos on one day
    recs += [{"lat": None, "lon": None, "ts": "2026-06-01T00:00:00"}]
    found = geo.detect_home(recs)
    assert (found["lat"], found["lon"], found["days"], found["photos"]) == (48.945, 9.154, 10, 10)
    assert geo.detect_home([]) is None


def test_reverse_result_is_cached_per_100m():
    geo._lookup.cache_clear()
    cfg = config.Config()
    geo.reverse(cfg, 38.72001, -9.14001)
    geo.reverse(cfg, 38.72004, -9.14004)      # rounds to the same 3-decimal key
    info = geo._lookup.cache_info()
    assert info.hits == 1 and info.misses == 1
