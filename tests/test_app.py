"""End-to-end tests of the web service through FastAPI's TestClient: every page and every
form action, against the synthetic library. No browser; see tests/e2e for that."""
from __future__ import annotations

import io
import json
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote, unquote

import pytest
from fastapi.testclient import TestClient

from app import cluster, config, events, ingest, main, mover
from tests import synth


@pytest.fixture
def client(library):
    main._state.update({"last_run": None, "last_stats": {}, "running": False, "error": None})
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def _wait_run(timeout: float = 30) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if main._state["last_run"] and not main._state["running"]:
            return main._state["last_stats"]
        time.sleep(0.1)
    raise AssertionError(f"pipeline did not finish: {main._state}")


def _force_uncertain(pid: str, i: int = 0) -> str:
    """Mark one photo of a proposal low-confidence (as a GPS-less photo far from any neighbour would be)."""
    props = cluster.load_proposals()
    props[pid]["photos"][i].update(conf=0.6, uncertain=True)
    props[pid]["n_uncertain"] = sum(p["uncertain"] for p in props[pid]["photos"])
    cluster.save_proposals(props)
    return props[pid]["photos"][i]["path"]


def _proposals(kind: str | None = None) -> dict:
    props = cluster.load_proposals()
    return {p["kind"]: p for p in props.values()} if kind is None else \
        next(p for p in props.values() if p["kind"] == kind)


# --- pages ------------------------------------------------------------------------------

def test_pages_render_empty(client):
    for path, text in [("/", "Dashboard"), ("/review", "No open proposals"), ("/clusters", "No clusters applied yet"),
                       ("/log", "filter:"), ("/settings", "Save settings")]:
        r = client.get(path)
        assert r.status_code == 200 and text in r.text, path
    assert "LIVE" in client.get("/").text                        # badge in the header (tests run live)
    cfg = config.load()
    cfg.dry_run = True
    config.save(cfg)
    assert "dry-run" in client.get("/").text
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/ui.js").status_code == 200
    assert f'src="/static/ui.js?v={main.STATIC_VERSION}"' in client.get("/").text and len(main.STATIC_VERSION) == 10


def test_api_status(client):
    s = client.get("/api/status").json()
    assert s == {"state": main._state, "pending": 0, "dry_run": False, "approved": 0, "applying": {}}


def test_startup_writes_config_and_schedules_scan(client):
    assert config.CONFIG_PATH.exists()
    for _ in range(100):                                                    # the warm-up thread fills the cache
        if cluster._records_cache["filled"] is not None:
            break
        time.sleep(0.1)
    assert len(cluster._records_cache["filled"]) > 0
    job = main.scheduler.get_job("scan")
    assert job is not None and job.trigger.interval.total_seconds() == config.load().scan_interval_min * 60


# --- run & review ---------------------------------------------------------------------------

def test_run_now_produces_proposals_and_dry_run_moves_nothing(client, library):
    cfg = config.load()
    cfg.dry_run = True
    config.save(cfg)
    r = client.post("/run")
    assert r.status_code == 303 and r.headers["location"] == "/"
    stats = _wait_run()
    assert stats["trigger"] == "manual" and stats["cluster"]["proposals"] == 3 and stats["applied"] == 0
    assert stats["ingest"]["total"] == library.n and stats["ingest"]["new"] == 0
    assert all(p.exists() for p in library.paths)
    page = client.get("/review").text
    assert "Proposed clusters (3)" in page and "2026-06 Lisbon, Sevilla" in page and "Ludwigsburg" in page
    assert ">Approve</button>" in page and "Approve &amp; move" not in page and "(25 Fotos)" in page   # dry-run wording
    assert client.get("/api/status").json()["pending"] == 3
    assert "Open proposals" in client.get("/").text and "run" in client.get("/log?kind=run").text
    assert events.read(limit=1, kind="run")[0]["new_photos"] == 0


def test_run_is_not_started_twice(client):
    assert main._lock.acquire(blocking=False)
    try:
        assert main.run_pipeline("test") == {"skipped": "already running"}
    finally:
        main._lock.release()


def test_run_error_is_shown_on_dashboard(client, monkeypatch):
    monkeypatch.setattr(main.ingest, "scan", lambda cfg: 1 / 0)
    out = main.run_pipeline("test")
    assert "division by zero" in out["error"]
    assert "Last run failed" in client.get("/").text


