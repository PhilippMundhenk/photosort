"""Undo, rename and put back run in the apply worker like approve (issue #4): queued, with progress,
one at a time with the pipeline, and never a file touched in dry-run."""
from __future__ import annotations

import contextlib
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app import cluster, config, events, main, mover

FETCH = {"X-Requested-With": "fetch"}


@pytest.fixture
def client(library):
    main._state.update({"last_run": None, "last_stats": {}, "running": False, "error": None})
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def _kind(kind: str) -> dict:
    return next(p for p in cluster.load_proposals().values() if p["kind"] == kind)


def _applied(client, kind: str = "local") -> tuple[config.Config, dict, Path]:
    cfg = config.load()
    cluster.run(cfg)
    pr = _kind(kind)
    client.post(f"/proposal/{pr['id']}/approve")
    main.wait_for_apply()
    return cfg, pr, mover.target_folder(cfg, pr)


@contextlib.contextmanager
def _worker_held():
    """The worker waits for _lock (as it does while a run scans): jobs stay queued meanwhile."""
    main._lock.acquire()
    try:
        yield
    finally:
        main._lock.release()


def _files(*folders: Path) -> list[str]:
    return sorted(str(p) for f in folders if f.exists() for p in f.rglob("*") if p.is_file())


def _set_dry_run(on: bool) -> None:
    cfg = config.load()
    cfg.dry_run = on
    config.save(cfg)


def test_undo_is_queued_shown_and_done_by_the_worker(client, library, monkeypatch):
    cfg, local, folder = _applied(client)
    seen = []
    real = mover.undo

    def undo(cfg, folder, progress=None):
        return real(cfg, folder, progress=lambda d, t, c=None: (seen.append((d, t)), progress(d, t, c)))
    monkeypatch.setattr(mover, "undo", undo)
    removed = mover.read_manifest(folder)["photos"][-1]["dst"]
    client.post("/cluster/move_out", data={"folder": str(folder), "photo": removed})
    src = mover.read_manifest(folder)["corrections"][0]["src"]
    with _worker_held():
        r = client.post("/cluster/undo", data={"folder": str(folder)})
        assert r.status_code == 303 and r.headers["location"] == "/clusters"
        assert folder.is_dir()                                               # nothing moved inside the request
        key = f"undo:{folder}"
        status = client.get("/api/status").json()
        assert status["queue"][key] == "queued" and status["jobs"] == 1
        page = client.get("/clusters").text
        assert "Undoing" in page and f'data-progress="{key}"' in page and "data-reload-done" in page
        assert ">undoing<" in page                                           # the row: no second undo button
        assert "Undoing" in client.get("/clusters/view", params={"folder": str(folder)}).text
        # a second click: the same job, not a second one
        assert client.post("/cluster/undo", data={"folder": str(folder)}).status_code == 303
        assert list(main._jobs) == [key]
        # other actions on the folder meanwhile would race it: refused, nothing done
        photo = mover.read_manifest(folder)["photos"][0]["dst"]
        r = client.post("/cluster/move_out", data={"folder": str(folder), "photo": photo})
        assert r.status_code == 409 and "Folder busy" in r.text and "Undoing" in r.text
        r = client.post("/cluster/rename", data={"folder": str(folder), "name": "x"}, headers=FETCH)
        assert r.status_code == 409 and "try again" in r.json()["error"]
        r = client.post("/cluster/assign", data={"folder": str(folder), "paths": [photo], "target": "inbox"})
        assert r.status_code == 409
        r = client.post("/cluster/put_back", data={"folder": str(folder), "src": src})
        assert r.status_code == 409 and "Undoing" in r.text
        assert Path(photo).exists() and Path(src).exists()
    main.wait_for_apply()
    assert not folder.exists() and all(Path(p["path"]).exists() for p in local["photos"])
    assert seen and seen[-1] == (len(local["photos"]) - 1, len(local["photos"]) - 1)
    assert _kind("local")["status"] == "rejected"
    status = client.get("/api/status").json()
    assert status["jobs"] == 0 and status["queue"] == {} and status["applying"] == {}
    assert "Undoing" not in client.get("/clusters").text


def test_running_job_reports_its_verb_and_progress(client, library):
    cfg, local, folder = _applied(client)
    main._jobs["undo:x"] = {"type": "undo", "folder": "x", "name": "Somewhere", "n": 9}
    main._applying["undo:x"] = {"done": 2, "total": 9, "name": "Somewhere", "verb": "Undoing", "current": None}
    try:
        status = client.get("/api/status").json()
        assert status["applying"]["undo:x"]["verb"] == "Undoing" and "undo:x" not in status["queue"]
        assert status["jobs"] == 0                                           # running, not waiting
        page = client.get("/clusters").text
        assert "running" in page and "2 / 9" in page
    finally:
        main._jobs.pop("undo:x")
        main._applying.pop("undo:x")


