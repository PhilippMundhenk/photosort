"""Latency of every feature on a 20 000-photo library, measured in one session through the web
layer (no browser): pages, polls, clicks, approvals, moves, undo, corrections, settings actions,
the scan and the clustering. Every number is printed (pytest -s) and bounded; the bounds are
what a user perceives as instant on a small NAS box, widened under the coverage tracer.

Run alone for the real numbers:  pytest -q -s tests/test_perf_features.py
"""
from __future__ import annotations

import os
import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cluster, config, ingest, main, mover
from tests import synth
from tests.synth import HOME, LISBON, LUDWIGSBURG, SEVILLE, T0

TRACED = 4.0 if sys.gettrace() is not None else 1.0
ROWS: list[tuple[str, float, float]] = []
pytestmark = pytest.mark.perf


def _t(name: str, fn, bound: float, repeat: int = 1):
    """Time fn() `repeat` times; the median is reported and bounded. Returns the last result."""
    times, out = [], None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        times.append(time.perf_counter() - t0)
    med = statistics.median(times)
    ROWS.append((name, med, bound * TRACED))
    print(f"\n  perf  {name:<52} {med * 1000:8.0f} ms   (bound {bound * TRACED * 1000:.0f} ms"
          f"{', traced' if TRACED > 1 else ''}{f', median of {repeat}' if repeat > 1 else ''})")
    if not os.environ.get("PERF_BASELINE"):                 # PERF_BASELINE=1: print everything, fail nothing
        assert med < bound * TRACED, f"{name}: {med:.3f} s exceeds {bound * TRACED:.2f} s"
    return out


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    """~20 500 photos: 8 000 everyday over three years, six trips of 2 000 (two phones), ten
    home bursts, 200 videos; records written directly (no exiftool)."""
    base = tmp_path_factory.mktemp("perf-features")
    for sub in ("phone-a", "phone-b", "sorted"):
        (base / sub).mkdir(parents=True)
    cfg = synth.make_config(base)
    config.save(cfg)
    from tests.conftest import _SESSION_TMP  # noqa: F401  (the data dir is the session's)
    lib = synth.Library(cfg)
    a, b = base / "phone-a", base / "phone-b"
    start = T0 - timedelta(days=3 * 365)
    for i in range(8000):                                                  # everyday, every ~3 h
        lib.photo(a if i % 3 else b, start + timedelta(hours=3.3 * i), HOME)
    for k in range(6):                                                     # trips: 10 days, 2 000 photos, 2 phones
        day = start + timedelta(days=60 + k * 170)
        where = LISBON if k % 2 else SEVILLE
        for i in range(2000):
            lib.photo(a if i % 2 else b, day + timedelta(minutes=7 * i), where, cam="phone-b" if i % 2 else "phone")
        lib.photo(a, day + timedelta(days=11), HOME)                       # back home
    for k in range(10):                                                    # home bursts
        day = start + timedelta(days=30 + k * 100, hours=14)
        for i in range(30):
            lib.photo(a, day + timedelta(minutes=6 * i), HOME)
    for i in range(200):                                                   # videos at home and away
        lib.video(a, start + timedelta(days=5 * i, hours=12), HOME if i % 4 else LISBON)
    for i in range(12):                                                    # a day out to add to
        lib.photo(a, T0 + timedelta(days=2, hours=11, minutes=10 * i), LUDWIGSBURG)
    return {"base": base, "cfg": cfg, "lib": lib, "n": lib.n}


@pytest.fixture(scope="module")
def client(big):
    from app import thumbs
    thumbs.wait()
    main._state.update({"last_run": None, "last_stats": {}, "running": False, "error": None, "progress": None,
                        "runs": 0})
    cfg = config.load()
    cfg.dry_run, cfg.generate_thumbnails = False, False
    config.save(cfg)
    with TestClient(main.app, follow_redirects=False) as c:
        yield c
    main.wait_for_apply()


FETCH = {"X-Requested-With": "fetch"}


