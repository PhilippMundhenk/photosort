"""The rule engine (issue #2) and several homes (issue #3).

The golden test holds the default ruleset to the code it replaced: tests/legacy_rules.py is
that code, frozen; both run over every synthetic library and many parameter sets and must
make identical proposals. The rest: custom rules, validation, multi-home zoning, the editor."""
from __future__ import annotations

import copy
import logging
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import cluster, config, geo, ingest, main, rules
from tests import legacy_rules as legacy
from tests import synth
from tests.synth import HOME, LISBON, LUDWIGSBURG, T0
from tests.test_cluster import h, rec

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


# --- golden: the defaults are the old code ----------------------------------------------------------

def _strip(props: dict) -> dict:
    out = copy.deepcopy(props)
    for pr in out.values():
        pr["decision"].pop("rule", None)
    return out


PARAMS = [
    {},
    {"trip_min_hours": 6, "trip_min_photos": 1, "dayout_min_photos": 3},
    {"trip_min_hours": 48, "dayout_min_photos": 2, "trip_min_photos": 5},      # day-out minimum below the trip one
    {"trip_gap_days": 0.5, "trip_split_distance_km": 50, "local_gap_hours": 4},
    {"burst_gap_hours": 1, "burst_min_photos": 4, "burst_baseline_factor": 1.5},
    {"burst_min_photos": 40, "burst_baseline_factor": 0.1, "name_multiday_by_month": False, "max_places_in_name": 1},
]


def _fuzz_timelines(n: int = 120) -> list[list[dict]]:
    rnd = random.Random(11)
    zones = [geo.ZONE_HOME, geo.ZONE_LOCAL, geo.ZONE_AWAY, geo.ZONE_UNKNOWN]
    out = []
    for _ in range(n):
        recs, t = [], datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(rnd.randint(0, 80)):
            t += timedelta(minutes=rnd.choice([1, 30, 600, 5000, 100000]))
            z = rnd.choice(zones)
            pos = None if z == geo.ZONE_UNKNOWN else (rnd.uniform(-90, 90), rnd.uniform(-180, 180))
            recs.append({"file": f"f{i}.jpg", "path": f"/in/{rnd.choice(['a', 'b'])}/f{i}.jpg", "ts": t.isoformat(),
                         "_t": t, "zone": z, "lat": pos[0] if pos else None, "lon": pos[1] if pos else None,
                         "gps_source": "exif" if pos else None, "source": rnd.choice(["a", "b", None]),
                         "place": rnd.choice([None, {"place": "X", "country": "Y"}, {"place": "Z", "country": "Y"}]),
                         "media": rnd.choice(["photo", "video"]), "camera": rnd.choice(["cam", None])})
        recs.sort(key=lambda r: r["_t"])
        out.append(recs)
    return out


def _same(cfg: config.Config, recs: list[dict]) -> int:
    baseline = cluster.home_baseline(recs, persist=False)
    old = legacy.proposals(cfg, recs, NOW, baseline)
    new, _ = cluster.automatic_proposals(cfg, recs, NOW, baseline)
    assert _strip(new) == old
    return len(old)


@pytest.mark.parametrize("params", PARAMS)
def test_golden_the_default_rules_are_the_old_code_on_the_synthetic_libraries(cfg, params):
    for busy in (False, True):
        lib_cfg = config.Config(**{**cfg.as_dict(), **params})
        synth.populate(lib_cfg, busy_day=busy)
        ingest.changed["all"] = True
        recs = cluster.records_filled(lib_cfg)
        assert len(recs) > 50
        n = _same(lib_cfg, recs)
        if not params:
            assert n >= 3                                        # trip, day out, birthday
        for lib in _fuzz_timelines(40):
            cluster.fill_gps_from_neighbours(lib_cfg, lib)
            _same(lib_cfg, lib)


@pytest.mark.parametrize("params", PARAMS[:4])
def test_golden_on_twenty_thousand_records(data_dir, params):
    from tests.test_perf_scale import _library_in_memory
    cfg = config.Config(home_lat=HOME[0], home_lon=HOME[1], **params)
    recs = _library_in_memory(20_000)
    cluster.fill_gps_from_neighbours(cfg, recs)
    assert _same(cfg, recs) >= 6