def test_put_back_runs_in_the_worker(client, library):
    cfg, local, folder = _applied(client)
    photo = mover.read_manifest(folder)["photos"][0]["dst"]
    client.post("/cluster/move_out", data={"folder": str(folder), "photo": photo})
    src = mover.read_manifest(folder)["corrections"][0]["src"]
    with _worker_held():
        r = client.post("/cluster/put_back", data={"folder": str(folder), "src": src})
        assert r.status_code == 303 and Path(src).exists() and not Path(photo).exists()
        assert client.post("/cluster/put_back", data={"folder": str(folder), "src": src}).status_code == 303
        page = client.get("/clusters/view", params={"folder": str(folder)}).text
        assert "Putting back" in page and Path(src).name in page
        r = client.post("/cluster/undo", data={"folder": str(folder)})     # another job on it: refused
        assert r.status_code == 409 and "Putting back" in r.text
    main.wait_for_apply()
    assert Path(photo).exists() and not Path(src).exists()
    assert src not in _kind("local")["excluded"]
    assert not mover.read_manifest(folder)["corrections"]
    # nothing to put back (gone from the corrections meanwhile): the job does nothing
    main._job_put_back(cfg, {"folder": str(folder), "src": src}, lambda *a: None)
    assert Path(photo).exists()


def test_big_or_shared_rename_runs_in_the_worker(client, library, monkeypatch):
    cfg, trip, folder = _applied(client, "trip")
    assert not main._rename_in_background(cfg, folder)                       # central records, small: at once
    assert not main._rename_in_background(cfg, Path(cfg.root) / "nothing")  # no manifest: nothing per photo
    cfg.sidecar_mode = "beside"
    assert main._rename_in_background(cfg, folder)                           # records on the share: worker
    monkeypatch.setattr(main, "RENAME_SYNC_MAX", 0)                          # this cluster counts as big
    dst = Path(cfg.root) / "2026-06 Portugal"
    with _worker_held():
        r = client.post("/cluster/rename", data={"folder": str(folder), "name": "2026-06 Portugal",
                                                 "remember_place": "1"}, headers=FETCH)
        body = r.json()
        assert r.status_code == 200 and body["name"] == dst.name and body["folder"] == str(dst)
        assert unquote(body["redirect"]) == f"/clusters/view?folder={dst}"
        assert folder.is_dir() and not dst.exists()                          # not yet
        page = client.get("/clusters/view", params={"folder": str(dst)}).text   # where the page goes meanwhile
        assert "Renaming" in page and "Not a cluster folder" not in page and f"rename:{folder}" in page
        r = client.post("/cluster/rename", data={"folder": str(dst), "name": "x"})   # the new name is busy too
        assert r.status_code == 409
    main.wait_for_apply()
    assert dst.is_dir() and not folder.exists() and mover.read_manifest(dst)["name"] == dst.name
    assert [p["name"] for p in config.load().named_places] == ["Portugal"]
    # without remember_place, and over a form: redirected to the new folder's page
    other = Path(cfg.root) / "2026-06 Lisboa"
    r = client.post("/cluster/rename", data={"folder": str(dst), "name": other.name})
    assert r.status_code == 303 and unquote(r.headers["location"]) == f"/clusters/view?folder={other}"
    main.wait_for_apply()
    assert other.is_dir() and [p["name"] for p in config.load().named_places] == ["Portugal"]


def test_failed_job_is_reported_on_the_pages_for_a_while(client, library, monkeypatch):
    cfg, trip, folder = _applied(client, "trip")
    monkeypatch.setattr(main, "RENAME_SYNC_MAX", 0)

    def in_use(cfg, folder, name, progress=None):
        raise mover.FolderInUse("a file in it is still open")
    monkeypatch.setattr(mover, "rename", in_use)
    client.post("/cluster/rename", data={"folder": str(folder), "name": "elsewhere"})
    main.wait_for_apply()
    key = f"rename:{folder}"
    assert client.get("/api/status").json()["queue"][key] == "failed: FolderInUse: a file in it is still open"
    for page in (client.get("/clusters").text, client.get("/clusters/view", params={"folder": str(folder)}).text):
        assert "failed" in page and "still open" in page
    assert events.read(limit=1, kind="review")[0]["action"] == "rename_failed"
    assert folder.is_dir()
    main._jobs_failed[key]["at"] = 0                                         # long ago: forgotten
    assert key not in client.get("/api/status").json()["queue"] and "still open" not in client.get("/clusters").text
    main._jobs_failed["rename:f"] = {"error": "x", "at": 9e12, "type": "rename", "folder": "f", "name": "n"}
    with _worker_held():
        main.queue_job("rename", Path("f"), "n", new_name="m")              # tried again: the failure goes
        assert "rename:f" in main._jobs and "rename:f" not in main._jobs_failed
        main._jobs.pop("rename:f")                                           # (and the worker skips it)
    main.wait_for_apply()
    assert "rename:f" not in main._jobs_failed


