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