def test_approve_moves_photos_without_review_folder(client, library):
    cfg = config.load()
    cluster.run(cfg)
    local = _proposals("local")
    _force_uncertain(local["id"])
    r = client.post(f"/proposal/{local['id']}/approve")
    assert r.status_code == 303 and r.headers["location"] == "/review"
    assert _proposals("local")["status"] == "approved"                   # the request returns at once
    assert client.get("/api/status").json()["approved"] == 1
    main.wait_for_apply()
    folder = mover.target_folder(cfg, local)
    assert folder.is_dir() and not list(folder.rglob(cfg.review_dir))
    assert _proposals("local")["status"] == "applied" and client.get("/api/status").json()["approved"] == 0
    assert events.read(limit=1, kind="review")[0] == {**events.read(limit=1, kind="review")[0],
                                                       "action": "approve", "proposal": local["id"]}
    page = client.get("/clusters").text
    assert "Ludwigsburg" in page and "in review" not in page
    assert client.get("/api/status").json()["pending"] == 2


def test_dry_run_never_moves_anything_whatever_the_ui_does(client, library, tmp_path):
    """The invariant behind the incident of 2026-09-26: with dry-run on, every action the UI
    offers may change proposals but not a single file."""
    cfg = config.load()
    cfg.dry_run, cfg.auto_apply_trips, cfg.auto_apply_local, cfg.auto_apply_home = True, True, True, True
    config.save(cfg)
    inbox_before = sorted(str(p) for p in Path(cfg.inboxes[0]["path"]).rglob("*") if p.is_file())
    inbox_before += sorted(str(p) for p in Path(cfg.inboxes[1]["path"]).rglob("*") if p.is_file())
    cluster.run(cfg)
    kinds = _proposals()
    client.post(f"/proposal/{kinds['trip']['id']}/approve", data={"name": "2026-06 Portugal"})
    client.post(f"/proposal/{kinds['local']['id']}/rename", data={"name": "x"})
    client.post("/proposals/approve_all")
    main.wait_for_apply()
    assert "error" not in main.run_pipeline("test")                         # auto-apply flags on, dry-run wins
    paths = [r["path"] for r in cluster.everyday_records(cfg)][:2]
    client.post("/everyday/assign", data={"paths": paths, "target": "new", "kind": "local", "name": "by hand"})
    client.post("/cluster/undo", data={"folder": str(Path(cfg.root) / "nothing")})       # no manifest: no-op
    r = client.post("/cluster/rename", data={"folder": str(Path(cfg.root) / "nothing"), "name": "x"})
    assert r.status_code == 409 and "Dry-run is on" in r.text                            # refused, not a 500
    r = client.post("/cluster/move_out", data={"folder": str(Path(cfg.root) / "nothing"), "photo": inbox_before[0]})
    assert r.status_code == 409
    inbox_after = sorted(str(p) for p in Path(cfg.inboxes[0]["path"]).rglob("*") if p.is_file())
    inbox_after += sorted(str(p) for p in Path(cfg.inboxes[1]["path"]).rglob("*") if p.is_file())
    assert inbox_after == inbox_before
    assert not Path(cfg.root).exists() or not any(Path(cfg.root).rglob("*"))
    now = _proposals()
    assert now["trip"]["status"] == "approved" and now["trip"]["name"] == "2026-06 Portugal"   # recorded, not moved
    assert now["home"]["status"] == "approved"
    status = client.get("/api/status").json()
    assert status["dry_run"] is True and status["approved"] >= 3 and status["applying"] == {}
    page = client.get("/review").text
    assert "Dry-run is on" in page and "waiting for dry-run" in page and "Approve &amp; move" not in page

    # switching dry-run off (Settings) is what moves them, and asks nothing of the worker before
    form = {k: str(v) for k, v in config.load().as_dict().items() if not isinstance(v, (bool, list))}
    form.update({"inboxes": config.inboxes_text(cfg), "photo_extensions": "jpg"})   # dry_run absent: off
    r = client.post("/settings", data=form)
    assert "will%20be%20moved%20now" in r.headers["location"]
    main.wait_for_apply()
    assert _proposals()["trip"]["status"] == "applied" and (Path(cfg.root) / "2026-06 Portugal").is_dir()
    assert events.read(limit=1, kind="settings")[0]["dry_run"] is False