def test_default_rules_and_the_wrappers_agree(cfg):
    run = [rec(T0 + h(x), "away", LISBON, "Lisbon") for x in (0, 30, 40)]
    assert cluster.excursion_kind(cfg, run) == "trip"
    pr = cluster.trip_proposal(cfg, run, NOW)
    assert pr["kind"] == "trip" and pr["decision"]["rule"] == "trip" and pr["name"].startswith("2026-06")
    day = [rec(T0 + h(i / 4), "local", LUDWIGSBURG, "Ludwigsburg") for i in range(8)]
    assert cluster.excursion_kind(cfg, day) == "local"
    assert cluster.local_proposal(cfg, day, NOW)["decision"]["rule"] == "day out near home"
    few = [rec(T0 + h(i / 4), "local", LUDWIGSBURG, "Ludwigsburg") for i in range(3)]
    assert cluster.excursion_kind(cfg, few) is None
    far_few = [rec(T0 + h(i / 4), "away", LISBON, "Lisbon") for i in range(2)]
    assert cluster.excursion_kind(cfg, far_few) is None                     # the explicit everyday rule
    long_blind = [rec(T0, "away", LISBON, "Lisbon")] + [rec(T0 + h(i * 10), "unknown", None) for i in range(1, 4)]
    assert cluster.excursion_kind(cfg, long_blind) is None


# --- the ruleset: validation, parsing, merging -----------------------------------------------------------

def test_validation_names_every_problem():
    bad = {"homes": [{"lat": 1}], "excursions": {"split_gap_days": "four", "nope": 1},
           "bursts": {"gap_hours": True},
           "excursion_rules": [{"kind": "party"}, {"kind": "trip", "when": {"weekday": ["Funday"], "zone": "moon",
                                                                           "photos_min": "3", "wat": 1},
                                "name_template": "{nope}", "ongoing_days": "x", "extra": 1}, "rule"],
           "burst_rules": "none"}
    problems = rules.validate(bad)
    for needle in ("homes[0] needs lat and lon", "split_gap_days must be a number", "unknown key nope",
                   "gap_hours must be a number", "kind must be one of", "weekday must be a list",
                   "zone must be one of", "photos_min must be a number", "unknown condition wat",
                   "name_template can use", "ongoing_days must be a number", "unknown key extra",
                   "excursion_rules[2] must be a mapping", "burst_rules must be a list"):
        assert any(needle in p for p in problems), needle
    assert rules.validate({"homes": "x", "excursions": "y", "excursion_rules": [{"kind": "local", "when": "no"}]})
    assert rules.validate(rules.defaults(config.Config())) == []


def test_every_condition_can_fail_and_the_rest_of_the_validation_messages():
    sat = datetime(2026, 6, 27, 10, tzinfo=timezone.utc)
    run = [dict(rec(sat + h(x), "local", LUDWIGSBURG, "Ludwigsburg"), source="a", camera="cam") for x in (0, 1, 2)]
    f = rules.features(run, baseline=2.0)
    assert rules.matches({"when": {}}, f)
    for cond in ({"span_hours_max": 1}, {"photos_max": 2}, {"devices_min": 2}, {"videos_min": 1},
                 {"start_hour_min": 11}, {"end_hour_max": 11}, {"located_min": 4}, {"zone": "home"},
                 {"mostly_zone": "away"}, {"baseline_factor": 2}, {"weekday": ["Monday"]}, {"span_hours_min": 3}):
        assert not rules.matches({"when": cond}, f), cond
    assert rules.matches({"when": {"devices_min": 1, "videos_min": 0, "span_hours_max": 3, "photos_max": 3,
                                   "start_hour_min": 10, "end_hour_max": 12, "baseline_factor": 1.5}}, f)
    assert f["devices"] == 1                                                 # computed once, on demand
    problems = rules.validate({"excursions": {"merge_devices": "yes"},
                               "burst_rules": [{"kind": "home", "name_template": 5}]})
    assert any("must be true or false" in p for p in problems)
    assert any("name_template must be text" in p for p in problems)


