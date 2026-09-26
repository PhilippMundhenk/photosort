"""Manual clusters and additions from the Everyday page: creation, persistence across runs,
exclusion from automatic clustering, the page and the assign route."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cluster, events, ingest, main
from tests import synth


def _recs(cfg, paths):
    recs, _ = cluster.load_records(cfg)
    by = {r["path"]: r for r in recs}
    return [by[p] for p in paths]


def _everyday_paths(cfg):
    return [r["path"] for r in cluster.everyday_records(cfg)]


def test_everyday_records_are_the_unclustered_ones(cfg, library):
    cluster.run(cfg)
    ev = cluster.everyday_records(cfg)
    props = cluster.load_proposals()
    clustered = {p["path"] for pr in props.values() for p in pr["photos"]}
    assert len(ev) == library.n - len(clustered) == 10
    assert all(r["zone"] == "home" for r in ev) and all(r["path"] not in clustered for r in ev)
    # a rejected proposal's photos become everyday again
    local = next(p for p in props.values() if p["kind"] == "local")
    local["status"] = "rejected"
    cluster.save_proposals(props)
    assert len(cluster.everyday_records(cfg)) == 10 + local["n"]


def test_create_manual_and_survive_runs(cfg, library):
    cluster.run(cfg)
    paths = _everyday_paths(cfg)[:4]
    pr = cluster.create_manual("home", "  2026-06 Grillabend ", _recs(cfg, paths))
    assert pr["id"].startswith("m") and pr["manual"] and pr["kind"] == "home" and pr["name"] == "2026-06 Grillabend"
    assert pr["paths"] == sorted(paths, key=lambda p: p) or set(pr["paths"]) == set(paths)
    assert pr["n"] == 4 and pr["status"] == "pending" and pr["decision"]["by"] == "user"
    assert pr["start"] <= pr["end"] and pr["name_edited"]
    props = cluster.load_proposals()
    props[pr["id"]] = pr
    cluster.save_proposals(props)

    cluster.run(cfg)                                  # automatic recompute keeps it
    props = cluster.load_proposals()
    kept = props[pr["id"]]
    assert kept["manual"] and set(kept["paths"]) == set(paths) and kept["n"] == 4
    assert all(p not in {x["path"] for k, q in props.items() if k != pr["id"] for x in q["photos"]} for p in paths)
    assert len(cluster.everyday_records(cfg)) == 10 - 4

    Path(paths[0]).unlink()                                         # a photo disappears: proposal shrinks
    ingest.delete_sidecar(Path(paths[0]), cfg)
    cluster.run(cfg)
    assert cluster.load_proposals()[pr["id"]]["n"] == 3
    for p in paths[1:]:                                             # all gone: proposal dropped
        Path(p).unlink()
        ingest.delete_sidecar(Path(p), cfg)
    cluster.run(cfg)
    assert pr["id"] not in cluster.load_proposals()


def test_default_name_for_manual_cluster(cfg, library):
    cluster.run(cfg)
    recs = _recs(cfg, _everyday_paths(cfg)[:3])
    assert cluster.create_manual("local", "", recs)["name"] == "2026-06 Bietigheim-Bissingen"
    assert "Fotos" in cluster.create_manual("home", "", recs)["name"]
    with pytest.raises(ValueError):
        cluster.create_manual("weird", "x", recs)
    with pytest.raises(ValueError):
        cluster.create_manual("local", "x", [])


def test_manual_photos_are_reserved_from_automatic_clustering(cfg, library):
    """Pulling photos out of the middle of the automatic trip into a manual cluster splits the trip."""
    cluster.run(cfg)
    trip = next(p for p in cluster.load_proposals().values() if p["kind"] == "trip")
    mid = [p["path"] for p in trip["photos"][20:24]]
    props = cluster.load_proposals()
    pr = cluster.create_manual("local", "2026-06-11 Sintra", _recs(cfg, mid))
    props[pr["id"]] = pr
    cluster.save_proposals(props)
    cluster.run(cfg)
    props = cluster.load_proposals()
    trips = [p for p in props.values() if p["kind"] == "trip"]
    assert sum(p["n"] for p in trips) == trip["n"] - 4 and props[pr["id"]]["n"] == 4


def test_add_to_automatic_proposal_persists(cfg, library):
    cluster.run(cfg)
    props = cluster.load_proposals()
    local = next(p for p in props.values() if p["kind"] == "local")
    extra = _everyday_paths(cfg)[:2]
    assert cluster.add_to_proposal(local, _recs(cfg, extra)) == 2
    assert cluster.add_to_proposal(local, _recs(cfg, extra)) == 0          # idempotent
    assert local["n"] == 15 + 2 and set(local["added"]) == set(extra)
    cluster.save_proposals(props)
    cluster.run(cfg)
    again = cluster.load_proposals()[local["id"]]
    assert again["n"] == 17 and set(again["added"]) == set(extra)
    assert set(extra).isdisjoint(_everyday_paths(cfg))
    # excluding one of the added photos in review works like any other
    again["excluded"] = [extra[0]]
    cluster.save_proposals(cluster.load_proposals() | {again["id"]: again})
    cluster.run(cfg)
    assert cluster.load_proposals()[local["id"]]["excluded"] == [extra[0]]


# --- web -----------------------------------------------------------------------------------------

@pytest.fixture
def client(library):
    main._state.update({"last_run": None, "last_stats": {}, "running": False, "error": None})
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def test_everyday_page_lists_by_month_and_day(client, cfg, library):
    cluster.run(cfg)
    r = client.get("/everyday")
    assert r.status_code == 200
    page = r.text
    assert "2026-06" in page and page.count('data-day="') == 9 and 'name="paths"' in page   # 10 photos, 9 days
    assert "Everyday" in client.get("/").text and 'href="/everyday"' in client.get("/").text
    assert "No everyday photos in 2030-01" in client.get("/everyday?month=2030-01").text
    assert 'class="month active"' in page and "<small>10</small>" in page          # month strip with counts
    assert "10 files" in page and "&larr;" not in page and "&rarr;" not in page      # single month: no arrows


def test_assign_creates_manual_cluster_and_adds_to_existing(client, cfg, library):
    cluster.run(cfg)
    paths = _everyday_paths(cfg)
    r = client.post("/everyday/assign", data={"paths": [paths[0], paths[1]], "target": "new",
                                              "kind": "home", "name": "2026-06-01 Kaffee"})
    assert r.status_code == 303 and "/review?open=m" in r.headers["location"]
    props = cluster.load_proposals()
    manual = next(p for p in props.values() if p.get("manual"))
    assert manual["name"] == "2026-06-01 Kaffee" and manual["n"] == 2 and manual["kind"] == "home"
    assert "by hand" in client.get("/review").text and "Kaffee" in client.get("/review").text
    assert events.read(limit=1, kind="review")[0]["action"] == "create"

    local = next(p for p in props.values() if p["kind"] == "local")
    r = client.post("/everyday/assign", data={"paths": [paths[2]], "target": local["id"]})
    assert r.headers["location"] == f"/review?open={local['id']}#{local['id']}"
    assert cluster.load_proposals()[local["id"]]["n"] == 16
    assert len(_everyday_paths(cfg)) == 10 - 3

    # nothing ticked, unknown target, or already-clustered paths: no change
    assert client.post("/everyday/assign", data={"target": "new"}).headers["location"] == "/everyday"
    assert client.post("/everyday/assign", data=[("paths", paths[3]), ("target", "nope")]).headers["location"] \
        == "/everyday"
    assert client.post("/everyday/assign", data=[("paths", paths[2]), ("target", "new")]).headers["location"] \
        == "/everyday"
    assert len(cluster.load_proposals()) == len(props)


def test_manual_cluster_can_be_approved_and_applied(client, cfg, library):
    from app import mover
    cluster.run(cfg)
    paths = _everyday_paths(cfg)[:3]
    client.post("/everyday/assign", data={"paths": paths, "target": "new", "kind": "local",
                                          "name": "2026-06-02 Spaziergang"})
    pr = next(p for p in cluster.load_proposals().values() if p.get("manual"))
    client.post(f"/proposal/{pr['id']}/approve")
    folder = mover.target_folder(cfg, pr)
    assert folder.is_dir() and len(list(folder.rglob("*.jpg"))) == 3
    assert cluster.load_proposals()[pr["id"]]["status"] == "applied"
    cluster.run(cfg)                                  # history is kept after the photos moved
    assert cluster.load_proposals()[pr["id"]]["status"] == "applied"


def test_manual_cluster_with_videos_and_span(cfg, library):
    a = Path(cfg.inboxes[0]["path"])
    library.video(a, synth.T0 + timedelta(days=1, hours=9), synth.HOME)
    cluster.run(cfg)
    recs = [r for r in cluster.everyday_records(cfg) if r["ts"][:10] in ("2026-06-01", "2026-06-02")]
    pr = cluster.create_manual("home", "", recs)
    assert pr["name"].startswith("2026-06 (") and "1 Videos" in pr["name"]
