from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import cluster, config, geo, ingest
from tests.synth import HOME, LISBON, LUDWIGSBURG, SEVILLE, T0

Z = geo


def rec(t: datetime, zone: str, pos=None, place=None, country=None, file=None, gps="exif") -> dict:
    return {"file": file or f"{t:%Y%m%d%H%M}.jpg", "path": f"/in/{file or t.isoformat()}", "ts": t.isoformat(),
            "_t": t, "zone": zone, "lat": pos[0] if pos else None, "lon": pos[1] if pos else None,
            "gps_source": gps if pos else None, "camera": "cam",
            "place": {"place": place, "country": country} if place else None}


def h(n: float) -> timedelta:
    return timedelta(hours=n)


# --- labels -------------------------------------------------------------------------

def test_span_label_variants():
    a = datetime(2026, 6, 4, 9)
    assert cluster.span_label(a, a + h(5)) == "2026-06-04"
    assert cluster.span_label(a, a + timedelta(days=20)) == "2026-06"                    # same month: month only
    assert cluster.span_label(a, a + timedelta(days=20), month_only=False) == "2026-06-04..24"
    assert cluster.span_label(a, a + timedelta(days=1)) == "2026-06"
    assert cluster.span_label(a, a + timedelta(days=40)) == "2026-06-04..07-14"          # across months: full range
    assert cluster.span_label(a, datetime(2027, 1, 2)) == "2026-06-04..2027-01-02"


def test_sanitize_strips_forbidden_characters():
    assert cluster.sanitize('2026-06 Lisbon: "a/b"?. ') == "2026-06 Lisbon- -a-b-"
    assert cluster.sanitize("..hidden") == "hidden"


def test_places_label_cities_then_countries_then_multiple():
    cfg = config.Config(max_places_in_name=4)
    t = T0
    recs = [rec(t, Z.ZONE_AWAY, LISBON, "Lisbon", "Portugal"), rec(t, Z.ZONE_AWAY, SEVILLE, "Sevilla", "Spain"),
            rec(t, Z.ZONE_AWAY, LISBON, "Lisbon", "Portugal"), rec(t, Z.ZONE_AWAY, SEVILLE, "Sevilla", "Spain")]
    assert cluster.places_label(cfg, recs) == "Lisbon, Sevilla"        # deduplicated, first appearance
    noisy = [rec(t, Z.ZONE_AWAY, LISBON, "Lisbon", "Portugal")] * 60 + [rec(t, Z.ZONE_AWAY, SEVILLE, "Stop", "Spain")]
    assert cluster.places_label(cfg, noisy) == "Lisbon"                 # one photo at a stop is dropped
    assert cluster.places_label(cfg, noisy + [noisy[-1]] * 2) == "Lisbon, Stop"   # three of 63: kept
    five = [rec(t, Z.ZONE_AWAY, LISBON, f"City{i}", "Portugal" if i < 3 else "Spain") for i in range(5)]
    assert cluster.places_label(cfg, five) == "Portugal, Spain"
    many = [rec(t, Z.ZONE_AWAY, LISBON, f"C{i}", f"Country{i}") for i in range(6)]
    assert cluster.places_label(cfg, many) == "Multiple"
    assert cluster.places_label(cfg, [rec(t, Z.ZONE_AWAY, LISBON)]) == "Unknown"


# --- excursions ------------------------------------------------------------------------