def test_parse_rejects_what_it_cannot_use():
    with pytest.raises(ValueError, match="not valid YAML"):
        rules.parse("excursion_rules: [")
    with pytest.raises(ValueError, match="must be a mapping"):
        rules.parse("- a\n- b")
    with pytest.raises(ValueError, match="unknown block"):
        rules.parse("rulez: []")
    with pytest.raises(ValueError, match="kind must be"):
        rules.parse("burst_rules:\n  - {kind: trip, when: {}}")
    assert rules.parse("") == {}
    got = rules.parse("bursts: {gap_hours: 1}")
    assert got == {"bursts": {"gap_hours": 1}}
    assert yaml.safe_load(rules.to_yaml(rules.defaults(config.Config()))) == rules.defaults(config.Config())


def test_effective_lays_custom_blocks_over_the_defaults_and_ignores_broken_ones(caplog):
    cfg = config.Config(rules={"bursts": {"gap_hours": 1.0}})
    rs = rules.effective(cfg)
    assert rs["bursts"] == {"gap_hours": 1.0} and rs["excursion_rules"] == rules.defaults(cfg)["excursion_rules"]
    cfg.rules = {"excursions": {"split_gap_days": 9}}
    ex = rules.effective(cfg)["excursions"]
    assert ex["split_gap_days"] == 9 and ex["split_distance_km"] == cfg.trip_split_distance_km   # the rest kept
    cfg.rules = {"excursion_rules": [{"kind": "nope"}]}
    with caplog.at_level(logging.ERROR, logger="photosort.rules"):
        assert rules.effective(cfg) == rules.defaults(cfg)
    assert "custom rules ignored" in caplog.text
    cfg.rules = "text"
    assert rules.effective(cfg) == rules.defaults(cfg)


# --- custom rules --------------------------------------------------------------------------------------------

def _run_with(cfg, custom: dict):
    cfg = config.Config(**{**cfg.as_dict(), "rules": custom})
    return cfg, rules.effective(cfg)


def test_custom_conditions_weekday_devices_videos_hours_and_a_burst_as_a_day_out(cfg):
    sat = datetime(2026, 6, 27, 10, tzinfo=timezone.utc)                    # a Saturday
    burst = [dict(rec(sat + h(i / 6), "local", LUDWIGSBURG, "Ludwigsburg"), source="a" if i % 2 else "b",
                  camera=f"cam{i % 2}", media="video" if i < 2 else "photo") for i in range(12)]
    cfg, rs = _run_with(cfg, {"excursion_rules": [{"name": "none", "kind": "everyday", "when": {}}], "burst_rules": [
        {"name": "weekend outing", "kind": "local",
         "when": {"zone": "local", "photos_min": 10, "weekday": ["Saturday", "Sunday"], "devices_min": 2,
                  "videos_min": 2, "start_hour_min": 8, "end_hour_max": 20, "span_hours_max": 5, "photos_max": 50},
         "name_template": "{span} {place} with {n} files", "ongoing_days": 0},
        {"name": "home occasion", "kind": "home", "when": {"zone": "home", "photos_min": 12, "baseline_factor": 4},
         "name_template": "{span} ({media}) at {home}"}]})
    made, _ = cluster.automatic_proposals(cfg, burst, NOW, 1.0, rs)
    assert len(made) == 1
    pr = next(iter(made.values()))
    assert pr["kind"] == "local" and pr["decision"]["rule"] == "weekend outing"
    assert pr["name"] == "2026-06-27 Ludwigsburg with 12 files" and pr["status"] == "pending"
    mon = [dict(r, _t=r["_t"] + timedelta(days=2), ts=(r["_t"] + timedelta(days=2)).isoformat()) for r in burst]
    assert cluster.automatic_proposals(cfg, mon, NOW, 1.0, rs)[0] == {}   # a Monday: no rule matches
    home = [dict(rec(sat + h(i / 6), "home", HOME), home="Cabin") for i in range(14)]
    made, threshold = cluster.automatic_proposals(cfg, home, NOW, 1.0, rs)
    pr = next(iter(made.values()))
    assert pr["kind"] == "home" and pr["name"] == "2026-06-27 (14 Fotos) at Cabin" and threshold == 12.0


