from datetime import datetime, timedelta, timezone

from app import cluster, config, geo
from app.kev import Decider
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
    assert cluster.span_label(a, a + timedelta(days=20)) == "2026-06-04..24"
    assert cluster.span_label(a, a + timedelta(days=40)) == "2026-06-04..07-14"
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
    near_short = [rec(t, Z.ZONE_LOCAL, LUDWIGSBURG), rec(t + timedelta(days=2), Z.ZONE_LOCAL, LUDWIGSBURG)]
    assert len(cluster.find_excursions(cfg, near_short)) == 1


def test_trip_proposal_confidences_and_ongoing():
    cfg = config.Config(trip_gap_days=4)
    t = T0
    run = [rec(t, Z.ZONE_AWAY, LISBON, "Lisbon", "Portugal"),
           rec(t + h(1), Z.ZONE_AWAY, LISBON, "Lisbon", gps="neighbour:x"),
           rec(t + h(2), Z.ZONE_UNKNOWN), rec(t + timedelta(days=3), Z.ZONE_AWAY, SEVILLE, "Sevilla", "Spain")]
    pr = cluster.trip_proposal(cfg, run, now=t + timedelta(days=30))
    assert pr["kind"] == "trip" and pr["name"] == "2026-06-01..04 Lisbon, Sevilla" and pr["status"] == "pending"
    assert [p["conf"] for p in pr["photos"]] == [1.0, 0.8, 0.6, 1.0]
    assert pr["photos"][0]["lat"] == LISBON[0]
    assert pr["n"] == 4 and pr["n_uncertain"] == 1 and pr["photos"][2]["uncertain"]
    assert pr["id"].startswith("t") and pr["id"] == cluster.trip_proposal(cfg, run, now=t)["id"]
    assert cluster.trip_proposal(cfg, run, now=t + timedelta(days=5))["status"] == "ongoing"


# --- bursts --------------------------------------------------------------------------

def test_group_bursts_and_baseline():
    cfg = config.Config(burst_gap_hours=3)
    t = T0
    recs = [rec(t, Z.ZONE_HOME), rec(t + h(1), Z.ZONE_HOME), rec(t + h(4.5), Z.ZONE_HOME),
            rec(t + h(6), Z.ZONE_HOME), rec(t + timedelta(days=1), Z.ZONE_LOCAL)]
    assert [len(b) for b in cluster.group_bursts(cfg, recs)] == [2, 2, 1]
    assert cluster.home_baseline(recs) == 4                # one day with 4 home photos -> median 4
    assert cluster.home_baseline([rec(t, Z.ZONE_AWAY)]) == 1.0


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
    stats = cluster.run(cfg, Decider(cfg))
    props = cluster.load_proposals()
    kinds = {p["kind"]: p for p in props.values()}
    assert set(kinds) == {"trip", "local", "home"} and stats["proposals"] == 3
    assert kinds["trip"]["name"] == "2026-06-04..24 Lisbon, Sevilla" and kinds["trip"]["n"] == 67
    assert kinds["local"]["name"] == "2026-06-27 Ludwigsburg" and kinds["local"]["n"] == 15
    assert kinds["home"]["name"] == "2026-06-30 (25 Fotos)" and kinds["home"]["decision"]["by"] == "rule"
    assert stats["photos"] == library.n and stats["everyday"] == library.n - 67 - 15 - 25
    assert stats["no_timestamp"] == 0 and stats["threshold"] == 12


def test_run_keeps_review_state_and_reuses_decisions(cfg, library):
    class CountingDecider(Decider):
        calls = 0

        def choice(self, *a, **kw):
            CountingDecider.calls += 1
            return super().choice(*a, **kw)

    cluster.run(cfg, CountingDecider(cfg))
    props = cluster.load_proposals()
    home = next(p for p in props.values() if p["kind"] == "home")
    local = next(p for p in props.values() if p["kind"] == "local")
    home["status"] = "rejected"
    local["name"], local["name_edited"] = "2026-06-27 Blühendes Barock", True
    local["excluded"] = [local["photos"][0]["path"]]
    cluster.save_proposals(props)

    cluster.run(cfg, CountingDecider(cfg))
    props = cluster.load_proposals()
    assert CountingDecider.calls == 1                       # rejected burst is not re-asked
    home2 = props[home["id"]]
    assert home2["status"] == "rejected"                    # kept as history, photos become everyday
    local2 = props[local["id"]]
    assert local2["name"] == "2026-06-27 Blühendes Barock" and local2["excluded"] == local["excluded"]


def test_run_skips_home_burst_below_confidence(cfg, library):
    cfg.occasion_confidence_min = 0.99
    cluster.run(cfg, Decider(cfg))
    assert {p["kind"] for p in cluster.load_proposals().values()} == {"trip", "local"}


def test_run_uses_decider_answer_and_applied_history(cfg, library):
    class Busy(Decider):
        def choice(self, context, state, options, instructions="", criteria=None):
            return {"id": "x", "by": "kev", "answer": "busy_day", "probs": {}, "conf": 0.9}
    cluster.run(cfg, Busy(cfg))
    assert "home" not in {p["kind"] for p in cluster.load_proposals().values()}


def test_run_records_without_timestamp_are_counted(cfg, library):
    from app import ingest
    p = library.paths[0]
    rec_ = ingest.read_sidecar(p, cfg)
    rec_["ts"] = None
    ingest.write_sidecar(p, rec_, cfg)
    stats = cluster.run(cfg, Decider(cfg))
    assert stats["no_timestamp"] == 1 and stats["photos"] == library.n - 1