def test_excursion_run_ends_at_any_home_photo_and_carries_gpsless():
    cfg = config.Config()
    t = T0
    recs = [rec(t, Z.ZONE_HOME, HOME), rec(t + h(1), Z.ZONE_UNKNOWN),   # GPS-less at home: not a run
            rec(t + h(2), Z.ZONE_LOCAL, HOME),                        # station on the way out: part of the run
            rec(t + h(5), Z.ZONE_AWAY, LISBON), rec(t + h(30), Z.ZONE_UNKNOWN), rec(t + h(50), Z.ZONE_AWAY, LISBON),
            rec(t + h(70), Z.ZONE_AWAY, LISBON), rec(t + h(80), Z.ZONE_LOCAL, HOME), rec(t + h(81), Z.ZONE_UNKNOWN),
            rec(t + h(90), Z.ZONE_HOME, HOME),                        # a single home photo ends the run
            rec(t + h(100), Z.ZONE_AWAY, SEVILLE), rec(t + h(101), Z.ZONE_AWAY, SEVILLE)]
    runs = cluster.find_excursions(cfg, recs)
    assert [[r["zone"] for r in run] for run in runs] == [["local", "away", "unknown", "away", "away", "local"],
                                                          ["away", "away"]]
    assert cluster.excursion_kind(cfg, runs[0]) == "trip"           # 78 h
    assert cluster.excursion_kind(cfg, runs[1]) is None             # 1 h, 2 photos: everyday


def test_excursion_kind_by_duration_and_size():
    cfg = config.Config(trip_min_hours=20, trip_min_photos=3, dayout_min_photos=8)
    t = T0
    day_out = [rec(t + h(i), Z.ZONE_LOCAL, LUDWIGSBURG) for i in range(8)]          # 7 h, 8 photos
    assert cluster.excursion_kind(cfg, day_out) == "local"
    assert cluster.excursion_kind(cfg, day_out[:7]) is None                          # too few for a day out
    far_day = [rec(t + h(i * 2), Z.ZONE_AWAY, LISBON) for i in range(9)]             # 16 h, far away: still a day out
    assert cluster.excursion_kind(cfg, far_day) == "local"
    three_far = [rec(t + h(i * 0.05), Z.ZONE_AWAY, LISBON) for i in range(3)]          # 3 photos 30 km out: an outing
    assert cluster.excursion_kind(cfg, three_far) == "local"
    assert cluster.excursion_kind(cfg, three_far[:2]) is None
    three_near = [rec(t + h(i * 0.05), Z.ZONE_LOCAL, LUDWIGSBURG) for i in range(3)]   # 3 in the next town: noise
    assert cluster.excursion_kind(cfg, three_near) is None
    assert cluster.excursion_kind(cfg, three_far + three_near) is None                # half away: not mostly away
    overnight = [rec(t, Z.ZONE_LOCAL, LUDWIGSBURG), rec(t + h(10), Z.ZONE_UNKNOWN),
                 rec(t + h(25), Z.ZONE_LOCAL, LUDWIGSBURG)]
    assert cluster.excursion_kind(cfg, overnight) is None                            # 2 located photos < 3
    overnight.append(rec(t + h(26), Z.ZONE_LOCAL, LUDWIGSBURG))
    assert cluster.excursion_kind(cfg, overnight) == "trip"                          # near home, but overnight


def test_excursion_split_needs_long_gap_and_distance():
    cfg = config.Config(trip_gap_days=4, trip_split_distance_km=300)
    t = T0
    same_area = [rec(t, Z.ZONE_AWAY, LISBON), rec(t + timedelta(days=10), Z.ZONE_AWAY, LISBON)]
    assert len(cluster.find_excursions(cfg, same_area)) == 1
    far_quick = [rec(t, Z.ZONE_AWAY, LISBON), rec(t + timedelta(days=1), Z.ZONE_AWAY, SEVILLE)]
    assert len(cluster.find_excursions(cfg, far_quick)) == 1
    far_slow = [rec(t, Z.ZONE_AWAY, LISBON), rec(t + timedelta(days=10), Z.ZONE_AWAY, SEVILLE)]
    assert len(cluster.find_excursions(cfg, far_slow)) == 2
    near_home = [rec(t, Z.ZONE_LOCAL, LUDWIGSBURG), rec(t + timedelta(days=10), Z.ZONE_LOCAL, LUDWIGSBURG)]
    assert len(cluster.find_excursions(cfg, near_home)) == 2              # long gap near home: two outings
    two_days = [rec(t, Z.ZONE_LOCAL, LUDWIGSBURG), rec(t + timedelta(days=2), Z.ZONE_LOCAL, LUDWIGSBURG)]
    assert len(cluster.find_excursions(cfg, two_days)) == 2               # two evenings, no home photo between
    overnight = [rec(t, Z.ZONE_AWAY, LISBON), rec(t + h(20), Z.ZONE_LOCAL, HOME)]
    assert len(cluster.find_excursions(cfg, overnight)) == 2              # day out, then a clip near home next day
    evening = [rec(t, Z.ZONE_LOCAL, LUDWIGSBURG), rec(t + h(11), Z.ZONE_LOCAL, LUDWIGSBURG)]
    assert len(cluster.find_excursions(cfg, evening)) == 1                # a long day out stays one
    far_night = [rec(t, Z.ZONE_AWAY, LISBON), rec(t + h(20), Z.ZONE_AWAY, LISBON)]
    assert len(cluster.find_excursions(cfg, far_night)) == 1              # away from home, nights do not split