def test_a_proposal_moving_into_the_folder_makes_it_busy(client, library):
    cfg, local, folder = _applied(client)
    trip_folder = mover.target_folder(cfg, _kind("trip"))
    assert main.folder_job(cfg, folder) is None
    with _worker_held():
        props = cluster.load_proposals()
        props[local["id"]]["status"] = "approved"                           # photos being added to it
        cluster.save_proposals(props)
        main.queue_apply(local["id"])
        assert main.folder_job(cfg, folder) == local["id"]
        assert main.folder_job(cfg, trip_folder) is None                     # another folder: free
        photo = mover.read_manifest(folder)["photos"][0]["dst"]
        r = client.post("/cluster/move_out", data={"folder": str(folder), "photo": photo})
        assert r.status_code == 409 and local["name"] in r.text and "Moving" in r.text
    main.wait_for_apply()
    assert _kind("local")["status"] == "applied"


# --- dry-run: nothing moves, on any of the new paths ---------------------------------------------

def test_dry_run_refuses_undo_rename_and_put_back_before_queueing(client, library, monkeypatch):
    cfg, local, folder = _applied(client)
    photo = mover.read_manifest(folder)["photos"][0]["dst"]
    client.post("/cluster/move_out", data={"folder": str(folder), "photo": photo})
    src = mover.read_manifest(folder)["corrections"][0]["src"]
    inboxes = [Path(i["path"]) for i in cfg.inboxes]
    before = _files(Path(cfg.root), *inboxes)
    _set_dry_run(True)
    monkeypatch.setattr(main, "RENAME_SYNC_MAX", 0)                          # the rename would be queued
    for path, data in (("/cluster/undo", {"folder": str(folder)}),
                       ("/cluster/put_back", {"folder": str(folder), "src": src}),
                       ("/cluster/rename", {"folder": str(folder), "name": "elsewhere"})):
        r = client.post(path, data=data)
        assert r.status_code == 409 and "Dry-run is on" in r.text, path
    assert not main._jobs and main._apply_queue.empty()
    main.wait_for_apply()
    assert _files(Path(cfg.root), *inboxes) == before


def test_dry_run_switched_on_while_jobs_wait_moves_nothing(client, library, monkeypatch):
    cfg, local, folder = _applied(client)
    trip = _kind("trip")
    client.post(f"/proposal/{trip['id']}/approve")
    main.wait_for_apply()
    trip_folder = mover.target_folder(cfg, trip)
    photo = mover.read_manifest(folder)["photos"][0]["dst"]
    client.post("/cluster/move_out", data={"folder": str(folder), "photo": photo})
    src = mover.read_manifest(folder)["corrections"][0]["src"]
    monkeypatch.setattr(main, "RENAME_SYNC_MAX", 0)
    inboxes = [Path(i["path"]) for i in cfg.inboxes]
    with _worker_held():
        client.post("/cluster/put_back", data={"folder": str(folder), "src": src})
        client.post("/cluster/undo", data={"folder": str(trip_folder)})
        r = client.post("/cluster/rename", data={"folder": str(folder), "name": "elsewhere"})
        assert r.status_code == 409, (r.headers, list(main._jobs))
        assert len(main._jobs) == 2                                          # put back and undo wait
        before = _files(Path(cfg.root), *inboxes)
        _set_dry_run(True)                                                   # switched on before they run
    main.wait_for_apply()
    assert _files(Path(cfg.root), *inboxes) == before
    queue = client.get("/api/status").json()["queue"]
    assert queue[f"undo:{trip_folder}"].startswith("failed: dry-run is on")
    assert queue[f"put_back:{folder}:{src}"].startswith("failed: dry-run is on")
    assert _kind("trip")["status"] == "applied"                              # not undone, not rejected
    # the rename alone, while dry-run switches on
    _set_dry_run(False)
    with _worker_held():
        client.post("/cluster/rename", data={"folder": str(trip_folder), "name": "elsewhere"})
        _set_dry_run(True)
    main.wait_for_apply()
    assert trip_folder.is_dir() and not (Path(cfg.root) / "elsewhere").exists()
    assert _files(Path(cfg.root), *inboxes) == before
    assert {e["action"] for e in events.read(limit=20, kind="review")} >= {"undo_failed", "put_back_failed",
                                                                           "rename_failed"}
