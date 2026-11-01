"""The why panel of a proposal (issue #6): what a proposal stores about the rule that made it
(rules.explain, cluster._why), how that is put into words (rules.describe), and how the review
and cluster pages show it."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import cluster, config, main, mover, rules
from tests.synth import HOME, LISBON, LUDWIGSBURG, T0
from tests.test_cluster import h, rec

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


# --- what a proposal stores ----------------------------------------------------------------------

def test_a_day_out_stores_its_rule_the_rules_before_it_and_the_numbers(cfg):
    day = [rec(T0 + h(i / 4), "local", LUDWIGSBURG, "Ludwigsburg") for i in range(8)]
    day.insert(4, rec(T0 + h(0.8), "unknown", None))
    made, _ = cluster.automatic_proposals(cfg, day, NOW, 1.0)
    pr = next(iter(made.values()))
    why = pr["decision"]["why"]
    assert pr["decision"]["rule"] == "day out near home"
    assert why["when"] == {"photos_min": cfg.dayout_min_photos} and why["failed"] == []
    before = {b["rule"]: b["failed"] for b in why["before"]}
    assert list(before) == ["trip", "long run without positions", "day out far away", "far away with too few photos"]
    assert before["trip"] == {"span_hours_min": float(cfg.trip_min_hours)}           # too short for a trip
    assert before["day out far away"] == {"mostly_zone": "away"}
    assert why["facts"] == {"span_hours": 1.75, "photos": 9, "located": 8, "zones": {"local": 8, "unknown": 1},
                            "devices": 1, "videos": 0}
    gaps = why["gaps"]
    assert gaps["longest_h"] == 0.2 and gaps["step_km"] == 0.0 and gaps["longest_km"] == 0.0
    assert gaps["split_gap_days"] == cfg.trip_gap_days and gaps["local_gap_hours"] == cfg.local_gap_hours
    assert len(json.dumps(why)) < 700                                       # a few numbers per proposal


def test_a_trip_stores_its_longest_pause_with_the_distance_across_it(cfg):
    run = [rec(T0, "away", LISBON, "Lisbon"), rec(T0 + h(2), "away", (LISBON[0] + 1, LISBON[1]), "Lisbon"),
           rec(T0 + h(32), "away", LISBON, "Lisbon"), rec(T0 + h(40), "away", LISBON, "Lisbon")]
    pr = cluster.trip_proposal(cfg, run, NOW)
    why = pr["decision"]["why"]
    assert why["before"] == [] and why["failed"] == []
    assert why["gaps"]["longest_h"] == 30.0 and 100 < why["gaps"]["longest_km"] < 120
    assert why["gaps"]["step_km"] == why["gaps"]["longest_km"]
    blind = [rec(T0, "away", LISBON, "Lisbon"), rec(T0 + h(30), "unknown", None), rec(T0 + h(31), "away", LISBON)]
    gaps = cluster.trip_proposal(cfg, blind, NOW)["decision"]["why"]["gaps"]
    assert gaps["longest_km"] is None and gaps["step_km"] is None
    short = cluster.trip_proposal(cfg, run[:2], NOW)["decision"]["why"]   # a wrapper: its rule need not match
    assert set(short["failed"]) == {"span_hours_min", "located_min"}


def test_a_home_burst_stores_the_baseline_and_the_burst_gap(cfg):
    burst = [rec(T0 + h(i / 6), "home", HOME) for i in range(30)]
    made, _ = cluster.automatic_proposals(cfg, burst, NOW, 2.0)
    pr = next(iter(made.values()))
    why = pr["decision"]["why"]
    assert pr["kind"] == "home" and why["facts"]["baseline"] == 2.0 and why["facts"]["zone"] == "home"
    assert why["gaps"] == {"longest_h": 0.2, "gap_hours": cfg.burst_gap_hours}
    alone = cluster.home_proposal(cfg, burst, 5.0)["decision"]["why"]     # a wrapper call: no baseline known
    assert alone["facts"]["baseline"] is None


def test_a_custom_burst_rule_as_a_day_out_explains_itself_with_the_burst_gap(cfg):
    cfg = config.Config(**{**cfg.as_dict(), "rules": {
        "excursion_rules": [{"name": "none", "kind": "everyday", "when": {}}],
        "burst_rules": [{"name": "weekday", "kind": "local", "when": {"weekday": ["Monday"]}},
                        {"name": "any", "kind": "local", "when": {}}]}})
    sat = datetime(2026, 6, 27, 10, tzinfo=timezone.utc)
    run = [rec(sat + h(i), "local", LUDWIGSBURG, "Ludwigsburg") for i in range(3)]
    made, _ = cluster.automatic_proposals(cfg, run, NOW, 1.0, rules.effective(cfg))
    why = next(iter(made.values()))["decision"]["why"]
    assert why["when"] == {} and why["before"] == [{"rule": "weekday", "failed": {"weekday": ["Monday"]}}]
    assert why["facts"]["weekday"] == "Saturday" and why["gaps"] == {"longest_h": 1.0, "gap_hours": cfg.burst_gap_hours}


# --- in words -------------------------------------------------------------------------------------

FACTS = {"span_hours": 30.25, "photos": 12, "located": 1, "zones": {"away": 10, "unknown": 2}, "zone": "away",
         "baseline": 2.0, "devices": 2, "videos": 1, "weekday": "Saturday", "start_hour": 9, "end_hour": 18}


@pytest.mark.parametrize(("k", "v", "need", "have"), [
    ("span_hours_min", 24.0, "lasts at least 24 h", "30.2 h"),
    ("span_hours_max", 5, "lasts less than 5 h", "30.2 h"),
    ("photos_min", 1, "at least 1 file", "12 files"),
    ("photos_max", 50, "at most 50 files", "12 files"),
    ("located_min", 3, "at least 3 with a position", "1 with a position"),
    ("mostly_zone", "away", "more than half of the files far from home", "10 of 12"),
    ("zone", "home", "most files at home", "most far from home"),
    ("baseline_factor", 3, "at least 3x your usual day at home (6 files)", "12 files, usual day 2"),
    ("devices_min", 2, "at least 2 devices", "2 devices"),
    ("videos_min", 2, "at least 2 videos", "1 video"),
    ("weekday", ["Saturday", "Sunday"], "starts on Saturday, Sunday", "starts on Saturday"),
    ("start_hour_min", 8, "starts at 8:00 or later", "starts at 9:xx"),
    ("end_hour_max", 20, "ends by 20:00", "ends at 18:xx"),
    ("future_condition", 1, "future_condition 1", "?"),
])
def test_every_condition_reads_as_plain_words(k, v, need, have):
    assert rules.condition_text(k, v, FACTS) == (need, have)


def test_missing_facts_read_as_unknown():
    assert rules.condition_text("baseline_factor", 3, {"photos": 4}) == ("at least 3x your usual day at home",
                                                                         "4 files, usual day ?")
    assert rules.condition_text("mostly_zone", "local", {}) == ("more than half of the files near home", "0 of ?")


def test_describe_the_stored_numbers_and_old_or_hand_made_proposals():
    why = {"when": {"span_hours_min": 24.0, "located_min": 3}, "failed": ["located_min"], "facts": FACTS,
           "before": [{"rule": "big", "failed": {"photos_min": 100}}, {"rule": "odd", "failed": {}}],
           "gaps": {"longest_h": 20.0, "longest_km": 3.4, "step_km": 120.0, "split_gap_days": 3.0,
                    "split_distance_km": 50.0, "local_gap_hours": 10.0}}
    w = rules.describe({"rule": "trip", "note": "30 h away from home", "why": why})
    assert w["rule"] == "trip" and w["note"] == "30 h away from home"
    assert [c["ok"] for c in w["conditions"]] == [True, False]
    assert w["before"] == [{"rule": "big", "missed": ["at least 100 files: 12 files"]}, {"rule": "odd", "missed": []}]
    assert w["run"] == "30.2 h, 12 files, 1 video among them, from 2 devices; 10 far from home, 2 without a position"
    assert w["gaps"][0] == "Longest pause between two photos: 20 h, 3.4 km apart"
    assert w["gaps"][1] == "Biggest step between two photos in a row: 120 km"
    assert "more than 3 days between photos over 50 km apart, or of more than 10 h near home" in w["gaps"][2]
    burst = rules.describe({"why": {"when": {}, "facts": {"span_hours": 1.0, "photos": 1, "zones": {}},
                                    "gaps": {"longest_h": 0.5, "gap_hours": 3.0}}})
    assert burst["run"] == "1 h, 1 file"
    assert burst["gaps"] == ["Longest pause between two photos: 0.5 h",
                             "A burst ends at a pause of more than 3 h between two photos"]
    old = rules.describe({"by": "rule", "note": "an older proposal", "rule": "trip"})   # stored before the panel
    assert old["note"] == "an older proposal" and old["conditions"] == [] and old["run"] is None
    assert rules.describe({"by": "user", "conf": 1.0}) is None and rules.describe(None) is None


# --- on the pages -----------------------------------------------------------------------------------

@pytest.fixture
def client(library):
    main._state.update({"last_run": None, "last_stats": {}, "running": False, "error": None, "progress": None,
                        "runs": 0})
    with TestClient(main.app, follow_redirects=False) as c:
        yield c
    main._state.update({"running": False, "progress": None, "error": None})
    main._applying.clear()


def test_review_cards_carry_a_closed_why_panel_without_form_controls(client):
    cfg = config.load()
    cluster.run(cfg)
    props = {p["kind"]: p for p in cluster.load_proposals().values()}
    html = client.get("/review").text
    trip = html.split(f'id="{props["trip"]["id"]}"')[1].split('<div class="card"')[0]
    panel = trip.split('<div class="why small">')[1].split('<div class="row" style="margin-top:8px">')[0]
    assert 'data-why aria-expanded="false"' in panel and "why: rule “trip”" in panel
    assert '<div class="why-body" hidden>' in panel and "<input" not in panel and "<button" not in panel
    assert "lasts at least" in panel and "— this run:" in panel and "The run:" in panel
    assert "Longest pause between two photos" in panel and 'href="/settings"' in panel
    local = html.split(f'id="{props["local"]["id"]}"')[1].split('<div class="card"')[0]
    assert "Checked first, did not apply:" in local and "“trip”: lasts at least" in local
    home = html.split(f'id="{props["home"]["id"]}"')[1].split('<div class="card"')[0]
    assert "x your usual day at home" in home and "A burst ends at a pause" in home
    assert html.count("data-why") == len(cluster.load_proposals())


def test_a_rule_without_conditions_old_proposals_and_hand_made_ones(client):
    cfg = config.load()
    cluster.run(cfg)
    props = cluster.load_proposals()
    kinds = {p["kind"]: pid for pid, p in props.items()}
    props[kinds["trip"]]["decision"] = {"by": "rule", "conf": 1.0, "note": "an older proposal", "rule": "trip"}
    props[kinds["local"]]["decision"]["why"]["when"] = {}
    props[kinds["home"]]["decision"] = {"by": "user", "conf": 1.0}
    cluster.save_proposals(props)
    html = client.get("/review").text
    trip = html.split(f'id="{kinds["trip"]}"')[1].split('<div class="card"')[0]
    assert "an older proposal" in trip and "The rule applies" not in trip and "Settings</a>." not in trip
    local = html.split(f'id="{kinds["local"]}"')[1].split('<div class="card"')[0]
    assert "The rule has no conditions" in local
    home = html.split(f'id="{kinds["home"]}"')[1].split('<div class="card"')[0]
    assert "data-why" not in home
    pr = cluster.create_manual("local", "2026-05-24 Grillabend", cluster.records_filled(cfg)[:2])
    w = rules.describe(pr["decision"])
    assert w["note"] == "assembled by hand" and w["rule"] is None


def test_the_cluster_page_keeps_the_why_of_the_proposal(client):
    cfg = config.load()
    cluster.run(cfg)
    trip = next(p for p in cluster.load_proposals().values() if p["kind"] == "trip")
    mover.apply(cfg, trip, reviewed=True)
    html = client.get("/clusters/view", params={"folder": str(mover.target_folder(cfg, trip))}).text
    assert "why: rule “trip”" in html and "Longest pause between two photos" in html


def test_matches_still_reads_every_condition_once(cfg):
    run = [dict(rec(T0 + h(x), "local", LUDWIGSBURG, "Ludwigsburg"), source="a") for x in (0, 1)]
    f = rules.features(run)
    assert rules.holds("unknown_condition", 1, f) and rules.failed({"when": {"photos_min": 3}}, f) == {"photos_min": 3}
    assert f["devices"] is None and rules.holds("devices_min", 1, f) and f["devices"] == 1
    assert rules.explain([], {"name": "x", "kind": "local"}, dict(f, devices=None))["when"] == {}