def test_trip_proposal_confidences_and_ongoing():
    cfg = config.Config(trip_gap_days=4)
    t = T0
    run = [rec(t, Z.ZONE_AWAY, LISBON, "Lisbon", "Portugal"),
           rec(t + h(1), Z.ZONE_AWAY, LISBON, "Lisbon", gps="neighbour:x"),
           rec(t + h(2), Z.ZONE_UNKNOWN), rec(t + timedelta(days=3), Z.ZONE_AWAY, SEVILLE, "Sevilla", "Spain")]
    pr = cluster.trip_proposal(cfg, run, now=t + timedelta(days=30))
    assert pr["kind"] == "trip" and pr["name"] == "2026-06 Lisbon, Sevilla" and pr["status"] == "pending"
    cfg.name_multiday_by_month = False
    assert cluster.trip_proposal(cfg, run, now=t + timedelta(days=30))["name"] == "2026-06-01..04 Lisbon, Sevilla"
    cfg.name_multiday_by_month = True
    assert [p["conf"] for p in pr["photos"]] == [1.0, 0.8, 0.6, 1.0]
    assert pr["photos"][0]["lat"] == LISBON[0]
    assert pr["n"] == 4 and pr["n_uncertain"] == 1 and pr["photos"][2]["uncertain"]
    assert pr["id"].startswith("t") and pr["id"] == cluster.trip_proposal(cfg, run, now=t)["id"]
    assert cluster.trip_proposal(cfg, run, now=t + timedelta(days=5))["status"] == "ongoing"


def test_a_recent_trip_ended_by_a_home_photo_is_not_ongoing():
    """The badge says "waiting for a home photo": with one after the trip it is pending, even
    the day after; a home photo of another device does not end it."""
    cfg = config.Config(trip_gap_days=4, home_lat=HOME[0], home_lon=HOME[1])
    t = T0
    trip = [{**rec(t + timedelta(days=d), Z.ZONE_AWAY, LISBON, "Lisbon", "Portugal", file=f"a{d}.jpg"),
             "source": "phone-a"} for d in range(3)]
    partner = {**rec(t + timedelta(days=3), Z.ZONE_HOME, HOME, file="b.jpg"), "source": "phone-b"}
    now = t + timedelta(days=3, hours=12)
    made, _ = cluster.automatic_proposals(cfg, [*trip, partner], now, baseline=1.0)
    assert [p["status"] for p in made.values() if p["kind"] == "trip"] == ["ongoing"]
    back = {**rec(t + timedelta(days=3), Z.ZONE_HOME, HOME, file="a-home.jpg"), "source": "phone-a"}
    made, _ = cluster.automatic_proposals(cfg, [*trip, partner, back], now, baseline=1.0)
    assert [p["status"] for p in made.values() if p["kind"] == "trip"] == ["pending"]


# --- bursts --------------------------------------------------------------------------

