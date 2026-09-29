"""Manual clusters and additions from the Everyday page: creation, persistence across runs,
exclusion from automatic clustering, the page and the assign route."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cluster, config, events, ingest, main
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
    assert "2026-06" in page and page.count('data-day="') == 9 and 'data-path="' in page    # 10 photos, 9 days
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
    main.wait_for_apply()
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


def test_everyday_photos_can_join_a_cluster_that_was_already_moved(client, library):
    """A photo that surfaced late (or was missed) joins a cluster whose folder exists: the target
    list offers moved clusters, the addition is moved into the folder by the worker, the
    manifest is extended, and undo still covers everything."""
    from app import main, mover
    cfg = config.load()
    cfg.dry_run = False
    config.save(cfg)
    cluster.run(cfg)
    local = next(p for p in cluster.load_proposals().values() if p["kind"] == "local")
    client.post(f"/proposal/{local['id']}/approve")
    main.wait_for_apply()
    folder = mover.target_folder(cfg, local)
    html = client.get("/everyday").text
    assert 'label="already moved' in html and f'<option value="{local["id"]}">local · {local["name"]}' in html
    assert 'data-filter="target"' in html
    extra = [r["path"] for r in cluster.everyday_records(cfg)][:2]
    r = client.post("/everyday/assign", data={"paths": extra, "target": local["id"]})
    assert r.status_code == 303 and local["id"] in r.headers["location"]
    main.wait_for_apply()
    pr = cluster.load_proposals()[local["id"]]
    assert pr["status"] == "applied" and pr["n"] == local["n"] + 2 and set(pr["added"]) == set(extra)
    m = mover.read_manifest(folder)
    assert len(m["photos"]) == local["n"] + 2 and not any(Path(p).exists() for p in extra)
    assert all(str(folder) in e["dst"] for e in m["photos"])
    assert client.post("/cluster/undo", data={"folder": str(folder)}).status_code == 303
    assert all(Path(p).exists() for p in extra) and all(Path(p["path"]).exists() for p in local["photos"])


def test_adding_to_a_moved_cluster_waits_in_dry_run(client, library):
    from app import main, mover
    cfg = config.load()
    cfg.dry_run = False
    config.save(cfg)
    cluster.run(cfg)
    local = next(p for p in cluster.load_proposals().values() if p["kind"] == "local")
    client.post(f"/proposal/{local['id']}/approve")
    main.wait_for_apply()
    cfg.dry_run = True
    config.save(cfg)
    extra = [r["path"] for r in cluster.everyday_records(cfg)][:1]
    client.post("/everyday/assign", data={"paths": extra, "target": local["id"]})
    main.wait_for_apply()
    pr = cluster.load_proposals()[local["id"]]
    assert pr["status"] == "approved" and Path(extra[0]).exists()           # waits for dry-run to go off
    assert len(mover.read_manifest(mover.target_folder(cfg, local))["photos"]) == local["n"]


def test_everyday_search_finds_a_photo_in_any_month(client, library):
    cfg = config.load()
    lib = synth.Library(cfg)
    lib.n = 7000
    old = lib.photo(Path(cfg.inboxes[0]["path"]), synth.T0 - timedelta(days=400), synth.HOME)   # 2025-04
    cluster.run(cfg)
    html = client.get("/everyday").text
    assert old.name not in html                                              # the latest month is shown
    html = client.get("/everyday", params={"q": old.stem[-4:]}).text
    assert old.name in html and "1 match for" in html and "back to the months" in html
    assert html.count("<figure") == 1
    assert "0 matches for" in client.get("/everyday", params={"q": "no-such-file"}).text


def _applied(client, kind: str) -> tuple[dict, Path]:
    from app import main, mover
    cfg = config.load()
    pr = next(p for p in cluster.load_proposals().values() if p["kind"] == kind)
    client.post(f"/proposal/{pr['id']}/approve")
    main.wait_for_apply()
    return cluster.load_proposals()[pr["id"]], mover.target_folder(cfg, pr)


def test_photos_move_from_one_cluster_to_another_to_a_new_one_or_back_to_the_inbox(client, library):
    """The cluster page has the Everyday selection bar: selected photos go to a moved cluster
    (the worker moves them into its folder), to an open proposal, to a new cluster, or back to
    the inbox. The old cluster keeps them as corrections; one journal batch holds the moves out."""
    from app import journal, main, mover
    cfg = config.load()
    cfg.dry_run = False
    config.save(cfg)
    cluster.run(cfg)
    local, folder = _applied(client, "local")
    home, home_folder = _applied(client, "home")
    trip = next(p for p in cluster.load_proposals().values() if p["kind"] == "trip")
    page = client.get("/clusters/view", params={"folder": str(folder)}).text
    assert 'id="assign"' in page and 'data-filter="target"' in page and 'data-select-all' in page
    assert f'<option value="{home["id"]}">home · {home["name"]}' in page      # a moved cluster is a target
    assert f'<option value="{trip["id"]}">trip · {trip["name"]}' in page      # so is an open proposal
    assert f'value="{local["id"]}"' not in page                               # its own cluster is not
    dsts = [p["dst"] for p in mover.read_manifest(folder)["photos"]]
    # to a cluster that was already moved: the worker takes them into that folder
    r = client.post("/cluster/assign", data={"folder": str(folder), "paths": dsts[:2], "target": home["id"]})
    assert r.status_code == 303 and home["id"] in r.headers["location"]
    main.wait_for_apply()
    m_home, m_local = mover.read_manifest(home_folder), mover.read_manifest(folder)
    assert len(m_home["photos"]) == home["n"] + 2 and len(m_local["photos"]) == local["n"] - 2
    assert len(m_local["corrections"]) == 2 and all(str(home_folder) in p["dst"] for p in m_home["photos"][-2:])
    assert not any(Path(d).exists() for d in dsts[:2])
    props = cluster.load_proposals()
    assert props[home["id"]]["status"] == "applied" and props[home["id"]]["n"] == home["n"] + 2
    assert len(props[local["id"]]["excluded"]) == 2                          # they count as gone from here
    batches = journal.batches()
    assert batches[0]["action"] == "apply" and batches[0]["n"] == 2          # the worker's move in
    assert batches[1]["action"] == "move_between" and batches[1]["n"] == 2   # the moves out, one batch
    assert batches[1]["meta"]["proposal"] == local["id"] and batches[1]["meta"]["target"] == home["id"]
    # to a new cluster: waits for approval in Review, the file is back in the inbox meanwhile
    r = client.post("/cluster/assign", data={"folder": str(folder), "paths": [dsts[2]], "target": "new",
                                             "kind": "local", "name": "2026-06-27 Extra"})
    new_id = r.headers["location"].split("open=")[1].split("#")[0]
    new = cluster.load_proposals()[new_id]
    assert new["status"] == "pending" and new["n"] == 1 and new["name"] == "2026-06-27 Extra"
    assert Path(new["photos"][0]["path"]).exists() and not Path(dsts[2]).exists()
    # to an open proposal
    r = client.post("/cluster/assign", data={"folder": str(folder), "paths": [dsts[3]], "target": trip["id"]})
    assert r.status_code == 303 and trip["id"] in r.headers["location"]
    assert cluster.load_proposals()[trip["id"]]["n"] == trip["n"] + 1
    # back to the inbox only: a correction, nothing else
    r = client.post("/cluster/assign", data={"folder": str(folder), "paths": [dsts[4]], "target": "inbox"})
    assert r.status_code == 303 and r.headers["location"].startswith("/clusters/view")
    m_local = mover.read_manifest(folder)
    assert len(m_local["corrections"]) == 5 and len(m_local["photos"]) == local["n"] - 5
    assert len(cluster.everyday_records(cfg)) >= 1
    # no-ops and refusals
    assert client.post("/cluster/assign", data={"folder": str(folder), "target": "new"}).status_code == 303
    r = client.post("/cluster/assign", data={"folder": str(folder), "paths": [dsts[5]], "target": local["id"]})
    assert r.status_code == 303 and Path(dsts[5]).exists()                    # its own cluster: nothing moves
    r = client.post("/cluster/assign", data={"folder": str(folder), "paths": [str(home_folder / "x.jpg")],
                                             "target": "inbox"})
    assert r.status_code == 400                                              # not in that folder
    r = client.post("/cluster/assign", data={"folder": str(folder), "paths": [dsts[5]], "target": "nope"})
    assert r.status_code == 303 and not Path(dsts[5]).exists()               # unknown target: back in the inbox,
    assert len(mover.read_manifest(folder)["corrections"]) == 6                # listed as a correction
    r = client.post("/cluster/assign", data={"folder": str(folder), "paths": [str(folder / "ghost.jpg")],
                                             "target": "inbox"})
    assert r.status_code == 303 and len(mover.read_manifest(folder)["corrections"]) == 6   # not there: nothing
    assert len(journal.batches()) == 8                                       # every action that moved: on record
    assert [b["action"] for b in journal.batches()].count("move_between") == 5


def test_a_photo_whose_record_travels_with_it_is_not_indexed_again(client, library, monkeypatch):
    from app import ingest, mover
    cfg = config.load()
    cfg.dry_run, cfg.sidecar_cleanup = False, "never"                         # the record stays with the photo
    config.save(cfg)
    cluster.run(cfg)
    local, folder = _applied(client, "local")
    indexed = []
    monkeypatch.setattr(ingest, "index_paths", lambda c, paths: indexed.append(paths))
    dst = mover.read_manifest(folder)["photos"][0]["dst"]
    r = client.post("/cluster/assign", data={"folder": str(folder), "paths": [dst], "target": "inbox"})
    assert r.status_code == 303 and indexed == []
    back = Path(mover.read_manifest(folder)["corrections"][0]["src"])
    assert back.exists() and ingest.read_sidecar(back, cfg) is not None


def test_moving_between_clusters_is_refused_in_dry_run(client, library):
    from app import journal, mover
    cfg = config.load()
    cfg.dry_run = False
    config.save(cfg)
    cluster.run(cfg)
    local, folder = _applied(client, "local")
    home, _ = _applied(client, "home")
    cfg.dry_run = True
    config.save(cfg)
    dsts = [p["dst"] for p in mover.read_manifest(folder)["photos"]]
    r = client.post("/cluster/assign", data={"folder": str(folder), "paths": dsts[:2], "target": home["id"]})
    assert r.status_code == 409 and all(Path(d).exists() for d in dsts)
    assert len(mover.read_manifest(folder)["photos"]) == local["n"]
    assert [b["action"] for b in journal.batches()] == ["apply", "apply"]      # nothing moved: nothing on record
