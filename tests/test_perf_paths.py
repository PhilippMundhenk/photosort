"""The paths a large library made slow or unstable: proposals re-read per status poll, the
sorted tree walked per page, every record re-zoned per run, the whole event log parsed per
dashboard, and the GPS fill run by every request at once."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from app import cluster, config, events, ingest, mover


def test_proposals_are_reread_only_when_the_file_changed(data_dir, monkeypatch):
    cluster.save_proposals({"x": {"status": "pending", "photos": []}})
    reads = []
    real = Path.read_text

    def counting(self, *a, **k):
        if self.name == "proposals.json":
            reads.append(self.name)
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", counting)
    assert cluster.load_proposals()["x"]["status"] == "pending"
    cluster.load_proposals()
    assert cluster.status_counts() == {"pending": 1, "approved": {}}
    assert reads == []                                                  # served from what save() kept
    time.sleep(0.01)
    cluster.PROPOSALS_PATH.write_text(json.dumps({"x": {"status": "approved", "photos": [], "error": "boom"}}),
                                      encoding="utf-8")
    assert cluster.load_proposals()["x"]["status"] == "approved" and reads == ["proposals.json"]
    assert cluster.status_counts() == {"pending": 0, "approved": {"x": "boom"}}
    assert reads == ["proposals.json"]
    mine = cluster.load_proposals()
    mine["x"]["status"] = "rejected"
    assert cluster.load_proposals()["x"]["status"] == "approved"        # callers get their own copy


def test_cluster_list_is_cached_until_a_manifest_changes(cfg, library, monkeypatch):
    cluster.run(cfg)
    local = next(p for p in cluster.load_proposals().values() if p["kind"] == "local")
    mover.apply(cfg, local, reviewed=True)
    assert [c["name"] for c in mover.list_clusters(cfg)] == [local["name"]]
    walks = []
    real = Path.rglob

    def counting(self, pattern):
        walks.append(pattern)
        return real(self, pattern)
    monkeypatch.setattr(Path, "rglob", counting)
    mover.list_clusters(cfg)
    assert mover.MANIFEST not in walks                                  # no walk of the sorted tree
    dst = mover.rename(cfg, mover.target_folder(cfg, local), "2026-06-27 Barock")
    assert [c["name"] for c in mover.list_clusters(cfg)] == ["2026-06-27 Barock"]
    mover.undo(cfg, dst)
    assert mover.list_clusters(cfg) == []
    other = config.Config(**{**cfg.as_dict(), "root": cfg.root + "-other"})
    assert mover.list_clusters(other) == []                              # a different root is a different list


def test_scan_rezones_only_when_zoning_inputs_change(cfg, library, monkeypatch):
    assert ingest.scan(cfg)["rezoned"] is True                          # first scan with this data dir
    calls = []
    real = ingest.enrich_location
    monkeypatch.setattr(ingest, "enrich_location", lambda c, r: calls.append(r.get("file")) or real(c, r))
    st = ingest.scan(cfg)
    assert st["rezoned"] is False and calls == []                       # nothing changed: no record touched
    cfg.home_lat += 1.0
    st = ingest.scan(cfg)
    assert st["rezoned"] is True and len(calls) == len(library.paths)
    calls.clear()
    cfg.named_places = [{"name": "Somewhere", "lat": 1.0, "lon": 1.0, "radius_km": 2.0}]
    assert ingest.scan(cfg)["rezoned"] is True and calls               # own places change labels too
    calls.clear()
    assert ingest.scan(cfg, force=True)["rezoned"] is True and calls    # force always does


def test_event_log_reads_only_the_tail_of_a_big_file(data_dir, monkeypatch):
    monkeypatch.setattr(events, "TAIL_BYTES", 2000)
    for i in range(200):
        events.log("review", i=i)
    assert [r["i"] for r in events.read(limit=5)] == [199, 198, 197, 196, 195]
    assert events.read(limit=5, kind="review")[0]["i"] == 199
    assert len(events.read(limit=1000)) < 200                            # the tail, not the whole file
    assert len(events.read(limit=1000, kind="review")) == 200            # a filter reads everything


def test_gps_fill_runs_once_for_concurrent_requests(cfg, library, monkeypatch):
    cluster.load_records(cfg)
    cluster._records_cache["filled"] = None
    fills = []
    real = cluster.fill_gps_from_neighbours

    def slow(c, recs, **k):
        fills.append(1)
        time.sleep(0.2)
        real(c, recs, **k)
    monkeypatch.setattr(cluster, "fill_gps_from_neighbours", slow)
    out = []
    threads = [threading.Thread(target=lambda: out.append(len(cluster.records_filled(cfg)))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(fills) == 1 and len(set(out)) == 1 and out[0] > 0


def test_reapplying_the_same_trip_keeps_one_manifest(cfg, library):
    """Photos of a trip that sync after it was moved form a new proposal with the same name;
    applying it must add to the folder's manifest, not replace it, or undo forgets the first batch."""
    cluster.run(cfg)
    trip = next(p for p in cluster.load_proposals().values() if p["kind"] == "trip")
    first, second = dict(trip, photos=trip["photos"][:5]), dict(trip, id="t2", photos=trip["photos"][5:9])
    mover.apply(cfg, first, reviewed=True)
    mover.apply(cfg, second, reviewed=True)
    folder = mover.target_folder(cfg, trip)
    m = mover.read_manifest(folder)
    assert len(m["photos"]) == 9 and m["proposal_id"] == "t2"
    assert mover.undo(cfg, folder) == 9
    assert all(Path(p["path"]).exists() for p in trip["photos"][:9])