def test_everything_on_twenty_thousand_photos(client, big):
    cfg = config.load()
    n = big["n"]
    assert n > 20000

    # --- the first scan (records exist: no exiftool) and the clustering -------------------------
    ingest.changed["all"] = True
    cluster._records_cache.update(key=None, filled=None)
    _t("load_records (first, 20k files)", lambda: cluster.load_records(cfg), 15.0)
    _t("records_filled (GPS fill)", lambda: cluster.records_filled(cfg), 2.0)
    st = _t("ingest.scan, first of the process (re-zone pass over 20k records)", lambda: ingest.scan(cfg), 25.0)
    assert st["rezoned"] is True and st["new"] == 0
    st = _t("ingest.scan, nothing new", lambda: ingest.scan(cfg), 3.0)
    assert st["rezoned"] is False and st["new"] == 0
    _t("cluster.run", lambda: cluster.run(cfg), 3.0)
    with main._lock:                                                       # the startup's own scan first
        pass
    stats = _t("run_pipeline (scan + cluster)", lambda: main.run_pipeline("test"), 20.0)
    assert stats["cluster"]["photos"] == n and stats["cluster"]["proposals"] >= 15
    props = cluster.load_proposals()
    kinds = [p["kind"] for p in props.values()]
    assert kinds.count("trip") == 6 and kinds.count("home") >= 10 and kinds.count("local") >= 1
    trip = next(p for p in props.values() if p["kind"] == "trip")
    home = next(p for p in props.values() if p["kind"] == "home")
    local = next(p for p in props.values() if p["kind"] == "local")

    # --- pages and polls ------------------------------------------------------------------------
    get = lambda path, **kw: (lambda: client.get(path, **kw))  # noqa: E731
    r = _t("GET /api/status", get("/api/status"), 0.02, repeat=20)
    assert r.status_code == 200
    r = _t("GET / (dashboard)", get("/"), 0.15, repeat=5)
    assert r.status_code == 200
    r = _t("GET /review (17 proposals, 12k photos)", get("/review"), 0.4, repeat=5)
    assert r.status_code == 200 and len(r.content) < 1_000_000, len(r.content)
    r = _t("GET /proposal/<trip>/photos (chunk of 200)", get(f"/proposal/{trip['id']}/photos"), 0.1, repeat=5)
    assert r.status_code == 200
    r = _t("GET /review?open=<trip>", get("/review", params={"open": trip["id"]}), 0.4, repeat=3)
    assert r.status_code == 200
    r = _t("GET /everyday (latest month)", get("/everyday"), 0.5, repeat=5)
    assert r.status_code == 200
    r = _t("GET /everyday?month=<busy month>", get("/everyday", params={"month": "2024-06"}), 0.5, repeat=3)
    assert r.status_code == 200
    _t("GET /clusters (none yet)", get("/clusters"), 0.1, repeat=5)
    _t("GET /log", get("/log"), 0.1, repeat=5)
    _t("GET /settings", get("/settings"), 0.1, repeat=5)
    _t("GET /thumb (placeholder)", get("/thumb", params={"path": trip["photos"][0]["path"]}), 0.05, repeat=10)

    # --- review actions -------------------------------------------------------------------------
    paths = [p["path"] for p in trip["photos"]]
    r = _t("POST toggle 1 photo", lambda: client.post(f"/proposal/{trip['id']}/toggle", data={"path": paths[0]},
                                                       headers=FETCH), 0.15, repeat=5)
    assert r.status_code == 200
    r = _t("POST toggle 50 photos", lambda: client.post(f"/proposal/{trip['id']}/toggle", data={"path": paths[1:51]},
                                                         headers=FETCH), 0.3)
    assert r.status_code == 200
    client.post(f"/proposal/{trip['id']}/toggle", data={"path": paths[:51]}, headers=FETCH)   # all back in
    rename = lambda: client.post(f"/proposal/{trip['id']}/rename", data={"name": "2024-01 Iberia"}, headers=FETCH)  # noqa: E731
    r = _t("POST rename (autosave)", rename, 0.15, repeat=5)
    assert r.status_code == 200
    r = _t("POST everyday/assign (12 photos, new cluster)",
           lambda: client.post("/everyday/assign", data={"paths": [str(p) for p in big["lib"].paths[-12:]],
                                                         "target": "new", "kind": "local", "name": "2026-06 Ausflug"}),
           0.6)
    assert r.status_code == 303
    r = _t("POST approve (2 000-photo trip)", lambda: client.post(f"/proposal/{trip['id']}/approve"), 0.2)
    assert r.status_code == 303
    r = _t("GET /review while the trip moves", get("/review"), 0.4, repeat=5)
    assert r.status_code == 200
    _t("GET /api/status while moving", get("/api/status"), 0.02, repeat=20)
    _t("wait for the move of 2 000 files", main.wait_for_apply, 60.0)
    assert cluster.load_proposals()[trip["id"]]["status"] == "applied"
    folder = mover.target_folder(cfg, cluster.load_proposals()[trip["id"]])
    _t("GET /clusters (1 cluster, after a move)", get("/clusters"), 0.3, repeat=3)
    r = _t("GET /clusters/view (2 000 photos)", get("/clusters/view", params={"folder": str(folder)}), 0.4, repeat=3)
    assert r.status_code == 200
    r = _t("POST approve_all (16 proposals)", lambda: client.post("/proposals/approve_all"), 0.5)
    assert r.status_code == 303
    _t("GET /review while 16 move", get("/review"), 0.4, repeat=5)
    _t("wait for the moves (10 000 files)", main.wait_for_apply, 240.0)
    _t("GET /review (empty queue)", get("/review"), 0.3, repeat=5)
    _t("GET /clusters (17 clusters)", get("/clusters"), 0.3, repeat=3)

    # --- corrections, renames, undo -------------------------------------------------------------
    m = mover.read_manifest(folder)
    photo = Path(m["photos"][0]["dst"])
    r = _t("POST cluster/move_out", lambda: client.post("/cluster/move_out",
                                                        data={"folder": str(folder), "photo": str(photo)}), 12.0)
    assert r.status_code == 303
    put_back = {"folder": str(folder), "src": m["photos"][0]["src"]}
    r = _t("POST cluster/put_back", lambda: client.post("/cluster/put_back", data=put_back), 0.5)
    assert r.status_code == 303
    rename_folder = {"folder": str(folder), "name": "2024-01 Spain"}
    r = _t("POST cluster/rename (2 000 files)",
           lambda: client.post("/cluster/rename", data=rename_folder, headers=FETCH), 10.0)
    assert r.status_code == 200
    renamed = Path(r.json()["folder"])
    home_folder = mover.target_folder(cfg, cluster.load_proposals()[home["id"]])
    r = _t("POST cluster/undo (30 files)", lambda: client.post("/cluster/undo", data={"folder": str(home_folder)}), 1.0)
    assert r.status_code == 303

    # --- everyday move, settings actions, a second run ------------------------------------------
    r = _t("POST everyday/move_all (8 000 files, queued)", lambda: client.post("/everyday/move_all"), 0.5)
    assert r.status_code == 303
    _t("GET /everyday while moving", get("/everyday"), 0.5, repeat=3)
    _t("wait for the everyday move (8 000 files)", main.wait_for_apply, 240.0)
    _t("GET /everyday (after the move)", get("/everyday"), 0.5, repeat=3)
    _t("POST settings/detect_home", lambda: client.post("/settings/detect_home"), 1.0)
    _t("POST settings (save)", lambda: client.post("/settings", data={k: str(v) for k, v in cfg.as_dict().items()
                                                                     if not isinstance(v, (bool, list, dict))}), 0.3)
    _t("run_pipeline after everything moved", lambda: main.run_pipeline("test"), 20.0)
    _t("POST settings/sidecars/purge", lambda: client.post("/settings/sidecars/purge"), 30.0)
    r = _t("GET /history (a dozen actions, 20k operations)", get("/history"), 0.6, repeat=3)
    assert r.status_code == 200
    from app import journal
    apply_batch = next(b for b in journal.batches() if b["action"] == "apply" and b["n"] >= 1990)
    one = Path(apply_batch["ops"][7]["src"]).name
    r = _t("GET /history?file=<one of 20k files>", get("/history", params={"file": one}), 0.6, repeat=3)
    assert r.status_code == 200
    _t("POST cluster/undo (2 000 files)", lambda: client.post("/cluster/undo", data={"folder": str(renamed)}), 30.0)
    undo_batch = journal.batches()[0]
    r = _t("POST history/<undo of 2 000>/revert", lambda: client.post(f"/history/{undo_batch['batch']}/revert"), 30.0)
    assert r.status_code == 303 and f"{undo_batch['n']} files put back" in r.headers["location"].replace("%20", " ")
    assert undo_batch["n"] >= 2000
    assert len(local["photos"]) >= 12

    print("\n  " + "-" * 78)
    worst = sorted(ROWS, key=lambda r: r[1] / r[2], reverse=True)[:5]
    for name, med, bound in worst:
        print(f"  closest to its bound: {name:<52} {med / bound * 100:5.0f} %")
