"""Two phones, one timeline each. Excursions are found per device and merged only when the
devices were in the same area at the same time; a GPS-less photo borrows its position from its
own device; and a proposal keeps its id (and with it the review state) when a photo that
synced late changes its first or last file."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from app import cluster, config, geo, ingest
from tests.synth import HOME, LISBON, RATINGEN, T0
from tests.test_cluster import h, rec

TOKYO = (35.68, 139.69)


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
        recs.append(dev(rec(t + h(10), geo.ZONE_AWAY, TOKYO, "Tokyo", "Tokyo",
                            file=f"b{d}.jpg"), "phone-b"))
    runs = cluster.find_excursions(cfg, _sorted(recs))
    assert len(runs) == 2                                              # not one "Lisbon, Tokyo" trip
    assert [{r["source"] for r in run} for run in runs] == [{"phone-a"}, {"phone-b"}]
    assert all(cluster.excursion_kind(cfg, run) == "trip" for run in runs)
    assert [cluster.places_label(cfg, run) for run in runs] == ["Lisbon", "Tokyo"]


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
    recs = [dev(rec(T0 + timedelta(days=d, hours=9), geo.ZONE_AWAY, TOKYO, "Tokyo", "Tokyo",
                    file=f"a{d}.jpg"), "phone-a") for d in range(7)]
    recs += [dev(rec(T0 + timedelta(days=2, hours=11, minutes=10 * i), geo.ZONE_LOCAL, RATINGEN, "Ratingen",
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
    cluster.fill_gps_from_neighbours(cfg, recs, interpolate_hours=0)        # the device rule alone
    assert (shot["lat"], shot["lon"]) == HOME and shot["zone"] == geo.ZONE_HOME  # nearest in time was phone-a
    assert shot["gps_source"] == "neighbour:b.jpg"
    assert (nogps["lat"], nogps["lon"]) == LISBON                                # no GPS ever: anyone's nearest


def test_camera_that_geotagged_once_still_borrows_the_phones_position():
    """A camera with one GPS'd photo (a Bluetooth geotag weeks ago) has no position of its own
    during the trip: its shots take the phone's, instead of staying unknown (everyday)."""
    cfg = config.Config(home_lat=HOME[0], home_lon=HOME[1])
    old = dev(rec(T0 - timedelta(days=60), geo.ZONE_HOME, HOME, file="cam-old.jpg"), "camera")
    trip = [dev(rec(T0 + timedelta(days=d, hours=9), geo.ZONE_AWAY, LISBON, "Lisbon", "Portugal",
                    file=f"p{d}.jpg"), "phone") for d in range(4)]
    shots = [dev(rec(T0 + timedelta(days=d, hours=12), geo.ZONE_UNKNOWN, file=f"c{d}.jpg"), "camera")
             for d in range(4)]
    back = dev(rec(T0 + timedelta(days=5, hours=9), geo.ZONE_HOME, HOME, file="p-home.jpg"), "phone")
    recs = _sorted([old, *trip, *shots, back])
    cluster.fill_gps_from_neighbours(cfg, recs)
    assert all(s["zone"] == geo.ZONE_AWAY and s["gps_source"].startswith("neighbour:p") for s in shots)
    runs = cluster.find_excursions(cfg, recs)
    assert len(runs) == 1 and {r["file"] for r in shots} <= {r["file"] for r in runs[0]}
    assert cluster.excursion_kind(cfg, runs[0]) == "trip"


def test_own_device_still_wins_when_it_has_a_position_nearby():
    cfg = config.Config(home_lat=HOME[0], home_lon=HOME[1])
    a = dev(rec(T0, geo.ZONE_AWAY, LISBON, file="a.jpg"), "phone-a")
    b = dev(rec(T0 + h(10), geo.ZONE_HOME, HOME, file="b.jpg"), "phone-b")
    shot = dev(rec(T0 + h(1), geo.ZONE_UNKNOWN, file="shot.jpg"), "phone-b")   # phone-a is closer in time
    recs = _sorted([a, b, shot])
    cluster.fill_gps_from_neighbours(cfg, recs)
    assert shot["gps_source"] == "neighbour:b.jpg" and shot["zone"] == geo.ZONE_HOME


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
    home["name"], home["name_edited"], home["status"] = "2026-06-29 Birthday Party", True, "approved"
    cluster.save_proposals(props)
    cluster.run(cfg)                                                   # a scan between approve and move
    again = cluster.load_proposals()[home["id"]]
    assert again["status"] == "approved" and again["name_edited"] is True
    assert mover.target_folder(cfg, again) == Path(cfg.root) / "2026-06-29 Birthday Party"