def test_group_bursts_and_baseline():
    cfg = config.Config(burst_gap_hours=3)
    t = T0
    recs = [rec(t, Z.ZONE_HOME), rec(t + h(1), Z.ZONE_HOME), rec(t + h(4.5), Z.ZONE_HOME),
            rec(t + h(6), Z.ZONE_HOME), rec(t + timedelta(days=1), Z.ZONE_LOCAL)]
    assert [len(b) for b in cluster.group_bursts(cfg, recs)] == [2, 2, 1]
    assert cluster.home_baseline(recs, persist=False) == 4                # one day with 4 home photos -> median 4
    assert cluster.home_baseline([rec(t, Z.ZONE_AWAY)], persist=False) == 1.0


def test_home_baseline_is_remembered_across_moves(data_dir):
    t = T0
    days = [rec(t + timedelta(days=d), Z.ZONE_HOME, HOME) for d in range(10)]            # 1 photo/day
    burst = [rec(t + timedelta(days=20, hours=i * 0.1), Z.ZONE_HOME, HOME) for i in range(25)]
    assert cluster.home_baseline(days + burst) == 1                                      # median of 11 days
    assert cluster.home_baseline(burst) == 1                                             # everyday moved out: same
    assert cluster.home_baseline([]) == 1
    assert cluster.BASELINE_PATH.exists()
    cluster.BASELINE_PATH.write_text("{broken", encoding="utf-8")
    assert cluster.home_baseline(burst) == 25                                            # unreadable: recomputed


def test_fill_gps_from_neighbours_respects_48h_window():
    cfg = config.Config(home_lat=HOME[0], home_lon=HOME[1])
    t = T0
    recs = [rec(t, Z.ZONE_AWAY, LISBON), rec(t + h(10), Z.ZONE_UNKNOWN), rec(t + h(80), Z.ZONE_UNKNOWN),
            rec(t + h(130), Z.ZONE_UNKNOWN), rec(t + h(140), Z.ZONE_HOME, HOME)]
    cluster.fill_gps_from_neighbours(cfg, recs)
    assert recs[1]["zone"] == Z.ZONE_AWAY and recs[1]["gps_source"].startswith("neighbour:")
    assert recs[2]["zone"] == Z.ZONE_UNKNOWN and recs[2]["lat"] is None      # 80 h from Lisbon, 60 h from home
    assert recs[3]["zone"] == Z.ZONE_HOME                                    # 10 h from the home photo
    cluster.fill_gps_from_neighbours(cfg, [rec(t, Z.ZONE_UNKNOWN)])          # nothing with GPS: no-op


def test_home_state_summary():
    cfg = config.Config()
    t = datetime(2026, 6, 29, 14, 0, tzinfo=timezone.utc)                    # a Monday
    burst = [{**rec(t + h(i * 0.15), Z.ZONE_HOME, HOME), "camera": "a" if i % 2 else "b"} for i in range(25)]
    st = cluster.home_state(cfg, burst, threshold=12)
    assert st == {"photos": 25, "videos": 0, "duration_h": 3.6, "weekday": "Monday", "start_hour": 14,
                  "end_hour": 17, "devices": 2, "burst_ratio": 2.08, "date": "2026-06-29"}


# --- run() -----------------------------------------------------------------------------

def test_run_produces_trip_dayout_and_occasion(cfg, library):
    stats = cluster.run(cfg)
    props = cluster.load_proposals()
    kinds = {p["kind"]: p for p in props.values()}
    assert set(kinds) == {"trip", "local", "home"} and stats["proposals"] == 3
    assert kinds["trip"]["name"] == "2026-06 Lisbon, Sevilla" and kinds["trip"]["n"] == 67
    assert kinds["local"]["name"] == "2026-06-27 Ludwigsburg" and kinds["local"]["n"] == 15
    assert kinds["home"]["name"] == "2026-06-30 (25 Fotos)" and kinds["home"]["decision"]["by"] == "rule"
    assert stats["photos"] == library.n and stats["everyday"] == library.n - 67 - 15 - 25
    assert stats["no_timestamp"] == 0 and stats["threshold"] == 12


