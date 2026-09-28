"""Two phones, one timeline each. Excursions are found per device and merged only when the
devices were in the same area at the same time; a GPS-less photo borrows its position from its
own device; and a proposal keeps its id (and with it the review state) when a photo that
synced late changes its first or last file."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from app import cluster, config, geo
from tests.synth import HOME, LISBON, LUDWIGSBURG, T0
from tests.test_cluster import h, rec

SINGAPORE = (1.29, 103.85)


def dev(r: dict, source: str) -> dict:
    r["source"] = source
    return r


def _sorted(recs: list[dict]) -> list[dict]:
    return sorted(recs, key=lambda r: r["_t"])


def test_two_phones_far_apart_at_the_same_time_are_two_trips():
    cfg = config.Config()
    recs = []
    for d in range(5):
        t = T0 + timedelta(days=d)
        recs.append(dev(rec(t + h(9), geo.ZONE_AWAY, LISBON, "Lisbon", "Portugal", file=f"a{d}.jpg"), "phone-a"))
        recs.append(dev(rec(t + h(10), geo.ZONE_AWAY, SINGAPORE, "Singapore", "Singapore",
                            file=f"b{d}.jpg"), "phone-b"))
    runs = cluster.find_excursions(cfg, _sorted(recs))
    assert len(runs) == 2                                              # not one "Lisbon, Singapore" trip
    assert [{r["source"] for r in run} for run in runs] == [{"phone-a"}, {"phone-b"}]
    assert all(cluster.excursion_kind(cfg, run) == "trip" for run in runs)
    assert [cluster.places_label(cfg, run) for run in runs] == ["Lisbon", "Singapore"]


def test_home_photo_of_the_other_phone_does_not_end_a_trip():
    cfg = config.Config()
    recs = []
    for d in range(6):
        t = T0 + timedelta(days=d)
        recs.append(dev(rec(t + h(9), geo.ZONE_AWAY, LISBON, "Lisbon", "Portugal", file=f"a{d}.jpg"), "phone-a"))
        recs.append(dev(rec(t + h(18), geo.ZONE_HOME, HOME, file=f"b{d}.jpg"), "phone-b"))   # partner at home
    recs.append(dev(rec(T0 + timedelta(days=6, hours=9), geo.ZONE_HOME, HOME, file="a-home.jpg"), "phone-a"))
    runs = cluster.find_excursions(cfg, _sorted(recs))
    assert len(runs) == 1 and len(runs[0]) == 6                       # one trip, not six day-fragments
    assert cluster.excursion_kind(cfg, runs[0]) == "trip"


def test_two_phones_on_the_same_trip_are_one_trip():
    cfg = config.Config()
    recs = []
    for d in range(4):
        t = T0 + timedelta(days=d)
        recs.append(dev(rec(t + h(9), geo.ZONE_AWAY, LISBON, "Lisbon", "Portugal", file=f"a{d}.jpg"), "phone-a"))
        recs.append(dev(rec(t + h(15), geo.ZONE_AWAY, (38.71, -9.13), "Lisbon", "Portugal",
                            file=f"b{d}.jpg"), "phone-b"))
    recs.append(dev(rec(T0 + timedelta(days=4, hours=9), geo.ZONE_HOME, HOME, file="a-home.jpg"), "phone-a"))
    recs.append(dev(rec(T0 + timedelta(days=4, hours=12), geo.ZONE_HOME, HOME, file="b-home.jpg"), "phone-b"))
    runs = cluster.find_excursions(cfg, _sorted(recs))
    assert len(runs) == 1 and len(runs[0]) == 8
    assert {r["source"] for r in runs[0]} == {"phone-a", "phone-b"}
    assert [r["file"] for r in runs[0]][:3] == ["a0.jpg", "b0.jpg", "a1.jpg"]   # merged in time order


def test_day_out_at_home_while_the_other_phone_travels():
    cfg = config.Config()
    recs = [dev(rec(T0 + timedelta(days=d, hours=9), geo.ZONE_AWAY, SINGAPORE, "Singapore", "Singapore",
                    file=f"a{d}.jpg"), "phone-a") for d in range(7)]
    recs += [dev(rec(T0 + timedelta(days=2, hours=11, minutes=10 * i), geo.ZONE_LOCAL, LUDWIGSBURG, "Ludwigsburg",
                     "Germany", file=f"b{i}.jpg"), "phone-b") for i in range(15)]
    recs.append(dev(rec(T0 + timedelta(days=2, hours=20), geo.ZONE_HOME, HOME, file="b-home.jpg"), "phone-b"))
    runs = cluster.find_excursions(cfg, _sorted(recs))
    assert sorted(cluster.excursion_kind(cfg, run) for run in runs) == ["local", "trip"]


def test_single_device_behaves_as_before():
    cfg = config.Config()
    t = T0
    recs = [rec(t, geo.ZONE_HOME, HOME), rec(t + h(5), geo.ZONE_AWAY, LISBON), rec(t + h(30), geo.ZONE_AWAY, LISBON),
            rec(t + h(90), geo.ZONE_HOME, HOME), rec(t + h(100), geo.ZONE_AWAY, LISBON)]
    runs = cluster.find_excursions(cfg, recs)                          # no `source` at all: one timeline
    assert [len(run) for run in runs] == [2, 1]


def test_gpsless_photo_borrows_from_its_own_phone():
    cfg = config.Config(home_lat=HOME[0], home_lon=HOME[1])
    a = dev(rec(T0, geo.ZONE_AWAY, LISBON, file="a.jpg"), "phone-a")
    b = dev(rec(T0 + h(1), geo.ZONE_HOME, HOME, file="b.jpg"), "phone-b")
    shot = dev(rec(T0 + h(0.5), geo.ZONE_UNKNOWN, file="shot.jpg"), "phone-b")   # screenshot at home, phone-b
    nogps = dev(rec(T0 + h(0.2), geo.ZONE_UNKNOWN, file="c.jpg"), "phone-c")      # a phone that never has GPS
    recs = _sorted([a, b, shot, nogps])
    cluster.fill_gps_from_neighbours(cfg, recs)
    assert (shot["lat"], shot["lon"]) == HOME and shot["zone"] == geo.ZONE_HOME  # nearest in time was phone-a
    assert shot["gps_source"] == "neighbour:b.jpg"
    assert (nogps["lat"], nogps["lon"]) == LISBON                                # no GPS ever: anyone's nearest


def test_late_photo_at_the_edge_keeps_proposal_id_and_review_state(cfg, library):
    cluster.run(cfg)
    props = cluster.load_proposals()
    trip = next(p for p in props.values() if p["kind"] == "trip")
    trip["excluded"] = [trip["photos"][3]["path"]]
    trip["name"], trip["name_edited"] = "2026-06 Portugal", True
    cluster.save_proposals(props)
    # a photo from the last trip day syncs late: the last file, part of the id hash, changes
    end = datetime.fromisoformat(trip["end"])
    library.photo(Path(cfg.inboxes[0]["path"]), end + timedelta(hours=2), LISBON)
    cluster.run(cfg)
    props = cluster.load_proposals()
    assert trip["id"] in props
    again = props[trip["id"]]
    assert again["n"] == trip["n"] + 1
    assert again["excluded"] == trip["excluded"] and again["name"] == "2026-06 Portugal"


def test_identity_needs_at_least_half_of_the_old_proposal(cfg, library):
    cluster.run(cfg)
    props = cluster.load_proposals()
    trip = next(p for p in props.values() if p["kind"] == "trip")
    local = next(p for p in props.values() if p["kind"] == "local")
    old = {trip["id"]: dict(trip, status="pending"), local["id"]: dict(local, status="approved")}
    n = len(trip["photos"])
    new = {"tXXXXXXXXXX": dict(trip, id="tXXXXXXXXXX", photos=trip["photos"][:2] + local["photos"][:1])}
    cluster._keep_identities(old, new)
    assert set(new) == {"tXXXXXXXXXX"}                                  # 2 of 67: a different thing
    # the old trip split in two by a late home photo: the larger half keeps the id
    new = {"tAAAAAAAAAA": dict(trip, id="tAAAAAAAAAA", photos=trip["photos"][: n // 3]),
           "tBBBBBBBBBB": dict(trip, id="tBBBBBBBBBB", photos=trip["photos"][n // 3:])}
    cluster._keep_identities(old, new)
    assert set(new) == {"tAAAAAAAAAA", trip["id"]} and new[trip["id"]]["id"] == trip["id"]
    assert len(new[trip["id"]]["photos"]) == n - n // 3
    # a manual proposal keeps its own id whatever it contains
    new = {"m123": dict(trip, id="m123", manual=True)}
    cluster._keep_identities(old, new)
    assert set(new) == {"m123"}
    assert json.loads(cluster.PROPOSALS_PATH.read_text(encoding="utf-8")).keys() == props.keys()   # nothing saved


def test_named_burst_keeps_its_name_flag_through_a_run_after_approval(cfg, library):
    """Approve a burst you named, let a scan run before the worker moves it: the target must
    still be <root>/<name>, not _unnamed/<name> (the flag was lost in the carry-over)."""
    from app import mover
    cluster.run(cfg)
    props = cluster.load_proposals()
    home = next(p for p in props.values() if p["kind"] == "home")
    home["name"], home["name_edited"], home["status"] = "2026-06-29 Hannas Geburtstag", True, "approved"
    cluster.save_proposals(props)
    cluster.run(cfg)                                                   # a scan between approve and move
    again = cluster.load_proposals()[home["id"]]
    assert again["status"] == "approved" and again["name_edited"] is True
    assert mover.target_folder(cfg, again) == Path(cfg.root) / "2026-06-29 Hannas Geburtstag"