# --- interpolation between located photos (issue #22) ---------------------------------------------

COLOGNE, MUNICH = (50.938, 6.96), (48.14, 11.58)


def _camera_inbox(cfg: config.Config) -> Path:
    cam = Path(cfg.inboxes[0]["path"]).parent / "camera"
    cfg.inboxes = [*cfg.inboxes, {"path": str(cam), "name": "camera"}]
    config.save(cfg)
    return cam


def test_approved_trip_not_moved_yet_goes_back_to_review_when_a_camera_joins(cfg, library):
    """Phones first, approved (dry-run, or a move that failed on a read-only inbox); then the
    camera is added: its shots join the trip, and the trip is reviewed again instead of moving
    photos nobody looked at."""
    cluster.run(cfg)
    props = cluster.load_proposals()
    trip = next(p for p in props.values() if p["kind"] == "trip")
    trip.update(status="approved", name="2026-06 Portugal", name_edited=True, error="PermissionError: read-only")
    cluster.save_proposals(props)
    cam = _camera_inbox(cfg)
    start = datetime.fromisoformat(trip["start"])
    shots = [library.photo(cam, start + timedelta(days=d, hours=2), cam="ILCE-7M4") for d in range(3)]
    cluster.run(config.load())
    again = cluster.load_proposals()[trip["id"]]
    assert {str(s) for s in shots} <= {p["path"] for p in again["photos"]}
    assert again["status"] == "pending" and again["new_photos"] == 3 and "error" not in again
    assert again["name"] == "2026-06 Portugal"
    cluster.run(config.load())                                             # nothing new: stays as it is
    assert cluster.load_proposals()[trip["id"]]["new_photos"] == 3


def test_approved_trip_without_new_photos_stays_approved(cfg, library):
    cluster.run(cfg)
    props = cluster.load_proposals()
    trip = next(p for p in props.values() if p["kind"] == "trip")
    trip["status"] = "approved"
    cluster.save_proposals(props)
    cluster.run(cfg)
    again = cluster.load_proposals()[trip["id"]]
    assert again["status"] == "approved" and "new_photos" not in again


def test_auto_approved_trip_takes_new_photos_without_review(cfg, library):
    cluster.run(cfg)
    props = cluster.load_proposals()
    trip = next(p for p in props.values() if p["kind"] == "trip")
    trip.update(status="approved", auto=True)
    cluster.save_proposals(props)
    cam = _camera_inbox(cfg)
    library.photo(cam, datetime.fromisoformat(trip["start"]) + timedelta(hours=2), cam="ILCE-7M4")
    cluster.run(config.load())
    assert cluster.load_proposals()[trip["id"]]["status"] == "approved"


def test_camera_shot_between_two_phone_photos_is_interpolated_by_time():
    cfg = config.Config(home_lat=HOME[0], home_lon=HOME[1])
    a = dev(rec(T0, geo.ZONE_AWAY, COLOGNE, file="p1.jpg"), "phone")
    b = dev(rec(T0 + h(3), geo.ZONE_AWAY, MUNICH, file="p2.jpg"), "phone")
    shot = dev(rec(T0 + h(1), geo.ZONE_UNKNOWN, file="c.jpg"), "camera")      # a third of the way, in time
    recs = _sorted([a, b, shot])
    cluster.fill_gps_from_neighbours(cfg, recs)
    assert shot["gps_source"] == "interpolated:p1.jpg..p2.jpg" and shot["zone"] == geo.ZONE_AWAY
    total = geo.haversine_km(*COLOGNE, *MUNICH)
    assert abs(geo.haversine_km(*COLOGNE, shot["lat"], shot["lon"]) - total / 3) < 0.5
    assert abs(geo.haversine_km(shot["lat"], shot["lon"], *MUNICH) - 2 * total / 3) < 0.5
    assert cluster._gps_conf(shot) == 0.8 and cluster._photo_entry(shot, 0.8)["uncertain"] is False