def test_approve_shows_progress_and_reports_failure(client, library, monkeypatch):
    cfg = config.load()
    cluster.run(cfg)
    trip = _proposals("trip")
    seen = []
    real = mover.apply

    def slow_apply(cfg, pr, reviewed=False, progress=None):
        m = real(cfg, pr, reviewed=reviewed, progress=lambda d, t, c=None: seen.append((d, t)))
        return m
    monkeypatch.setattr(mover, "apply", slow_apply)
    client.post(f"/proposal/{trip['id']}/approve")
    page = client.get("/review").text
    assert "Moving (1)" in page and 'data-poll="approved"' in page and "Proposed clusters (2)" in page
    main.wait_for_apply()
    assert seen[-1] == (trip["n"], trip["n"]) and _proposals("trip")["status"] == "applied"
    assert "Moving" not in client.get("/review").text
    assert client.post(f"/proposal/{trip['id']}/approve").status_code == 303   # applied: approve is a no-op
    assert _proposals("trip")["status"] == "applied"

    # a failing move leaves the proposal approved with the error shown, and can be retried
    local = _proposals("local")
    def boom(*a, **k):
        raise OSError("share gone")
    monkeypatch.setattr(mover, "apply", boom)
    client.post(f"/proposal/{local['id']}/approve")
    main.wait_for_apply()
    again = _proposals("local")
    assert again["status"] == "approved" and "share gone" in again["error"]
    page = client.get("/review").text
    assert "failed" in page and "share gone" in page and "retry" in page
    assert events.read(limit=1, kind="review")[0]["action"] == "apply_failed"
    monkeypatch.setattr(mover, "apply", real)
    main.queue_apply(local["id"])                                          # what "retry" does after a restart
    main.wait_for_apply()
    assert _proposals("local")["status"] == "applied" and _proposals("local")["error"] is None


def test_approve_and_reject_take_the_name_along(client, library):
    cfg = config.load()
    cluster.run(cfg)
    trip, local = _proposals("trip"), _proposals("local")
    client.post(f"/proposal/{trip['id']}/approve", data={"name": "2026-06 Portugal", "remember_place": "1"})
    assert _proposals("trip")["name"] == "2026-06 Portugal" and _proposals("trip")["status"] == "approved"
    assert [p["name"] for p in config.load().named_places] == ["Portugal"]
    main.wait_for_apply()
    assert (Path(cfg.root) / "2026-06 Portugal").is_dir()
    client.post(f"/proposal/{local['id']}/reject", data={"name": "2026-06-27 Barock"})
    assert _proposals("local")["name"] == "2026-06-27 Barock" and _proposals("local")["status"] == "rejected"
    kinds = [e["action"] for e in events.read(limit=6, kind="review")]
    assert kinds.count("rename") == 2
    client.post(f"/proposal/{local['id']}/reject", data={"name": ""})       # empty name: ignored
    assert _proposals("local")["name"] == "2026-06-27 Barock"


def test_concurrent_edits_do_not_lose_updates(client, library):
    """Rename and approve of the same proposal from two threads: both must land."""
    import threading
    cfg = config.load()
    cluster.run(cfg)
    trip = _proposals("trip")
    real_save = cluster.save_proposals

    def slow_save(props):                                                   # widen the race window
        time.sleep(0.2)
        real_save(props)
    cluster.save_proposals = slow_save
    try:
        t1 = threading.Thread(target=lambda: client.post(f"/proposal/{trip['id']}/rename", data={"name": "2026-06 X"}))
        t2 = threading.Thread(target=lambda: client.post(f"/proposal/{trip['id']}/approve"))
        t1.start()
        time.sleep(0.05)
        t2.start()
        t1.join()
        t2.join()
    finally:
        cluster.save_proposals = real_save
    pr = _proposals("trip")
    assert pr["name"] == "2026-06 X" and pr["status"] in ("approved", "applied")
    main.wait_for_apply()