def test_run_keeps_review_state(cfg, library):
    cluster.run(cfg)
    props = cluster.load_proposals()
    home = next(p for p in props.values() if p["kind"] == "home")
    local = next(p for p in props.values() if p["kind"] == "local")
    home["status"] = "rejected"
    local["name"], local["name_edited"] = "2026-06-27 Blühendes Barock", True
    local["excluded"] = [local["photos"][0]["path"]]
    cluster.save_proposals(props)

    cluster.run(cfg)
    props = cluster.load_proposals()
    home2 = props[home["id"]]
    assert home2["status"] == "rejected"                    # kept as history, photos become everyday
    local2 = props[local["id"]]
    assert local2["name"] == "2026-06-27 Blühendes Barock" and local2["excluded"] == local["excluded"]


def test_every_dense_home_burst_is_proposed(cfg, library):
    """No model in the loop: a burst well above the usual day is always proposed, with a note."""
    cluster.run(cfg)
    home = next(p for p in cluster.load_proposals().values() if p["kind"] == "home")
    assert home["decision"]["by"] == "rule" and home["decision"]["conf"] == 1.0
    assert home["decision"]["note"].startswith("25 files in 3.6 h at home, 2.08x your usual day, 2 devices")
    assert home["decision"]["state"]["photos"] == 25 and home["name"].endswith("(25 Fotos)")
    cfg.burst_baseline_factor = 40.0                        # threshold above the burst: everyday
    cluster.run(cfg)
    assert "home" not in {p["kind"] for p in cluster.load_proposals().values()}


def test_first_scan_after_a_load_does_not_reload(cfg, library, monkeypatch):
    ingest._known.clear()
    calls = []
    real = cluster._load_records
    monkeypatch.setattr(cluster, "_load_records", lambda c: calls.append(1) or real(c))
    cluster.load_records(cfg)                                            # e.g. the warm-up at startup
    ingest.scan(cfg)                                                     # first scan: knows what the load saw
    cluster.load_records(cfg)
    assert calls == [1]
    ingest._known.clear()
    ingest.scan(cfg)                                                     # a scan with no prior knowledge reloads
    cluster.load_records(cfg)
    assert calls == [1, 1]


def test_load_records_cache_is_patched_per_record(cfg, library, monkeypatch):
    ingest.scan(cfg)                                                     # the first scan in a process reloads once
    calls = []
    real = cluster._load_records
    monkeypatch.setattr(cluster, "_load_records", lambda c: calls.append(1) or real(c))
    a, _ = cluster.load_records(cfg)
    b, _ = cluster.load_records(cfg)
    assert calls == [1] and a == b and a[0] is not b[0]                  # cached, copies
    a[0]["zone"] = "mutated"
    assert cluster.load_records(cfg)[0][0]["zone"] != "mutated"

    first = library.paths[0]
    ingest.write_sidecar(first, {**ingest.read_sidecar(first, cfg), "ts": None}, cfg)
    recs, skipped = cluster.load_records(cfg)
    assert calls == [1] and len(skipped) == 1 and len(recs) == library.n - 1     # patched, not reloaded

    gone = library.paths[1]
    gone.unlink()
    ingest.delete_sidecar(gone, cfg)
    recs, _ = cluster.load_records(cfg)
    assert calls == [1] and all(r["path"] != str(gone) for r in recs) and len(recs) == library.n - 2

    cfg.home_lat += 1
    cluster.load_records(cfg)
    assert calls == [1, 1]                                               # a config change reloads
    ingest.scan(cfg)                                                     # nothing new: no reload
    cluster.load_records(cfg)
    assert calls == [1, 1]
    library.paths[2].unlink()                                            # vanished between scans
    ingest.scan(cfg)
    recs, _ = cluster.load_records(cfg)
    assert calls == [1, 1] and all(r["path"] != str(library.paths[2]) for r in recs)
    ingest.changed["all"] = True                                         # migration/purge: full reload
    cluster.load_records(cfg)
    assert calls == [1, 1, 1]