def test_a_long_gap_or_the_setting_at_zero_takes_the_nearest_photo():
    cfg = config.Config(home_lat=HOME[0], home_lon=HOME[1])
    a = dev(rec(T0, geo.ZONE_AWAY, COLOGNE, file="p1.jpg"), "phone")
    b = dev(rec(T0 + h(10), geo.ZONE_AWAY, MUNICH, file="p2.jpg"), "phone")    # 10 h apart: a night, a flight
    shot = dev(rec(T0 + h(8), geo.ZONE_UNKNOWN, file="c.jpg"), "camera")
    cluster.fill_gps_from_neighbours(cfg, _sorted([a, b, shot]))
    assert shot["gps_source"] == "neighbour:p2.jpg" and (shot["lat"], shot["lon"]) == MUNICH
    cfg.gps_interpolation_hours = 12
    shot.update(lat=None, lon=None, gps_source=None)
    cluster.fill_gps_from_neighbours(cfg, _sorted([a, b, shot]))
    assert shot["gps_source"].startswith("interpolated:")
    cfg.gps_interpolation_hours = 0                                          # off
    shot.update(lat=None, lon=None, gps_source=None)
    cluster.fill_gps_from_neighbours(cfg, _sorted([a, b, shot]))
    assert shot["gps_source"] == "neighbour:p2.jpg"
    cfg.gps_interpolation_hours, cfg.rules = 6, {"excursions": {"interpolate_gps_hours": 12}}   # the rules' key
    shot.update(lat=None, lon=None, gps_source=None)
    cluster.fill_gps_from_neighbours(cfg, _sorted([a, b, shot]))
    assert shot["gps_source"].startswith("interpolated:")


def test_interpolation_uses_the_own_device_first_and_only_photos_on_both_sides():
    cfg = config.Config(home_lat=HOME[0], home_lon=HOME[1])
    own = dev(rec(T0, geo.ZONE_AWAY, COLOGNE, file="own.jpg"), "phone-b")
    other1 = dev(rec(T0 + h(0.5), geo.ZONE_AWAY, LISBON, file="o1.jpg"), "phone-a")
    other2 = dev(rec(T0 + h(1.5), geo.ZONE_AWAY, LISBON, file="o2.jpg"), "phone-a")
    shot = dev(rec(T0 + h(1), geo.ZONE_UNKNOWN, file="s.jpg"), "phone-b")    # its own phone has one side only
    cluster.fill_gps_from_neighbours(cfg, _sorted([own, other1, other2, shot]))
    assert shot["gps_source"] == "neighbour:own.jpg"
    first = dev(rec(T0 - h(1), geo.ZONE_UNKNOWN, file="f.jpg"), "camera")    # before every located photo
    exact = dev(rec(T0 + h(0.5), geo.ZONE_UNKNOWN, file="e.jpg"), "camera")  # the same second as one of them
    cluster.fill_gps_from_neighbours(cfg, _sorted([own, other1, other2, first, exact]))
    assert first["gps_source"] == "neighbour:own.jpg"
    assert exact["gps_source"] == "neighbour:o1.jpg" and (exact["lat"], exact["lon"]) == LISBON