def test_approve_all_queues_every_pending_proposal(client, library):
    cfg = config.load()
    cluster.run(cfg)
    props = cluster.load_proposals()
    trip = next(p for p in props.values() if p["kind"] == "trip")
    props[trip["id"]]["status"] = "ongoing"                                # waiting for a home photo: not touched
    cluster.save_proposals(props)
    local = _proposals("local")
    page = client.get("/review").text
    assert "Approve &amp; move all (2)" in page and 'action="/proposals/approve_all"' in page
    r = client.post("/proposals/approve_all", data={f"name_{local['id']}": "2026-06-27 Barock",
                                                    f"remember_place_{local['id']}": "1"})
    assert r.status_code == 303 and r.headers["location"] == "/review"
    now = _proposals()
    assert now["trip"]["status"] == "ongoing"
    assert now["local"]["status"] == "approved" and now["home"]["status"] == "approved"
    assert now["local"]["name"] == "2026-06-27 Barock" and [p["name"] for p in config.load().named_places] == ["Barock"]
    assert client.get("/api/status").json()["approved"] == 2
    main.wait_for_apply()
    now = _proposals()
    assert now["local"]["status"] == "applied" and now["home"]["status"] == "applied"
    assert (Path(cfg.root) / "2026-06-27 Barock").is_dir()
    assert "Approve &amp; move all" not in client.get("/review").text     # nothing pending any more
    assert client.post("/proposals/approve_all").status_code == 303        # idempotent