def test_record_written_during_a_full_reload_is_not_lost(cfg, library, monkeypatch):
    """The cache warm-up or a page may be reloading every record while the scan (or a test)
    writes a new one; that record's change note used to be wiped when the reload finished."""
    from datetime import timedelta

    from tests.synth import HOME, T0
    cluster.load_records(cfg)
    real = cluster._load_records
    late: list[Path] = []

    def load_and_get_interrupted(c):
        recs, skipped = real(c)
        late.append(library.photo(Path(cfg.inboxes[0]["path"]), T0 + timedelta(days=200), HOME))   # during the load
        return recs, skipped
    monkeypatch.setattr(cluster, "_load_records", load_and_get_interrupted)
    ingest.changed["all"] = True
    recs, _ = cluster.load_records(cfg)
    assert str(late[0]) not in {r["path"] for r in recs}                   # the load did not see it
    assert str(late[0]) in ingest.changed["paths"]                          # but it is still noted
    monkeypatch.setattr(cluster, "_load_records", real)
    recs, _ = cluster.load_records(cfg)
    assert str(late[0]) in {r["path"] for r in recs}                       # and patched in next time


def test_deleting_the_proposals_file_resets_the_counts_too(data_dir):
    """Deleting proposals.json is the way to start over; the status poll must not keep
    reporting the old counts from its cache."""
    cluster.save_proposals({"a": {"status": "pending", "photos": []}, "b": {"status": "approved", "photos": []}})
    assert cluster.status_counts() == {"pending": 1, "approved": {"b": None}}
    cluster.PROPOSALS_PATH.unlink()
    assert cluster.load_proposals() == {}
    assert cluster.status_counts() == {"pending": 0, "approved": {}}


def test_moved_away_records_leave_the_cache_without_touching_the_share(cfg, library, monkeypatch):
    """A move deletes thousands of records; the next page load must drop them from the cache in
    one pass, not stat every one of them on the share."""
    cluster.load_records(cfg)
    cluster.records_filled(cfg)
    real = Path.exists
    stats = []
    monkeypatch.setattr(Path, "exists", lambda self: stats.append(str(self)) or real(self))
    for p in library.paths[:40]:
        ingest.delete_sidecar(p, cfg)                                       # what _transfer does per file
    assert len(ingest.changed["gone"]) == 40 and not ingest.changed["paths"]
    stats.clear()
    recs, _ = cluster.load_records(cfg)
    assert len(recs) == len(library.paths) - 40
    assert not any(str(p) in s for p in library.paths[:40] for s in stats)   # no stat of a gone file
    assert cluster._records_cache["filled"] is not None                     # and no refill
    ingest.write_sidecar(library.paths[50], ingest.read_sidecar(library.paths[50], cfg), cfg)
    ingest.delete_sidecar(library.paths[51], cfg)
    recs, _ = cluster.load_records(cfg)                                     # a write and a delete together
    assert len(recs) == len(library.paths) - 41


