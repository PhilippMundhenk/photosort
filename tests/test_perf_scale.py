"""Scale and latency, in process (no browser): clustering 20 000 records, loading 5 000 records
from disk, and the web layer with a 3 000-photo proposal. Bounds are generous so that a slow CI
runner passes; the measured values are printed (pytest -s) so a regression is visible long
before it trips a bound.

  what                                  bound
  cluster.run, 20 000 records           15 s
  load_records, 5 000 files on disk     6 s   (second call, cached: 0.3 s)
  /api/status, p95 of 30 calls          80 ms
  /review with a 3 000-photo proposal   4 s
  /everyday, 2 000 photos in a month    4 s
  toggle of 100 photos in one request   1.5 s
"""
from __future__ import annotations

import json
import random
import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cluster, config, geo, main
from tests import synth
from tests.synth import HOME, LISBON, SEVILLE, T0

MEASURED: dict[str, float] = {}
# the coverage tracer (the gated suite) makes every Python line several times slower and turns
# the worker thread into a GIL hog; the bounds are for the code, so they widen under a tracer
TRACED = 4.0 if sys.gettrace() is not None else 1.0


def _report(name: str, seconds: float, bound: float) -> None:
    MEASURED[name] = seconds
    bound *= TRACED
    note = ", traced" if TRACED > 1 else ""
    print(f"\n  perf  {name:<44} {seconds * 1000:8.0f} ms   (bound {bound * 1000:.0f} ms{note})")
    assert seconds < bound, f"{name}: {seconds:.2f} s exceeds {bound} s"


def _rec(i: int, t, zone: str, pos, source: str, gps: bool = True) -> dict:
    return {"file": f"IMG_{i:06d}.jpg", "path": f"/photos/inbox/{source}/IMG_{i:06d}.jpg", "ts": t.isoformat(),
            "_t": t, "zone": zone if gps else geo.ZONE_UNKNOWN, "lat": pos[0] if gps else None,
            "lon": pos[1] if gps else None, "gps_source": "exif" if gps else None, "camera": source,
            "source": source, "inbox": f"/photos/inbox/{source}", "media": "photo",
            "place": {"place": "Lisbon", "country": "Portugal"} if pos == LISBON else
                     {"place": "Sevilla", "country": "Spain"} if pos == SEVILLE else
                     {"place": "Bietigheim-Bissingen", "country": "Germany"}}