def test_rename_over_fetch_returns_json(client, library):
    cfg = config.load()
    cluster.run(cfg)
    trip = _proposals("trip")
    r = client.post(f"/proposal/{trip['id']}/rename", data={"name": "2026-06 Portugal"},
                    headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200 and r.json()["name"] == "2026-06 Portugal"
    client.post(f"/proposal/{trip['id']}/approve")
    main.wait_for_apply()
    folder = mover.target_folder(cfg, _proposals("trip"))
    r = client.post("/cluster/rename", data={"folder": str(folder), "name": "2026-06 Lissabon"},
                    headers={"X-Requested-With": "fetch"})
    body = r.json()
    assert r.status_code == 200 and body["name"] == "2026-06 Lissabon"
    assert body["redirect"].startswith("/clusters/view?folder=")
    assert (Path(cfg.root) / "2026-06 Lissabon").is_dir()
    page = client.get("/review").text
    assert "data-autosave" in page and page.count("rename</button>") == page.count("<noscript><button")


def test_cluster_rename_in_use_is_reported_not_500(client, library, monkeypatch):
    cfg = config.load()
    cluster.run(cfg)
    local = _proposals("local")
    client.post(f"/proposal/{local['id']}/approve")
    main.wait_for_apply()
    folder = mover.target_folder(cfg, local)

    def in_use(cfg, folder, name):
        raise mover.FolderInUse("busy")
    monkeypatch.setattr(mover, "rename", in_use)
    r = client.post("/cluster/rename", data={"folder": str(folder), "name": "x"}, headers={"X-Requested-With": "fetch"})
    assert r.status_code == 423 and r.json() == {"ok": False, "error": "busy"}
    r = client.post("/cluster/rename", data={"folder": str(folder), "name": "x"})
    assert r.status_code == 423 and "Not renamed" in r.text


def test_reject_rename_toggle(client, library):
    cfg = config.load()
    cluster.run(cfg)
    trip, home = _proposals("trip"), _proposals("home")

    r = client.post(f"/proposal/{trip['id']}/rename", data={"name": '2026-06 Portugal: "Sommer"'})
    assert r.status_code == 303 and _proposals("trip")["name"] == "2026-06 Portugal- -Sommer-"
    assert "2026-06 Portugal- -Sommer-" in client.get("/review").text

    path = trip["photos"][3]["path"]
    r = client.post(f"/proposal/{trip['id']}/toggle", data={"path": path, "back": f"/review?open={trip['id']}#x"})
    assert r.headers["location"] == f"/review?open={trip['id']}#x" and _proposals("trip")["excluded"] == [path]
    page = client.get(f"/review?open={trip['id']}").text                  # fallback keeps the cluster open
    assert f'id="{trip["id"]}"' in page and page.count("<details open>") >= 2
    r = client.post(f"/proposal/{trip['id']}/toggle", data={"path": path}, headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200 and r.json() == {"ok": True, "status": "pending", "name": _proposals("trip")["name"],
                                                 "excluded": False}
    assert _proposals("trip")["excluded"] == []

    client.post(f"/proposal/{home['id']}/reject")
    assert _proposals("home")["status"] == "rejected"
    assert client.get("/api/status").json()["pending"] == 2                # trip + local remain
    # rename and toggled-out photos survive the next run; rejected photos become everyday
    client.post(f"/proposal/{trip['id']}/toggle", data={"path": path})
    cluster.run(cfg)
    assert _proposals("trip")["name"] == "2026-06 Portugal- -Sommer-"
    assert _proposals("trip")["excluded"] == [path]
    assert _proposals("home")["status"] == "rejected"
    assert client.post("/proposal/nope/approve").headers["location"] == "/review"


# --- clusters ------------------------------------------------------------------------------------

def test_name_unnamed_burst_then_view_move_out_undo(client, library):
    cfg = config.load()
    cluster.run(cfg)
    home = _proposals("home")
    client.post(f"/proposal/{home['id']}/approve")
    main.wait_for_apply()
    folder = mover.target_folder(cfg, home)
    page = client.get("/review").text
    assert "Bursts waiting for a name (1)" in page and "Name it" in page
    assert "Bursts waiting for a name" in client.get("/").text

    r = client.post("/cluster/rename", data={"folder": str(folder), "name": "2026-06-30 Hannas Geburtstag"})
    dst = Path(cfg.root) / "2026-06-30 Hannas Geburtstag"
    assert r.status_code == 303 and unquote(r.headers["location"]) == f"/clusters/view?folder={dst}"
    assert dst.is_dir() and not folder.exists()
    assert "Bursts waiting for a name (0)" in client.get("/review").text

    view = client.get("/clusters/view", params={"folder": str(dst)})
    assert view.status_code == 200 and "Hannas Geburtstag" in view.text and "remove from cluster" in view.text
    m = mover.read_manifest(dst)
    photo = m["photos"][0]["dst"]
    r = client.post("/cluster/move_out", data={"folder": str(dst), "photo": photo})
    assert r.status_code == 303 and not Path(photo).exists()
    assert len(mover.read_manifest(dst)["photos"]) == 24 and "Removed from this cluster" in client.get(
        "/clusters/view", params={"folder": str(dst)}).text

    r = client.post("/cluster/undo", data={"folder": str(dst)})
    assert r.status_code == 303 and r.headers["location"] == "/clusters" and not dst.exists()
    assert len(list(Path(cfg.inboxes[0]["path"]).glob("*.jpg")) + list(Path(cfg.inboxes[1]["path"]).glob("*.jpg"))) \
        == library.n


def test_everyday_photos_move_on_request_with_progress(client, library):
    cfg = config.load()
    cluster.run(cfg)
    n = len(mover.everyday_movable(cfg, cfg.everyday_keep_days))
    assert n == 10 and cfg.everyday_keep_days == 4.0
    cfg.everyday_keep_days = 10_000                                         # keep everything: nothing movable
    config.save(cfg)
    assert "Nothing to move" in client.get("/everyday").text
    assert mover.everyday_movable(cfg, cfg.everyday_keep_days) == []
    cfg.everyday_keep_days = 4.0
    config.save(cfg)
    page = client.get("/everyday").text
    assert f"Move {n} everyday photos into YYYY/MM/" in page
    assert client.get("/api/status").json()["applying"] == {}
    r = client.post("/everyday/move_all")
    assert r.status_code == 303 and r.headers["location"] == "/everyday"
    client.post("/everyday/move_all")                                       # already queued: no duplicate
    main.wait_for_apply()
    assert (Path(cfg.root) / "2026" / "06").is_dir() and len(list((Path(cfg.root) / "2026").rglob("*.jpg"))) == n
    assert len(cluster.everyday_records(cfg)) == 0
    page = client.get("/everyday").text
    assert "Nothing to move" in page or "No everyday photos" in page
    assert events.read(limit=1, kind="review")[0] == {**events.read(limit=1, kind="review")[0],
                                                       "action": "move_everyday", "n": n}
    # the scheduled run does not move everyday photos unless asked to
    library.photo(Path(cfg.inboxes[0]["path"]), synth.T0 + timedelta(days=0, hours=1), synth.HOME)
    cluster.run(cfg)
    assert main.run_pipeline("test")["applied"] == 0 and len(cluster.everyday_records(cfg)) == 1
    cfg.auto_apply_everyday = True
    config.save(cfg)
    assert main.run_pipeline("test")["applied"] == 1


def test_everyday_move_refused_in_dry_run(client, library):
    cfg = config.load()
    cfg.dry_run = True
    config.save(cfg)
    cluster.run(cfg)
    page = client.get("/everyday").text
    assert "dry-run is on, nothing moves" in page and "/everyday/move_all" not in page
    assert client.post("/everyday/move_all").status_code == 409


def test_progress_reports_the_current_file(cfg, library):
    seen = []
    cluster.run(cfg)
    local = next(p for p in cluster.load_proposals().values() if p["kind"] == "local")
    mover.apply(cfg, local, progress=lambda d, t, c: seen.append((d, t, c)))
    assert seen[0][0] == 0 and seen[0][2]["file"].startswith("IMG_") and seen[0][2]["bytes"] > 0
    assert seen[-1] == (local["n"], local["n"], None)


def test_removed_photo_is_everyday_and_can_be_put_back(client, library, monkeypatch):
    cfg = config.load()
    cfg.sidecar_cleanup = "never"                        # the record travels with the photo (no exiftool here)
    config.save(cfg)
    scans = []
    real_scan = ingest.scan
    monkeypatch.setattr(ingest, "scan", lambda c, **k: scans.append(1) or real_scan(c, **k))
    cluster.run(cfg)
    local = _proposals("local")
    client.post(f"/proposal/{local['id']}/approve")
    main.wait_for_apply()
    folder = mover.target_folder(cfg, local)
    m = mover.read_manifest(folder)
    photo = m["photos"][0]["dst"]
    n_everyday = len(cluster.everyday_records(cfg))
    r = client.post("/cluster/move_out", data={"folder": str(folder), "photo": photo})
    assert r.status_code == 303
    src = mover.read_manifest(folder)["corrections"][0]["src"]
    assert Path(src).exists() and src in _proposals("local")["excluded"] and scans == [1]   # indexed at once
    assert len(cluster.everyday_records(cfg)) == n_everyday + 1         # visible on the Everyday page at once
    assert src in client.get("/everyday?month=2026-06").text
    page = client.get("/clusters/view", params={"folder": str(folder)}).text
    assert "Removed from this cluster" in page and "put back" in page and "remove from cluster" in page
    assert "not this trip" not in page
    r = client.post("/cluster/put_back", data={"folder": str(folder), "src": src})
    assert r.status_code == 303 and Path(photo).exists() and not Path(src).exists()
    assert src not in _proposals("local")["excluded"]
    assert len(cluster.everyday_records(cfg)) == n_everyday
    assert "Removed from this cluster" not in client.get("/clusters/view", params={"folder": str(folder)}).text
    assert client.post("/cluster/put_back", data={"folder": str(folder), "src": src}).status_code == 303   # no-op

    # a removal made before exclusions were recorded is repaired by the next run
    mover.move_out(cfg, folder, Path(photo))
    props = cluster.load_proposals()
    props[local["id"]]["excluded"] = []
    cluster.save_proposals(props)
    assert len(cluster.everyday_records(cfg)) == n_everyday
    cluster.run(cfg)
    assert src in _proposals("local")["excluded"] and len(cluster.everyday_records(cfg)) == n_everyday + 1

    # a removed photo that has vanished cannot be put back: a page says so, not a 500
    Path(src).unlink()
    r = client.post("/cluster/put_back", data={"folder": str(folder), "src": src})
    assert r.status_code == 404 and "Not put back" in r.text


def test_undo_marks_applied_proposal_rejected(client, library):
    cfg = config.load()
    cluster.run(cfg)
    trip = _proposals("trip")
    client.post(f"/proposal/{trip['id']}/approve")
    main.wait_for_apply()
    folder = mover.target_folder(cfg, trip)
    assert _proposals("trip")["status"] == "applied"
    client.post("/cluster/undo", data={"folder": str(folder)})
    assert _proposals("trip")["status"] == "rejected"
    assert "No clusters applied yet" in client.get("/clusters").text


def test_detect_home_from_photos(client, library):
    cfg = config.load()
    cfg.home_lat, cfg.home_lon = 0.0, 0.0
    config.save(cfg)
    for d in range(40):                                                   # a normal stretch at home
        library.photo(Path(cfg.inboxes[0]["path"]), synth.T0 + timedelta(days=40 + d, hours=19), synth.HOME)
    r = client.post("/settings/detect_home")
    assert r.status_code == 303 and "Home%20set%20to%2048.9440" in r.headers["location"]
    new = config.load()
    assert (new.home_lat, new.home_lon) == (48.944, 9.118)
    assert "Home set to 48.9440" in client.get(r.headers["location"]).text
    assert events.read(limit=1, kind="settings")[0]["detected"]["days"] >= 9


def test_detect_home_without_gps(client, tmp_path):
    cfg = config.load()
    cfg.inboxes = [{"path": str(tmp_path / "empty"), "name": "e"}]
    config.save(cfg)
    r = client.post("/settings/detect_home")
    assert "No%20photos%20with%20GPS" in r.headers["location"]


def test_rename_remembers_place(client, library):
    cfg = config.load()
    cluster.run(cfg)
    local = _proposals("local")
    client.post(f"/proposal/{local['id']}/rename", data={"name": "2026-06-27 Blühendes Barock", "remember_place": "1"})
    places = config.load().named_places
    assert len(places) == 1 and places[0]["name"] == "Blühendes Barock"
    assert abs(places[0]["lat"] - synth.LUDWIGSBURG[0]) < 0.01 and places[0]["radius_km"] == 0.5
    assert events.read(limit=1, kind="settings")[0]["place"]["name"] == "Blühendes Barock"
    # the label is used from the next scan on
    ingest.scan(cfg := config.load())
    assert ingest.read_sidecar(Path(local["photos"][0]["path"]), cfg)["place"]["place"] == "Blühendes Barock"
    client.post(f"/proposal/{local['id']}/rename", data={"name": "no place"})
    assert len(config.load().named_places) == 1                                    # unticked: unchanged


def test_cluster_rename_remembers_place(client, library):
    cfg = config.load()
    cluster.run(cfg)
    trip = _proposals("trip")
    client.post(f"/proposal/{trip['id']}/approve")
    main.wait_for_apply()
    folder = mover.target_folder(cfg, trip)
    client.post("/cluster/rename", data={"folder": str(folder), "name": "2026-06 Portugal", "remember_place": "1"})
    places = config.load().named_places
    assert [p["name"] for p in places] == ["Portugal"] and 10 < places[0]["radius_km"] < 60   # Lisbon, 95th pct
    strip = main._DATE_PREFIX.sub
    assert strip("", "2026-06-01..04 Alps") == "Alps" and strip("", "2026 Alps") == "Alps"


def test_cluster_view_of_non_cluster_folder(client, tmp_path):
    r = client.get("/clusters/view", params={"folder": str(tmp_path)})
    assert r.status_code == 200 and "Not a cluster folder" in r.text


# --- settings -----------------------------------------------------------------------------------

def test_settings_save_coerces_and_reschedules(client):
    cfg = config.load()
    form = {k: str(v) for k, v in cfg.as_dict().items() if not isinstance(v, (bool, list))}
    form.update({"inboxes": config.inboxes_text(cfg), "photo_extensions": "jpg, heic",
                 "scan_interval_min": "42", "home_lat": "48.1", "copy_instead_of_move": "on"})
    r = client.post("/settings", data=form)
    assert r.status_code == 303 and r.headers["location"] == "/settings"
    new = config.load()
    assert new.scan_interval_min == 42 and new.home_lat == 48.1 and new.photo_extensions == ["jpg", "heic"]
    assert new.copy_instead_of_move is True and new.dry_run is False        # checkbox absent -> off
    assert main.scheduler.get_job("scan").trigger.interval.total_seconds() == 42 * 60
    assert events.read(limit=1, kind="settings")[0]["changed"]
    page = client.get("/settings").text
    assert 'value="42"' in page and 'name="copy_instead_of_move" checked' in page
    assert "LIVE" in client.get("/").text                                  # header badge, dry-run off


def test_live_mode_auto_applies_trips_with_review_folder(client, library, monkeypatch):
    cfg = config.load()
    cfg.dry_run, cfg.auto_apply_trips, cfg.auto_apply_everyday = False, True, True
    config.save(cfg)
    cluster.run(cfg)
    trip = _proposals("trip")
    doubtful = trip["photos"][5]["file"]                                  # recomputed by the pipeline run:
    real = cluster._gps_conf                                              # make one photo low-confidence for real
    monkeypatch.setattr(cluster, "_gps_conf", lambda r: 0.6 if r["file"] == doubtful else real(r))

    stats = main.run_pipeline("test")
    assert stats["applied"] == 1 + 10                                      # trip + everyday photos
    folder = mover.target_folder(cfg, trip)
    assert folder.is_dir() and list(folder.rglob(cfg.review_dir))          # auto-applied: _review used
    assert _proposals("trip")["status"] == "applied" and _proposals("local")["status"] == "pending"
    assert "in review" in client.get("/clusters").text
    assert (Path(cfg.root) / "2026" / "06").is_dir()


def test_live_mode_applies_approved_without_review_folder(client, library):
    cfg = config.load()
    cfg.dry_run = False
    config.save(cfg)
    cluster.run(cfg)
    props = cluster.load_proposals()
    local = next(p for p in props.values() if p["kind"] == "local")
    local["status"] = "approved"
    cluster.save_proposals(props)
    main.run_pipeline("test")
    folder = mover.target_folder(cfg, local)
    assert folder.is_dir() and not list(folder.rglob(cfg.review_dir))


# --- thumbnails ----------------------------------------------------------------------------------

def test_thumb_nas_then_generated_then_placeholder(client, library, tmp_path):
    from PIL import Image
    photo = library.paths[0]                                             # fake bytes, not decodable
    r = client.get("/thumb", params={"path": str(photo)})
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    Image.new("RGB", (800, 600), "red").save(photo, "JPEG")             # now a real photo
    r = client.get("/thumb", params={"path": str(photo)})
    assert r.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(r.content)) as im:
        assert im.size == (440, 330)
    t = photo.parent / "@eaDir" / photo.name / "SYNOPHOTO_THUMB_M.jpg"
    t.parent.mkdir(parents=True)
    t.write_bytes(b"THUMB")
    assert client.get("/thumb", params={"path": str(photo)}).content == b"THUMB"     # NAS wins
    outside = tmp_path / "outside.jpg"
    Image.new("RGB", (80, 60)).save(outside, "JPEG")
    r = client.get("/thumb", params={"path": str(outside)})
    assert r.headers["content-type"].startswith("image/svg+xml")                    # not under inbox/root
    r = client.get("/thumb", params={"path": str(Path(cfg_root(client)) / "x.mp4")})
    assert r.headers["content-type"].startswith("image/svg+xml") and b"video" in r.content.lower()


def cfg_root(client) -> str:
    return config.load().root


def test_media_serves_original_or_preview(client, library, tmp_path):
    from PIL import Image
    photo = library.paths[0]
    Image.new("RGB", (800, 600), "blue").save(photo, "JPEG")
    r = client.get("/media", params={"path": str(photo)})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == photo.read_bytes()
    heic_like = photo.with_suffix(".bmp")                                # not browser-native -> preview jpeg
    Image.new("RGB", (3000, 2000), "green").save(heic_like, "BMP")
    r = client.get("/media", params={"path": str(heic_like)})
    assert r.status_code == 200
    with Image.open(io.BytesIO(r.content)) as im:
        assert im.size == (2000, 1333)
    assert client.get("/media", params={"path": str(tmp_path / "nope.jpg")}).status_code == 404


def test_review_page_thumbnails_are_linked(client, library):
    cfg = config.load()
    cluster.run(cfg)
    page = client.get("/review").text
    assert f"/thumb?path={quote(str(library.paths[-1]), safe='')}" in page or "/thumb?path=" in page
    assert json.loads(client.get("/api/status").text)["pending"] == 3


def test_log_page_filters(client, library):
    events.log("review", proposal="p1", action="reject", name="x")
    events.log("correction", name="x", photo="a.jpg")
    page = client.get("/log?kind=review").text
    assert "proposal=p1" in page and "photo=a.jpg" not in page
    assert client.get("/log?kind=correction").text.count("photo=a.jpg") == 1
