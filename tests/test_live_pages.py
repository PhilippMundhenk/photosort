"""The web layer under real use: several clicks in one request, a scan finishing while the user
works, the status the pages poll, and the settings that move files without review."""
from __future__ import annotations

from datetime import datetime, timedelta
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


def _kind(kind: str) -> dict:
    return next(p for p in cluster.load_proposals().values() if p["kind"] == kind)


FETCH = {"X-Requested-With": "fetch"}


def test_several_toggles_travel_in_one_request(client, library):
    cluster.run(config.load())
    trip = _kind("trip")
    p0, p1, p2 = (trip["photos"][i]["path"] for i in range(3))
    r = client.post(f"/proposal/{trip['id']}/toggle", data={"path": [p0, p1, p2, p1]}, headers=FETCH)
    assert r.status_code == 200
    d = r.json()
    assert d["excluded_paths"] == sorted([p0, p2])                     # p1 clicked twice: back in
    assert d["excluded"] is False                                       # the last click's photo
    assert _kind("trip")["excluded"] == sorted([p0, p2])
    r = client.post(f"/proposal/{trip['id']}/toggle", data={"path": p0}, headers=FETCH)
    assert r.json()["excluded_paths"] == [p2] and r.json()["excluded"] is False
    r = client.post(f"/proposal/{trip['id']}/toggle", data={"path": p0})   # plain form: redirect as before
    assert r.status_code == 303 and _kind("trip")["excluded"] == sorted([p0, p2])


def test_everyday_photos_stay_unless_auto_move_is_on(client, library):
    cfg = config.load()
    cfg.dry_run, cfg.auto_apply_everyday, cfg.everyday_keep_days = False, False, 0
    config.save(cfg)
    stats = main.run_pipeline("test")
    main.wait_for_apply()
    assert stats["applied"] == 0 and stats["queued"] == 0
    assert all(p.exists() for p in library.paths)                       # every file still in its inbox
    assert main.EVERYDAY_JOB not in list(main._apply_queue.queue)
    assert not (Path(cfg.root) / "2026").exists()
    cfg.auto_apply_everyday = True
    config.save(cfg)
    stats = main.run_pipeline("test")
    assert stats["applied"] == 10 and (Path(cfg.root) / "2026" / "06").is_dir()


def test_status_reports_queue_failures_and_finished_runs(client, library):
    cfg = config.load()
    cfg.dry_run = True
    config.save(cfg)
    runs = client.get("/api/status").json()["state"]["runs"]
    main.run_pipeline("test")
    s = client.get("/api/status").json()
    assert s["state"]["runs"] == runs + 1 and s["pending"] == 3 and s["queue"] == {}
    trip = _kind("trip")
    client.post(f"/proposal/{trip['id']}/approve")
    main.wait_for_apply()
    s = client.get("/api/status").json()
    assert s["queue"] == {trip["id"]: "queued"} and s["approved"] == 1 and s["pending"] == 2   # dry-run: waits
    props = cluster.load_proposals()
    props[trip["id"]]["error"] = "boom"
    cluster.save_proposals(props)
    assert client.get("/api/status").json()["queue"] == {trip["id"]: "failed: boom"}


def test_run_finishing_does_not_forget_review_edits(client, library):
    """The run recomputes the proposals; what the user did on the page meanwhile must survive,
    even when photos that synced late change the proposal's edges (and so its hash)."""
    cfg = config.load()
    cfg.dry_run = True
    config.save(cfg)
    main.run_pipeline("test")
    trip = _kind("trip")
    client.post(f"/proposal/{trip['id']}/toggle", data={"path": trip["photos"][0]["path"]}, headers=FETCH)
    client.post(f"/proposal/{trip['id']}/rename", data={"name": "2026-06 Portugal"}, headers=FETCH)
    end = datetime.fromisoformat(trip["end"])
    library.photo(Path(cfg.inboxes[1]["path"]), end + timedelta(hours=3), synth.LISBON, cam="phone-b")
    main.run_pipeline("test")
    again = cluster.load_proposals()[trip["id"]]
    assert again["excluded"] == [trip["photos"][0]["path"]] and again["name"] == "2026-06 Portugal"
    assert again["n"] == trip["n"] + 1
    assert 'value="2026-06 Portugal"' in client.get("/review").text


def test_review_page_offers_reload_instead_of_reloading(client, library):
    """The page's script reloads nothing by itself; the base template carries the offer."""
    html = client.get("/review").text
    assert 'id="busy-hint"' in html
    js = (Path(main.BASE) / "static" / "ui.js").read_text(encoding="utf-8")
    body = js.split("function offerReload")[1]
    assert "location.reload()" in body                                  # only on the stateless pages or on click
    assert js.count("location.reload()") == 2                           # the offer's click handler, and that one


def test_pages_have_no_form_control_per_photo(client, library):
    """Password-manager extensions watch every input and button on the page and re-scan them on
    every change and scroll; one per photo (hundreds) made Firefox flag the extension. Photos
    are plain elements on every page, however many there are."""
    cfg = config.load()
    cluster.run(cfg)
    props = cluster.load_proposals()
    pending = [p for p in props.values() if p["status"] in ("pending", "ongoing")]
    n_photos = sum(p["n"] for p in pending)
    html = client.get("/review").text
    assert html.count("<figure") == n_photos and n_photos > 50
    assert html.count("<input") <= 3 * len(pending) + 3               # name, remember-place per card; not per photo
    assert html.count("<button") <= 3 * len(pending) + 3               # approve, reject per card; approve all, run
    assert html.count("<form") <= 3 * len(pending) + 3
    everyday = client.get("/everyday").text
    assert everyday.count("<figure") > 5
    assert everyday.count("<input") <= 3 and everyday.count("<button") <= 4 and everyday.count("<form") <= 3
    local = next(p for p in pending if p["kind"] == "local")
    mover.apply(cfg, local, reviewed=True)
    page = client.get("/clusters/view", params={"folder": str(mover.target_folder(cfg, local))}).text
    assert page.count("<figure") == local["n"]
    assert page.count("<form") <= 5 and page.count("<button") <= 4 and page.count("<input") <= 8   # not per photo


def test_retry_after_a_failed_move_moves(client, library, monkeypatch):
    """A move that failed (e.g. the sorted root not writable) leaves the proposal approved with
    an error; the retry button must queue it again, and once the cause is fixed it moves."""
    cfg = config.load()
    cfg.dry_run = False
    config.save(cfg)
    cluster.run(cfg)
    local = _kind("local")
    real = mover.apply
    calls = []

    def failing(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError(13, "Permission denied", str(Path(cfg.root) / local["name"]))
        return real(*a, **k)
    monkeypatch.setattr(mover, "apply", failing)
    client.post(f"/proposal/{local['id']}/approve")
    main.wait_for_apply()
    pr = cluster.load_proposals()[local["id"]]
    assert pr["status"] == "approved" and "Permission denied" in pr["error"]
    assert "failed" in client.get("/review").text and "retry" in client.get("/review").text
    assert client.get("/api/status").json()["queue"] == {local["id"]: "failed: " + pr["error"]}
    client.post(f"/proposal/{local['id']}/approve")                   # the retry button
    main.wait_for_apply()
    assert len(calls) == 2
    assert cluster.load_proposals()[local["id"]]["status"] == "applied"
    assert not any(Path(p["path"]).exists() for p in local["photos"])
    assert [e for e in events.read(limit=50) if e.get("action") == "retry"]