def test_an_everyday_rule_stops_the_search_and_a_ruleset_can_switch_a_kind_off(cfg, library):
    cfg, rs = _run_with(cfg, {"excursion_rules": [{"name": "nothing away", "kind": "everyday", "when": {}}],
                              "burst_rules": [{"name": "no occasions", "kind": "everyday", "when": {"zone": "home"}},
                                              {"name": "home occasion", "kind": "home",
                                               "when": {"zone": "home", "photos_min": 12, "baseline_factor": 4},
                                               "name_template": "{span} ({media})"}]})
    config.save(cfg)
    stats = cluster.run(cfg)
    assert stats["proposals"] == 0 and cluster.load_proposals() == {}
    two = [rec(T0, "away", LISBON, "Lisbon"), rec(T0 + h(30), "away", LISBON, "Lisbon")]
    assert cluster.excursion_kind(cfg, two) is None
    assert cluster.trip_proposal(cfg, two, NOW)["kind"] == "trip"


def test_the_neighbour_window_and_the_same_area_window_are_rule_parameters(cfg):
    blind = [rec(T0, "away", LISBON, "Lisbon"), rec(T0 + h(30), "unknown", None)]
    cfg.rules = {"excursions": {"neighbour_gps_hours": 10}}
    cluster.fill_gps_from_neighbours(cfg, blind)
    assert blind[1]["lat"] is None                                          # 30 h apart: too far now
    cfg.rules = {}
    cluster.fill_gps_from_neighbours(cfg, blind)
    assert blind[1]["lat"] == LISBON[0]                                      # the default 48 h
    a = [dict(rec(T0 + h(x), "away", LISBON, "Lisbon"), source="a") for x in (0, 1)]
    b = [dict(rec(T0 + h(x), "away", LISBON, "Lisbon"), source="b") for x in (30, 31)]
    cfg.rules = {"excursions": {"same_area_hours": 2, "local_gap_hours": 40}}
    assert len(cluster.find_excursions(cfg, a + b)) == 2                     # 30 h apart: not the same area
    cfg.rules = {"excursions": {"same_area_hours": 48, "local_gap_hours": 40}}
    assert len(cluster.find_excursions(cfg, a + b)) == 1
    assert rules.validate({"excursions": {"same_area_hours": "x"}})


def test_run_uses_custom_rules_from_the_config(cfg, library):
    cfg.rules = {"excursion_rules": [{"name": "any outing", "kind": "local", "when": {"photos_min": 1},
                                      "name_template": "Out {span}", "ongoing_days": 0}]}
    config.save(cfg)
    cluster.run(cfg)
    kinds = [p["kind"] for p in cluster.load_proposals().values()]
    assert "trip" not in kinds and kinds.count("local") >= 2
    assert all(p["name"].startswith("Out 2026") for p in cluster.load_proposals().values() if p["kind"] == "local")
    rs = rules.effective(cfg)
    rs["excursions"]["merge_devices"] = False
    assert cluster.find_excursions(cfg, cluster.records_filled(cfg), rs)


# --- several homes ----------------------------------------------------------------------------------------------

CABIN = (47.30, 11.10)