def test_cache_is_throttled_while_the_pipeline_is_busy(cfg, library, monkeypatch):
    cluster.load_records(cfg)
    first = library.paths[0]
    try:
        cluster.busy = True
        ingest.write_sidecar(first, {**ingest.read_sidecar(first, cfg), "ts": None}, cfg)
        recs, skipped = cluster.load_records(cfg)
        assert not skipped and ingest.changed["paths"]                    # served stale, change kept pending
        monkeypatch.setattr(cluster.time, "time", lambda: cluster._records_cache["built"] + 10)
        recs, skipped = cluster.load_records(cfg)
        assert len(skipped) == 1 and not ingest.changed["paths"]           # old enough: patched
    finally:
        cluster.busy = False
    monkeypatch.undo()
    ingest.write_sidecar(first, {**ingest.read_sidecar(first, cfg), "ts": "2026-06-01T10:00:00"}, cfg)
    recs, skipped = cluster.load_records(cfg)
    assert not skipped                                                     # not busy: patched at once


def test_records_filled_is_cached_and_patched(cfg, library, monkeypatch):
    calls = []
    real = cluster.fill_gps_from_neighbours
    monkeypatch.setattr(cluster, "fill_gps_from_neighbours", lambda c, r, **k: calls.append(1) or real(c, r, **k))
    cluster.records_filled(cfg)
    cluster.records_filled(cfg)
    assert calls == [1]
    no_gps = next(r for r in cluster.records_filled(cfg) if r.get("gps_source", "").startswith("neighbour"))
    assert no_gps["lat"] is not None
    ingest.write_sidecar(library.paths[0], ingest.read_sidecar(library.paths[0], cfg), cfg)
    cluster.records_filled(cfg)
    assert calls == [1, 1]                                               # a record change refills once
    assert len(cluster.everyday_records(cfg)) == library.n


def test_a_move_costs_the_pages_no_refill_and_the_worker_catches_up(cfg, library, monkeypatch):
    """Issue #62: a moved photo's record written in the sorted tree only drops the source from
    the cache (no re-read, re-sort, re-fill per page request); while a job runs the pages use
    the cache as it is, even for changes inside an inbox (copy mode marks its sources)."""
    calls = []
    real = cluster.fill_gps_from_neighbours
    monkeypatch.setattr(cluster, "fill_gps_from_neighbours", lambda c, r, **k: calls.append(1) or real(c, r, **k))
    cluster.records_filled(cfg)
    src = library.paths[0]
    dst = Path(cfg.root) / "2026-06 Somewhere" / src.name
    dst.parent.mkdir(parents=True)
    rec = ingest.read_sidecar(src, cfg)
    ingest.write_sidecar(dst, rec, cfg)                                    # what a move does
    ingest.delete_sidecar(src, cfg)
    filled = cluster.records_filled(cfg)
    assert calls == [1] and str(src) not in {r["path"] for r in filled}  # dropped, nothing refilled
    monkeypatch.setattr(cluster, "moving", True)
    other = library.paths[1]
    ingest.write_sidecar(other, {**ingest.read_sidecar(other, cfg), "copied_to": str(dst)}, cfg)   # copy mode
    assert str(other) in {r["path"] for r in cluster.records_filled(cfg)} and calls == [1]   # not now
    monkeypatch.setattr(cluster, "moving", False)
    assert str(other) not in {r["path"] for r in cluster.records_filled(cfg)} and calls == [1, 1]