def test_a_camera_on_a_drive_spreads_along_the_track_and_joins_the_day_out(cfg, library):
    """End to end: a camera inbox next to the synthetic library; its shot during a drive from
    Cologne to Munich that the phone recorded lands between the two cities, in the day out."""
    inbox = Path(cfg.inboxes[0]["path"])
    cam = _camera_inbox(cfg)
    day = datetime(2026, 7, 20, 8, tzinfo=T0.tzinfo)
    for i, pos in enumerate([COLOGNE, MUNICH, MUNICH]):
        library.photo(inbox, day + h(2 * i), pos)
    library.photo(inbox, day + timedelta(days=2), HOME)
    shot = library.photo(cam, day + h(1), cam="ILCE-7M4")
    cluster.run(config.load())
    trip = next(p for p in cluster.load_proposals().values() if str(shot) in {q["path"] for q in p["photos"]})
    entry = next(q for q in trip["photos"] if q["path"] == str(shot))
    assert trip["kind"] == "local" and entry["gps_source"].startswith("interpolated:")
    assert 6.96 < entry["lon"] < 11.58


def test_changing_the_interpolation_setting_refills_the_cached_positions(cfg, library):
    inbox = Path(cfg.inboxes[0]["path"])
    cam = _camera_inbox(cfg)
    day = datetime(2026, 7, 20, 8, tzinfo=T0.tzinfo)
    library.photo(inbox, day, COLOGNE)
    library.photo(inbox, day + h(4), MUNICH)
    shot = str(library.photo(cam, day + h(3), cam="ILCE-7M4"))
    cfg = config.load()
    by_path = {r["path"]: r for r in cluster.records_filled(cfg)}
    assert by_path[shot]["gps_source"].startswith("interpolated:")
    before = cluster.everyday_records(cfg)
    cfg.gps_interpolation_hours = 0
    by_path = {r["path"]: r for r in cluster.records_filled(cfg)}
    assert by_path[shot]["gps_source"].startswith("neighbour:")
    after = {r["path"]: r for r in cluster.everyday_records(cfg)}
    assert after[shot]["gps_source"].startswith("neighbour:") and before is not after


# --- per-inbox parameters (issue #6) -------------------------------------------------------------------

def test_an_inbox_can_have_its_own_home_radius_and_gps_window(tmp_path):
    near = (HOME[0] + 0.009, HOME[1])                                          # 1 km from home
    cfg = config.Config(home_lat=HOME[0], home_lon=HOME[1],
                        inbox_settings={"actioncam": {"home_radius_km": 2.0, "neighbour_gps_hours": 2.0}})
    tags = {"DateTimeOriginal": "2026:06:01 10:00:00", "GPSLatitude": near[0], "GPSLongitude": near[1]}
    assert ingest.build_record(cfg, tmp_path / "a.jpg", tags, source="phone")["zone"] == geo.ZONE_LOCAL
    assert ingest.build_record(cfg, tmp_path / "a.jpg", tags, source="actioncam")["zone"] == geo.ZONE_HOME
    assert ingest.zoning_key(cfg) != ingest.zoning_key(config.Config(home_lat=HOME[0], home_lon=HOME[1]))
    phone = dev(rec(T0, geo.ZONE_AWAY, LISBON, file="p.jpg"), "phone")
    cam = dev(rec(T0 + h(5), geo.ZONE_UNKNOWN, file="c.jpg"), "actioncam")      # 5 h later: outside its 2 h
    other = dev(rec(T0 + h(5), geo.ZONE_UNKNOWN, file="o.jpg"), "camera")       # default 48 h: borrows
    cluster.fill_gps_from_neighbours(cfg, _sorted([phone, cam, other]))
    assert cam["lat"] is None and other["gps_source"] == "neighbour:p.jpg"


def test_settings_show_and_save_the_per_device_options(cfg, library):
    from fastapi.testclient import TestClient

    from app import main
    with TestClient(main.app, follow_redirects=False) as client:
        page = client.get("/settings", params={"mode": "advanced"}).text
        assert 'name="inbox_settings"' in page and "borrow_offset=yes" in page
        form = {"inbox_settings": "phone-b: borrow_offset=yes, gps_hours=6"}
        client.post("/settings", data=form)
        assert config.load().inbox_settings == {"phone-b": {"borrow_offset": True, "neighbour_gps_hours": 6.0}}
        assert "phone-b: borrow_offset=yes, gps_hours=6" in client.get("/settings").text