def test_a_second_home_is_home_and_ends_an_excursion(cfg):
    cfg.homes = [{"name": "Cabin", "lat": CABIN[0], "lon": CABIN[1], "radius_km": 1.0}, {"name": "broken", "lat": "x"},
                 {"lat": 95, "lon": 0}]
    assert [hm["name"] for hm in rules.homes(cfg)] == ["home", "Cabin"]
    d, zone, name = rules.zone_at(cfg, *CABIN)
    assert zone == geo.ZONE_HOME and name == "Cabin" and d == 0.0
    d, zone, name = rules.zone_at(cfg, CABIN[0] + 0.05, CABIN[1])            # ~5 km from the cabin
    assert zone == geo.ZONE_LOCAL and name == "Cabin" and 5 < d < 6
    assert rules.zone_at(cfg, *LISBON)[1] == geo.ZONE_AWAY and rules.zone_at(cfg, *HOME) == (0.0, geo.ZONE_HOME, "home")
    r = {"lat": CABIN[0], "lon": CABIN[1]}
    ingest.enrich_location(cfg, r)
    assert r["zone"] == geo.ZONE_HOME and r["home"] == "Cabin"
    ingest.enrich_location(cfg, {"lat": None, "lon": None})
    key_two = ingest.zoning_key(cfg)
    cfg.homes = []
    assert ingest.zoning_key(cfg) != key_two                                 # records are re-zoned on a change
    assert rules.zone_at(config.Config(), *HOME) == (None, geo.ZONE_UNKNOWN, None)   # no home at all
    # an excursion that ends at the cabin, not at the first home
    cfg.homes = [{"name": "Cabin", "lat": CABIN[0], "lon": CABIN[1], "radius_km": 1.0}]
    recs = [rec(T0, "home", HOME), rec(T0 + h(24), "away", LISBON), rec(T0 + h(48), "away", LISBON),
            rec(T0 + h(72), "home", CABIN), rec(T0 + h(80), "away", LISBON), rec(T0 + h(100), "away", LISBON)]
    for r in recs:
        ingest.enrich_location(cfg, r)
    assert [r["zone"] for r in recs] == ["home", "away", "away", "home", "away", "away"]
    runs = cluster.find_excursions(cfg, recs)
    assert [len(run) for run in runs] == [2, 2] and runs[0][0]["_t"] == T0 + h(24)


def test_settings_page_edits_homes_and_rules(client, cfg):
    html = client.get("/settings?mode=expert").text
    assert 'name="rules"' in html and 'name="homes"' in html and "rules in force now" in html
    assert "the rules built from the fields" in html
    form = {"settings_mode": "expert", "inbox_root": cfg.inbox_root, "root": cfg.root, "everyday_layout": "YYYY/MM",
            "timezone": cfg.timezone, "home_lat": str(cfg.home_lat), "home_lon": str(cfg.home_lon),
            "everyday_keep_days": "4", "dry_run": "on", "sidecar_mode": "central", "sidecar_name": cfg.sidecar_name,
            "scan_interval_min": "10", "homes": f"Cabin = {CABIN[0]}, {CABIN[1]}, 1\nnonsense line",
            "rules": ("bursts: {gap_hours: 2}\nburst_rules:\n"
                      "  - {name: party, kind: home, when: {zone: home, photos_min: 6}, name_template: '{span} p'}")}
    r = client.post("/settings", data=form)
    assert r.status_code == 303 and "Rules" not in r.headers["location"]
    saved = config.load()
    assert saved.homes == [{"name": "Cabin", "lat": CABIN[0], "lon": CABIN[1], "radius_km": 1.0}]
    assert saved.rules["bursts"] == {"gap_hours": 2} and saved.rules["burst_rules"][0]["name"] == "party"
    html = client.get("/settings?mode=expert").text
    assert "in force — the thresholds above do not apply" in html and "custom rules (the thresholds below" in html
    assert "1 more home" in html and "gap_hours: 2" in html
    r = client.post("/settings", data={**form, "rules": "excursion_rules: [{kind: nope}]", "scan_interval_min": "7"})
    assert "Rules%20not%20saved" in r.headers["location"] or "Rules not saved" in r.headers["location"]
    saved = config.load()
    assert saved.rules["bursts"] == {"gap_hours": 2} and saved.scan_interval_min == 7   # the rest was saved
    r = client.post("/settings", data={**form, "rules": "", "homes": ""})
    assert r.status_code == 303 and config.load().rules == {} and config.load().homes == []


@pytest.fixture
def client(cfg):
    main._state.update({"last_run": None, "last_stats": {}, "running": False, "error": None, "progress": None,
                        "runs": 0})
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def test_a_hand_edited_config_with_broken_rules_still_loads(data_dir):
    config.CONFIG_PATH.write_text("rules: [1, 2]\nhomes: notalist\n", encoding="utf-8")
    cfg = config.load()
    assert cfg.rules == {} and cfg.homes == []                               # coerced back to the defaults
    assert rules.effective(cfg) == rules.defaults(cfg)
    Path(config.CONFIG_PATH).unlink()