def test_the_worker_brings_the_cache_up_to_date_after_a_job(cfg, library, monkeypatch):
    from app import main
    cfg.dry_run = False
    config.save(cfg)
    cluster.run(cfg)
    local = next(p for p in cluster.load_proposals().values() if p["kind"] == "local")
    calls = []
    real = cluster.records_filled
    monkeypatch.setattr(cluster, "records_filled", lambda c: calls.append(1) or real(c))
    props = cluster.load_proposals()
    props[local["id"]]["status"] = "approved"
    cluster.save_proposals(props)
    import threading
    if not any(t.name == "apply" for t in threading.enumerate()):
        threading.Thread(target=main._apply_worker, name="apply", daemon=True).start()
    main.queue_apply(local["id"])
    main.wait_for_apply()
    assert calls and cluster.moving is False
    assert not {p["path"] for p in local["photos"]} & {r["path"] for r in real(config.load())}


def test_run_records_without_timestamp_are_counted(cfg, library):
    from app import ingest
    p = library.paths[0]
    rec_ = ingest.read_sidecar(p, cfg)
    rec_["ts"] = None
    ingest.write_sidecar(p, rec_, cfg)
    stats = cluster.run(cfg)
    assert stats["no_timestamp"] == 1 and stats["photos"] == library.n - 1


# --- several homes (issue #3) -------------------------------------------------------------------------

GRANNY = (48.70, 9.30)


def _at(home: str, t: datetime, pos=HOME) -> dict:
    return {**rec(t, Z.ZONE_HOME, pos), "home": home, "file": f"{home}-{t:%m%d%H%M}.jpg",
            "path": f"/in/{home}-{t.isoformat()}"}


def test_each_home_has_its_own_usual_day_and_its_bursts_say_where(data_dir):
    cfg = config.Config(home_lat=HOME[0], home_lon=HOME[1], burst_min_photos=4, burst_baseline_factor=4,
                        homes=[{"name": "Granny", "lat": GRANNY[0], "lon": GRANNY[1]}])
    recs = [_at("home", T0 + timedelta(days=d, hours=8 + i)) for d in range(10) for i in range(8)]   # 8 a day
    recs += [_at("Granny", T0 + timedelta(days=d, hours=12), GRANNY) for d in range(0, 10, 2)]        # 1 a day
    recs += [_at("Granny", T0 + timedelta(days=20, hours=14, minutes=10 * i), GRANNY) for i in range(6)]
    recs.sort(key=lambda r: r["_t"])
    assert cluster.home_baselines(recs) == {"home": 8, "Granny": 1}
    assert cluster.home_baseline(recs) == 8                                   # all homes together, reported
    made, _ = cluster.automatic_proposals(cfg, recs, T0 + timedelta(days=60), 8, baselines={"home": 8, "Granny": 1})
    names = [p["name"] for p in made.values()]
    assert names == ["2026-06-22 (6 Fotos) @ Granny"]                          # 6 >= 4 x 1 at Granny's
    made, _ = cluster.automatic_proposals(cfg, recs, T0 + timedelta(days=60), 8)   # one baseline: 6 < 4 x 8
    assert made == {}
    assert cluster.burst_home([rec(T0, Z.ZONE_AWAY, LISBON)]) is None


def test_the_old_flat_baseline_file_belongs_to_the_first_home(data_dir):
    cluster.BASELINE_PATH.write_text('{"2026-05-01": 7, "2026-05-02": 9}', encoding="utf-8")
    recs = [_at("home", T0 + h(i)) for i in range(2)]
    assert cluster.home_baselines(recs) == {"home": 7}                        # 7, 9 and today's 2
    stored = cluster.BASELINE_PATH.read_text(encoding="utf-8")
    assert '"home": {' in stored and '"2026-05-02": 9' in stored
    cluster.BASELINE_PATH.write_text('["not", "a", "mapping"]', encoding="utf-8")
    assert cluster.home_baselines(recs) == {"home": 2}                        # unreadable: recomputed


def test_one_home_keeps_the_old_occasion_name():
    from app import rules
    one = config.Config(home_lat=HOME[0], home_lon=HOME[1])
    two = config.Config(home_lat=HOME[0], home_lon=HOME[1], homes=[{"name": "Granny", "lat": 48.7, "lon": 9.3}])
    assert rules.defaults(one)["burst_rules"][0]["name_template"] == "{span} ({media})"
    assert rules.defaults(two)["burst_rules"][0]["name_template"] == "{span} ({media}) @ {home}"