def _library_in_memory(n: int = 20_000) -> list[dict]:
    """Three years of two phones: daily photos at home, six trips of 3-14 days, bursts, and 10 %
    without GPS. Deterministic."""
    rnd = random.Random(7)
    recs, i = [], 0
    start = T0 - timedelta(days=3 * 365)
    trips = [(30 + k * 170, 3 + k * 2, LISBON if k % 2 else SEVILLE) for k in range(6)]
    per_day = max(1, n // (3 * 365))
    for d in range(3 * 365):
        day = start + timedelta(days=d)
        trip = next((t for t in trips if t[0] <= d < t[0] + t[1]), None)
        for j in range(per_day):
            src = "phone-a" if (j % 3) else "phone-b"
            t = day + timedelta(hours=8 + (j * 37) % 12, minutes=(j * 13) % 60)
            gps = rnd.random() > 0.1
            if trip and src == "phone-a":
                recs.append(_rec(i, t, geo.ZONE_AWAY, trip[2], src, gps))
            else:
                recs.append(_rec(i, t, geo.ZONE_HOME, HOME, src, gps))
            i += 1
            if i >= n:
                break
        if i >= n:
            break
    recs.sort(key=lambda r: r["_t"])
    return recs


def test_clustering_twenty_thousand_records(data_dir, monkeypatch):
    cfg = config.Config(home_lat=HOME[0], home_lon=HOME[1])
    recs = _library_in_memory(20_000)
    t0 = time.perf_counter()
    cluster.fill_gps_from_neighbours(cfg, recs)
    _report("fill_gps_from_neighbours, 20k", time.perf_counter() - t0, 6.0)
    monkeypatch.setattr(cluster, "load_records", lambda c: ([dict(r) for r in recs], []))
    monkeypatch.setattr(cluster, "records_filled", lambda c: [dict(r) for r in recs])
    t0 = time.perf_counter()
    stats = cluster.run(cfg)
    _report("cluster.run, 20k records", time.perf_counter() - t0, 15.0)
    kinds = [p["kind"] for p in cluster.load_proposals().values()]
    assert kinds.count("trip") == 6 and stats["photos"] == len(recs)
    t0 = time.perf_counter()
    cluster.run(cfg)                                                   # a second run carries state over
    _report("cluster.run again (carry-over)", time.perf_counter() - t0, 15.0)
    t0 = time.perf_counter()
    for _ in range(20):
        cluster.status_counts()
        cluster.load_proposals()
    _report("20x status_counts + load_proposals", time.perf_counter() - t0, 3.0)


@pytest.fixture
def big_library(cfg):
    """5 000 photos on disk (records written directly, no exiftool)."""
    lib = synth.Library(cfg)
    inbox = Path(cfg.inboxes[0]["path"])
    start = T0 - timedelta(days=1250)
    for i in range(5000):
        lib.photo(inbox, start + timedelta(hours=6 * i), HOME)
    return lib


def test_loading_five_thousand_records_from_disk(big_library):
    cfg = big_library.cfg
    cluster._records_cache.update(key=None, filled=None)
    from app import ingest
    ingest.changed["all"] = True
    t0 = time.perf_counter()
    recs, _ = cluster.load_records(cfg)
    _report("load_records, 5000 files", time.perf_counter() - t0, 6.0)
    assert len(recs) == 5000
    t0 = time.perf_counter()
    cluster.records_filled(cfg)
    _report("records_filled (first fill)", time.perf_counter() - t0, 3.0)
    t0 = time.perf_counter()
    cluster.records_filled(cfg)
    _report("records_filled (cached)", time.perf_counter() - t0, 0.3)
    t0 = time.perf_counter()
    st = ingest.scan(cfg)
    _report("scan, nothing new, 5000 files", time.perf_counter() - t0, 8.0)
    assert st["new"] == 0 and st["total"] == 5000
    t0 = time.perf_counter()
    st = ingest.scan(cfg)                                              # zoning unchanged: no re-zone pass
    _report("scan again (no re-zone)", time.perf_counter() - t0, 4.0)
    assert st["rezoned"] is False


@pytest.fixture
def client(cfg):
    main._state.update({"last_run": None, "last_stats": {}, "running": False, "error": None, "progress": None,
                        "runs": 0})
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def test_web_layer_with_a_three_thousand_photo_proposal(client, cfg):
    lib = synth.Library(cfg)
    a = Path(cfg.inboxes[0]["path"])
    day = T0 + timedelta(days=3)
    for i in range(3000):                                              # one long trip: 3000 photos
        lib.photo(a, day + timedelta(minutes=10 * i), LISBON)
    lib.photo(a, day + timedelta(days=25), HOME)
    for i in range(2000):                                              # and everyday photos, 4 a day
        lib.photo(a, T0 + timedelta(days=60, hours=6 * i), HOME)
    t0 = time.perf_counter()
    main.run_pipeline("test")
    _report("run_pipeline, 5001 photos", time.perf_counter() - t0, 30.0)
    trip = next(p for p in cluster.load_proposals().values() if p["kind"] == "trip")
    assert trip["n"] == 3000
    lat = []
    for _ in range(30):
        t0 = time.perf_counter()
        assert client.get("/api/status").status_code == 200
        lat.append(time.perf_counter() - t0)
    lat.sort()
    _report("/api/status p95 (30 calls)", lat[int(len(lat) * 0.95) - 1], 0.08)
    _report("/api/status median", statistics.median(lat), 0.05)
    t0 = time.perf_counter()
    assert client.get("/review").status_code == 200
    _report("/review, 3000-photo proposal", time.perf_counter() - t0, 4.0)
    t0 = time.perf_counter()
    r = client.get("/everyday")
    _report("/everyday, latest month of 2000 photos", time.perf_counter() - t0, 4.0)
    assert r.status_code == 200 and r.text.count("<figure") >= 40      # the latest, partial month
    paths = [p["path"] for p in trip["photos"][:100]]
    t0 = time.perf_counter()
    r = client.post(f"/proposal/{trip['id']}/toggle", data={"path": paths}, headers={"X-Requested-With": "fetch"})
    _report("toggle 100 photos in one request", time.perf_counter() - t0, 1.5)
    assert r.status_code == 200 and len(r.json()["excluded_paths"]) == 100
    t0 = time.perf_counter()
    for _ in range(10):
        client.get("/api/status")
    _report("10x /api/status after a save", time.perf_counter() - t0, 0.5)


def test_reading_the_tail_of_a_huge_event_log_is_quick(data_dir):
    from app import events
    line = json.dumps({"ts": "2026-09-29T00:00:00+00:00", "kind": "review", "action": "toggle",
                       "photo": "IMG_0001.jpg", "excluded": True, "proposal": "t0123456789"}) + "\n"
    with events.EVENTS_PATH.open("w", encoding="utf-8") as f:
        for _ in range(300):
            f.write(line * 1000)                                       # 300 000 events, ~45 MB
    t0 = time.perf_counter()
    rows = events.read(limit=8)
    _report("events.read(limit=8) on 300k events", time.perf_counter() - t0, 0.5)
    assert len(rows) == 8
    t0 = time.perf_counter()
    rows = events.read(limit=300)
    _report("events.read(limit=300) on 300k events", time.perf_counter() - t0, 0.5)
    assert len(rows) == 300


def test_pages_stay_quick_while_a_big_move_runs(client, cfg):
    """Approve a 3000-photo trip and, while the worker moves it, time the review page, the
    dashboard and the status poll: the user must not notice the move."""
    lib = synth.Library(cfg)
    a = Path(cfg.inboxes[0]["path"])
    day = T0 + timedelta(days=3)
    for i in range(3000):
        lib.photo(a, day + timedelta(minutes=10 * i), LISBON)
    lib.photo(a, day + timedelta(days=25), HOME)
    for i in range(300):
        lib.photo(a, T0 + timedelta(days=60, hours=6 * i), HOME)
    c = config.load()
    c.dry_run, c.generate_thumbnails = False, False      # no in-process prefetch competing for the GIL
    config.save(c)                                       # (production decodes in a paced helper process)
    main.run_pipeline("test")
    trip = next(p for p in cluster.load_proposals().values() if p["kind"] == "trip")
    t0 = time.perf_counter()
    r = client.post(f"/proposal/{trip['id']}/approve")
    _report("POST approve (3000-photo trip)", time.perf_counter() - t0, 1.0)
    assert r.status_code == 303
    lat: dict[str, list[float]] = {"/review": [], "/": [], "/everyday": [], "/api/status": []}
    while trip["id"] in main._applying or trip["id"] in list(main._apply_queue.queue):
        for path in lat:
            t0 = time.perf_counter()
            assert client.get(path).status_code == 200
            lat[path].append(time.perf_counter() - t0)
        if sum(len(v) for v in lat.values()) > 400:
            break
    main.wait_for_apply()
    assert lat["/review"], "the move was over before a single page was timed"
    for path, times in lat.items():
        times.sort()
        p95 = times[int(len(times) * 0.95) - 1] if len(times) > 1 else times[0]
        _report(f"{path} p95 while moving ({len(times)} calls)", p95, 0.3 if path == "/api/status" else 1.5)
    assert cluster.load_proposals()[trip["id"]]["status"] == "applied"
