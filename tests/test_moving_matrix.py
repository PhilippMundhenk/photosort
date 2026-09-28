"""Every way a file can move, against the three modes:

  dry-run on          nothing moves, whatever is switched on or clicked
  live, switches off  nothing moves unless a button is pressed; each button moves only its target
  live, one switch on the switch moves only its own kind, and only at the end of a run

"Everything automatic that can happen" is exercised in full: scheduled and manual runs, a
scan that finds new photos, the startup queue, the cache warm-up, home re-detection.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cluster, config, events, main, mover
from tests import synth


@pytest.fixture
def client(library):
    main._state.update({"last_run": None, "last_stats": {}, "running": False, "error": None, "progress": None,
                        "runs": 0})
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def _live(**switches) -> config.Config:
    cfg = config.load()
    cfg.dry_run = False
    for k in ("auto_apply_trips", "auto_apply_local", "auto_apply_home", "auto_apply_everyday"):
        setattr(cfg, k, switches.get(k, False))
    cfg.everyday_keep_days = 0                                   # every everyday photo is old enough
    config.save(cfg)
    return cfg


def _files_under(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".jpg", ".mp4")}


def _kinds() -> dict[str, dict]:
    return {p["kind"]: p for p in cluster.load_proposals().values() if p["status"] in ("pending", "ongoing")}


def _wait_run(timeout: float = 30) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if main._state["last_run"] and not main._state["running"]:
            main.wait_for_apply()
            return
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def _everything_automatic(client, library) -> None:
    """All the automatic activity a running service produces, with nothing clicked."""
    inbox = Path(library.cfg.inboxes[0]["path"])
    main.run_pipeline("schedule")                              # a scheduled run
    main.wait_for_apply()
    library.photo(inbox, datetime.now(timezone.utc) - timedelta(days=30), synth.HOME)   # a photo syncs in
    main.run_pipeline("schedule")
    main.wait_for_apply()
    client.post("/run")                                        # "Run now" (a button, but it only scans)
    _wait_run()
    main.queue_approved()                                      # what startup does after a restart
    main.wait_for_apply()
    main._warm_cache()
    cfg = config.load()
    cfg.home_lat, cfg.home_lon = 0.0, 0.0                      # home unset: the run detects it and re-scans
    config.save(cfg)
    main.run_pipeline("schedule")
    main.wait_for_apply()


def _assert_untouched(library) -> None:
    missing = [p for p in library.paths if not p.exists()]
    assert not missing, f"moved out of the inbox: {missing[:3]}..."
    assert _files_under(Path(library.cfg.root)) == set()
    assert not [e for e in events.read(limit=10000) if e["kind"] in ("apply", "undo", "correction")
                or e.get("action") in ("move_everyday", "move_everyday_failed")]


# --- live, all switches off: only a button moves files ------------------------------------------

def test_live_with_switches_off_moves_nothing_by_itself(client, library):
    _live()
    _everything_automatic(client, library)
    _assert_untouched(library)
    s = client.get("/api/status").json()
    assert s["dry_run"] is False and s["pending"] >= 3 and s["approved"] == 0 and s["queue"] == {}
    assert main.EVERYDAY_JOB not in list(main._apply_queue.queue)
    stats = main._state["last_stats"]
    assert stats["applied"] == 0 and stats["queued"] == 0


def test_live_switches_off_each_button_moves_only_its_target(client, library):
    cfg = _live()
    main.run_pipeline("test")
    kinds = _kinds()
    trip, local, home = kinds["trip"], kinds["local"], kinds["home"]
    trip_paths = {Path(p["path"]) for p in trip["photos"]}
    all_paths = set(library.paths)

    # approve one proposal: exactly its photos move, nothing else
    client.post(f"/proposal/{trip['id']}/approve")
    main.wait_for_apply()
    assert not any(p.exists() for p in trip_paths)
    assert all(p.exists() for p in all_paths - trip_paths)
    assert _files_under(mover.target_folder(cfg, trip)) and len(_files_under(Path(cfg.root))) == len(trip_paths)

    # a run afterwards moves nothing further
    main.run_pipeline("schedule")
    main.wait_for_apply()
    assert all(p.exists() for p in all_paths - trip_paths)

    # move everyday photos: only unclustered ones older than the keep window
    before = _files_under(Path(cfg.root))
    r = client.post("/everyday/move_all")
    assert r.status_code == 303
    main.wait_for_apply()
    moved = _files_under(Path(cfg.root)) - before
    clustered = {Path(p["path"]) for pr in (local, home) for p in pr["photos"]}
    assert moved and all(p.parent.parent.name == "2026" or p.parent.parent.parent.name == "2026" for p in moved)
    assert all(p.exists() for p in clustered)                    # pending clusters are not "everyday"

    # approve all: the remaining pending proposals, nothing else left to move
    client.post("/proposals/approve_all")
    main.wait_for_apply()
    assert not any(p.exists() for p in clustered)
    assert mover.target_folder(cfg, local).is_dir()
    assert (Path(cfg.root) / cfg.unnamed_dir / home["name"]).is_dir()

    # undo one cluster: its photos come back, the others stay
    client.post("/cluster/undo", data={"folder": str(mover.target_folder(cfg, local))})
    assert all(Path(p["path"]).exists() for p in local["photos"])
    assert not any(p.exists() for p in trip_paths)


# --- live, one switch on: it moves only its kind, at the end of a run ----------------------------

@pytest.mark.parametrize("switch,kind", [("auto_apply_trips", "trip"), ("auto_apply_local", "local"),
                                         ("auto_apply_home", "home"), ("auto_apply_everyday", "everyday")])
def test_one_switch_moves_only_its_own_kind(client, library, switch, kind):
    cfg = _live(**{switch: True})
    cluster.run(cfg)                                            # what the run will propose
    kinds = _kinds()
    if kind == "everyday":
        expected = {Path(r["path"]) for r in cluster.everyday_records(cfg)}
    else:
        expected = {Path(p["path"]) for p in kinds[kind]["photos"]}
    assert expected
    main.run_pipeline("schedule")
    main.wait_for_apply()
    assert not any(p.exists() for p in expected), f"{kind} not moved"
    assert all(p.exists() for p in set(library.paths) - expected), "something else moved"
    assert _files_under(Path(cfg.root))
    if kind == "home":
        assert (Path(cfg.root) / cfg.unnamed_dir / kinds["home"]["name"]).is_dir()      # waits for a name
    if kind != "everyday":
        assert cluster.load_proposals()[kinds[kind]["id"]]["status"] == "applied"
        assert cluster.load_proposals()[kinds[kind]["id"]]["auto"] is True
    # a second run, nothing new: nothing more moves
    remaining = {p for p in library.paths if p.exists()}
    main.run_pipeline("schedule")
    main.wait_for_apply()
    assert {p for p in library.paths if p.exists()} == remaining


# --- dry-run: nothing, whatever is on or clicked --------------------------------------------------

def test_dry_run_ignores_every_switch_and_every_automatic_path(client, library):
    cfg = _live(auto_apply_trips=True, auto_apply_local=True, auto_apply_home=True, auto_apply_everyday=True)
    cfg.dry_run = True
    config.save(cfg)
    _everything_automatic(client, library)
    _assert_untouched(library)
    kinds = _kinds()
    client.post(f"/proposal/{kinds['trip']['id']}/approve")    # approved, and still nothing moves
    client.post("/proposals/approve_all")
    assert client.post("/everyday/move_all").status_code == 409
    main.wait_for_apply()
    main.run_pipeline("schedule")                              # a run with approved proposals waiting
    main.wait_for_apply()
    _assert_untouched(library)
    assert client.get("/api/status").json()["approved"] >= 2   # they wait for dry-run to be switched off


def test_switching_dry_run_off_moves_only_what_was_approved(client, library):
    cfg = _live()
    cfg.dry_run = True
    config.save(cfg)
    main.run_pipeline("test")
    kinds = _kinds()
    client.post(f"/proposal/{kinds['local']['id']}/approve")
    _assert_untouched(library)
    form = {k: str(v) for k, v in config.load().as_dict().items() if not isinstance(v, (bool, list, dict))}
    r = client.post("/settings", data=form)                    # every checkbox unticked: dry-run off, switches off
    assert r.status_code == 303 and "will be moved now" in r.headers["location"].replace("%20", " ")
    main.wait_for_apply()
    local_paths = {Path(p["path"]) for p in kinds["local"]["photos"]}
    assert not any(p.exists() for p in local_paths)
    assert all(p.exists() for p in set(library.paths) - local_paths)
    main.run_pipeline("schedule")                              # and later runs move nothing more
    main.wait_for_apply()
    assert all(p.exists() for p in set(library.paths) - local_paths)