def test_changes_noted_while_a_page_refreshes_the_cache_neither_crash_nor_get_lost(cfg, library):
    """The worker notes moved photos while page requests take the notes (issue #62): iterating
    the live set raised "Set changed size during iteration" (an error page during a move)."""
    import threading
    cluster.records_filled(cfg)
    stop, errors = threading.Event(), []
    outside = Path(cfg.root) / "2026-06 Somewhere"

    def worker():
        i = 0
        while not stop.is_set():
            ingest._bump(outside / f"IMG_{i}.jpg")                         # a destination record
            ingest._bump(library.paths[i % 5], gone=True)                   # its source went away
            i += 1
    t = threading.Thread(target=worker)
    t.start()
    try:
        for _ in range(300):
            try:
                cluster.records_filled(cfg)
            except RuntimeError as e:                                       # pragma: no cover - the bug
                errors.append(e)
    finally:
        stop.set()
        t.join()
    assert errors == []
    left = {r["path"] for r in cluster.records_filled(cfg)}
    assert not {str(p) for p in library.paths[:5]} & left                  # every noted removal arrived


def test_short_stops_at_either_end_leave_the_name_and_the_most_photographed_comes_first():
    """Issue #5: a four-photo airport layover named a trip ("Zeppelinheim, Yangpu, Shanghai")."""
    cfg = config.Config(home_lat=HOME[0], home_lon=HOME[1])
    t = T0
    airport = [rec(t + h(i / 4), Z.ZONE_AWAY, (50.03, 8.57), "Zeppelinheim", "Germany") for i in range(4)]
    trip = [rec(t + h(14 + i * 3), Z.ZONE_AWAY, (31.23, 121.47), "Shanghai", "China") for i in range(30)]
    trip += [rec(t + h(40 + i), Z.ZONE_AWAY, (31.27, 121.52), "Yangpu", "China") for i in range(6)]   # a district
    trip.sort(key=lambda r: r["_t"])
    back = [rec(t + h(120 + i / 4), Z.ZONE_AWAY, (50.03, 8.57), "Zeppelinheim", "Germany") for i in range(3)]
    assert cluster.places_label(cfg, airport + trip) == "Shanghai, Yangpu"      # the stop out goes
    assert cluster.places_label(cfg, trip + back) == "Shanghai, Yangpu"         # ... and the one back
    middle = [rec(t + h(60 + i / 4), Z.ZONE_AWAY, (30.0, 120.0), "Hangzhou", "China") for i in range(3)]
    assert cluster.places_label(cfg, sorted(trip + middle, key=lambda r: r["_t"])) == "Shanghai, Yangpu, Hangzhou"
    big = [rec(t + h(i / 4), Z.ZONE_AWAY, (50.03, 8.57), "Frankfurt", "Germany") for i in range(5)]
    few = [rec(t + h(14 + i * 3), Z.ZONE_AWAY, (31.23, 121.47), "Shanghai", "China") for i in range(10)]
    assert cluster.places_label(cfg, big + few) == "Shanghai, Frankfurt"        # 5 of 15: not a mere stop
    cfg.max_places_in_name = 1
    assert cluster.places_label(cfg, airport + trip) == "China"                  # countries: the stop goes too
    cfg.max_places_in_name = 2
    cfg.rules = {"naming": {"stop_hours": 0.1}}                                  # stops must be shorter now
    assert cluster.places_label(cfg, airport + trip) == "China, Germany"
    cfg.rules = {"naming": {"stop_ratio": 20}}                                   # ... or far smaller (4 x 20 > 36)
    assert cluster.places_label(cfg, airport + trip) == "China, Germany"
    cfg.rules = {"naming": {"stop_photos": 2, "stop_share": 0.01}}               # ... or fewer photos
    assert cluster.places_label(cfg, airport + trip) == "China, Germany"