def test_records_in_the_sorted_tree_are_ignored_without_a_stat(cfg, library, monkeypatch):
    real = Path.exists
    stats = []
    monkeypatch.setattr(Path, "exists", lambda self: stats.append(str(self)) or real(self))
    assert cluster._record_for(cfg, str(Path(cfg.root) / "2026-06 X" / "IMG_9.jpg")) is None
    assert stats == []


def test_cluster_list_never_blocks_a_page_on_the_walk(cfg, library, monkeypatch):
    cluster.run(cfg)
    local = next(p for p in cluster.load_proposals().values() if p["kind"] == "local")
    mover.apply(cfg, local, reviewed=True)
    assert [c["name"] for c in mover.list_clusters(cfg)] == [local["name"]]   # the first list: built now
    real = mover._list_clusters
    started = threading.Event()

    def slow(c):
        started.set()
        time.sleep(0.5)                                                     # a share that takes its time
        return real(c)
    monkeypatch.setattr(mover, "_list_clusters", slow)
    mover.invalidate_clusters()                                             # a manifest was written meanwhile
    t0 = time.perf_counter()
    stale = mover.list_clusters(cfg)
    assert time.perf_counter() - t0 < 0.4 and [c["name"] for c in stale] == [local["name"]]   # a short wait, old list
    assert started.wait(2)
    mover.wait_for_clusters()
    assert [c["name"] for c in mover.list_clusters(cfg)] == [local["name"]]


def test_undo_clears_half_written_temp_files(cfg, library):
    cluster.run(cfg)
    local = next(p for p in cluster.load_proposals().values() if p["kind"] == "local")
    mover.apply(cfg, local, reviewed=True)
    folder = mover.target_folder(cfg, local)
    (folder / "IMG_0001.jpg.photosort.json.tmp").write_text("{", encoding="utf-8")   # the kill hit mid-write
    (folder / "manifest.json.tmp").write_text("{", encoding="utf-8")
    assert mover.undo(cfg, folder) == local["n"]
    assert not folder.exists()


def test_a_cluster_written_here_is_listed_before_any_walk_of_the_share(cfg, library, monkeypatch):
    cluster.run(cfg)
    mover.list_clusters(cfg)                                                # an (empty) list is cached
    monkeypatch.setattr(mover, "_list_clusters", lambda c: time.sleep(3) or [])   # the share is slow today
    local = next(p for p in cluster.load_proposals().values() if p["kind"] == "local")
    mover.apply(cfg, local, reviewed=True)
    t0 = time.perf_counter()
    names = [c["name"] for c in mover.list_clusters(cfg)]
    assert names == [local["name"]] and time.perf_counter() - t0 < 0.5      # from the write, not the walk
    folder = mover.target_folder(cfg, local)
    dst = mover.rename(cfg, folder, "2026-06-27 Barock")
    assert [c["name"] for c in mover.list_clusters(cfg)] == ["2026-06-27 Barock"]
    mover.undo(cfg, dst)
    assert mover.list_clusters(cfg) == []
    mover.wait_for_clusters()


def test_proposals_are_parsed_once_and_copies_do_not_leak(data_dir):
    """Two calls after one save parse nothing; each caller may edit status, name, excluded and the
    photo list of its copy without the next caller seeing it (the photo entries are shared)."""
    cluster.save_proposals({"a": {"status": "pending", "name": "x", "excluded": [], "photos": [{"path": "/p1"}]}})
    calls = []
    real = json.loads
    monkeypatch_loads = lambda s, *a, **k: calls.append(1) or real(s, *a, **k)  # noqa: E731
    import app.cluster as mod
    saved = mod.json.loads
    mod.json.loads = monkeypatch_loads
    try:
        one = cluster.load_proposals()
        two = cluster.load_proposals()
    finally:
        mod.json.loads = saved
    assert calls == []                                                      # the save left a parsed copy
    one["a"]["status"] = "approved"
    one["a"]["excluded"].append("/p1")
    one["a"]["photos"].append({"path": "/p2"})
    assert two["a"]["status"] == "pending" and two["a"]["excluded"] == [] and len(two["a"]["photos"]) == 1
    assert cluster.load_proposals()["a"]["status"] == "pending"
    cluster.PROPOSALS_PATH.write_text('{"b": {"status": "pending", "photos": []}}', encoding="utf-8")
    assert set(cluster.load_proposals()) == {"b"}                           # a changed file is re-read
    assert "\n" not in cluster.PROPOSALS_PATH.read_text(encoding="utf-8").strip()   # compact on disk
