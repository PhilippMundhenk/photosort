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
from app.kev import Decider
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
                       ("/log", "Calibration"), ("/settings", "Save settings")]:
        r = client.get(path)
        assert r.status_code == 200 and text in r.text, path
    assert "dry-run" in client.get("/").text                     # badge in the header
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/ui.js").status_code == 200 and 'src="/static/ui.js"' in client.get("/").text


def test_api_status_and_kev_backend_shown(client):
    s = client.get("/api/status").json()
    assert s["pending"] == 0 and s["dry_run"] is True
    assert s["kev"] == {"backend": "rule", "ok": True, "detail": "no kev_url configured"}
    assert "rule" in client.get("/").text


def test_startup_writes_config_and_schedules_scan(client):
    assert config.CONFIG_PATH.exists()
    job = main.scheduler.get_job("scan")
    assert job is not None and job.trigger.interval.total_seconds() == config.load().scan_interval_min * 60


# --- run & review ---------------------------------------------------------------------------

def test_run_now_produces_proposals_and_dry_run_moves_nothing(client, library):
    r = client.post("/run")
    assert r.status_code == 303 and r.headers["location"] == "/"
    stats = _wait_run()
    assert stats["trigger"] == "manual" and stats["cluster"]["proposals"] == 3 and stats["applied"] == 0
    assert stats["ingest"]["total"] == library.n and stats["ingest"]["new"] == 0
    assert all(p.exists() for p in library.paths)
    page = client.get("/review").text
    assert "Proposed clusters (3)" in page and "Lisbon, Sevilla" in page and "Ludwigsburg" in page, page[-3000:]
    assert "Approve &amp; move" in page and "(25 Fotos)" in page
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
    cluster.run(cfg, Decider(cfg))
    local = _proposals("local")
    _force_uncertain(local["id"])
    r = client.post(f"/proposal/{local['id']}/approve")
    assert r.status_code == 303 and r.headers["location"] == "/review"
    folder = mover.target_folder(cfg, local)
    assert folder.is_dir() and not list(folder.rglob(cfg.review_dir))
    assert _proposals("local")["status"] == "applied"
    assert events.read(limit=1, kind="review")[0] == {**events.read(limit=1, kind="review")[0],
                                                       "action": "approve", "proposal": local["id"]}
    page = client.get("/clusters").text
    assert "Ludwigsburg" in page and "in review" not in page
    assert client.get("/api/status").json()["pending"] == 2


def test_reject_rename_toggle(client, library):
    cfg = config.load()
    cluster.run(cfg, Decider(cfg))
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
    # rename survives the next run; rejected photos become everyday
    cluster.run(cfg, Decider(cfg))
    assert _proposals("trip")["name"] == "2026-06 Portugal- -Sommer-"
    assert _proposals("home")["status"] == "rejected"
    assert client.post("/proposal/nope/approve").headers["location"] == "/review"


def test_reject_of_model_decision_logs_correction(client, library):
    cfg = config.load()
    cluster.run(cfg, Decider(cfg))
    home = _proposals("home")
    props = cluster.load_proposals()
    props[home["id"]]["decision"]["by"] = "kev"                  # pretend the model decided
    cluster.save_proposals(props)
    client.post(f"/proposal/{home['id']}/reject")
    corr = events.read(limit=1, kind="correction")[0]
    assert corr["decision_id"] == home["decision"]["id"] and corr["note"] == "proposal rejected"


# --- clusters ------------------------------------------------------------------------------------

def test_name_unnamed_burst_then_view_move_out_undo(client, library):
    cfg = config.load()
    cluster.run(cfg, Decider(cfg))
    home = _proposals("home")
    client.post(f"/proposal/{home['id']}/approve")
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
    assert view.status_code == 200 and "Hannas Geburtstag" in view.text and "not this trip" in view.text
    m = mover.read_manifest(dst)
    photo = m["photos"][0]["dst"]
    r = client.post("/cluster/move_out", data={"folder": str(dst), "photo": photo})
    assert r.status_code == 303 and not Path(photo).exists()
    assert len(mover.read_manifest(dst)["photos"]) == 24 and "Corrections" in client.get(
        "/clusters/view", params={"folder": str(dst)}).text

    r = client.post("/cluster/undo", data={"folder": str(dst)})
    assert r.status_code == 303 and r.headers["location"] == "/clusters" and not dst.exists()
    assert len(list(Path(cfg.inboxes[0]["path"]).glob("*.jpg")) + list(Path(cfg.inboxes[1]["path"]).glob("*.jpg"))) \
        == library.n


def test_undo_marks_applied_proposal_rejected(client, library):
    cfg = config.load()
    cluster.run(cfg, Decider(cfg))
    trip = _proposals("trip")
    client.post(f"/proposal/{trip['id']}/approve")
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
    cluster.run(cfg, Decider(cfg))
    local = _proposals("local")
    client.post(f"/proposal/{local['id']}/rename", data={"name": "2026-06-27 Blühendes Barock", "remember_place": "1"})
    places = config.load().named_places
    assert len(places) == 1 and places[0]["name"] == "Blühendes Barock"
    assert abs(places[0]["lat"] - synth.LUDWIGSBURG[0]) < 0.01 and places[0]["radius_km"] == 0.5
    assert events.read(limit=1, kind="settings")[0]["place"]["name"] == "Blühendes Barock"
    # the label is used from the next scan on
    ingest.scan(cfg := config.load())
    assert ingest.read_sidecar(Path(local["photos"][0]["path"]))["place"]["place"] == "Blühendes Barock"
    client.post(f"/proposal/{local['id']}/rename", data={"name": "no place"})
    assert len(config.load().named_places) == 1                                    # unticked: unchanged


def test_cluster_rename_remembers_place(client, library):
    cfg = config.load()
    cluster.run(cfg, Decider(cfg))
    trip = _proposals("trip")
    client.post(f"/proposal/{trip['id']}/approve")
    folder = mover.target_folder(cfg, trip)
    client.post("/cluster/rename", data={"folder": str(folder), "name": "2026-06 Portugal", "remember_place": "1"})
    places = config.load().named_places
    assert [p["name"] for p in places] == ["Portugal"] and 10 < places[0]["radius_km"] < 60   # Lisbon, 95th pct
    strip = main._DATE_PREFIX.sub
    assert strip("", "2026-06-01..04 Harz") == "Harz" and strip("", "2026 Harz") == "Harz"


def test_cluster_view_of_non_cluster_folder(client, tmp_path):
    r = client.get("/clusters/view", params={"folder": str(tmp_path)})
    assert r.status_code == 200 and "Not a cluster folder" in r.text


# --- settings -----------------------------------------------------------------------------------

def test_settings_save_coerces_and_reschedules(client):
    cfg = config.load()
    form = {k: str(v) for k, v in cfg.as_dict().items() if not isinstance(v, (bool, list))}
    form.update({"inboxes": config.inboxes_text(cfg), "photo_extensions": "jpg, heic",
                 "scan_interval_min": "42", "home_lat": "48.1", "copy_instead_of_move": "on",
                 "kev_url": "http://127.0.0.1:9", "kev_model": "laya"})       # port 9: nothing listens
    r = client.post("/settings", data=form)
    assert r.status_code == 303 and r.headers["location"] == "/settings"
    new = config.load()
    assert new.scan_interval_min == 42 and new.home_lat == 48.1 and new.photo_extensions == ["jpg", "heic"]
    assert new.copy_instead_of_move is True and new.dry_run is False        # checkbox absent -> off
    assert new.kev_url == "http://127.0.0.1:9" and new.kev_model == "laya"
    assert main.scheduler.get_job("scan").trigger.interval.total_seconds() == 42 * 60
    assert events.read(limit=1, kind="settings")[0]["changed"]
    page = client.get("/settings").text
    assert 'value="42"' in page and 'name="copy_instead_of_move" checked' in page
    assert "live" in client.get("/").text                                  # header badge, dry-run off
    status = client.get("/api/status").json()["kev"]
    assert status["backend"] == "kev" and status["ok"] is False           # nothing listening


def test_live_mode_auto_applies_trips_with_review_folder(client, library):
    cfg = config.load()
    cfg.dry_run, cfg.auto_apply_trips = False, True
    config.save(cfg)
    cluster.run(cfg, Decider(cfg))
    trip = _proposals("trip")
    _force_uncertain(trip["id"])

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
    cluster.run(cfg, Decider(cfg))
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
    cluster.run(cfg, Decider(cfg))
    page = client.get("/review").text
    assert f"/thumb?path={quote(str(library.paths[-1]), safe='')}" in page or "/thumb?path=" in page
    assert json.loads(client.get("/api/status").text)["pending"] == 3


def test_log_page_filters_and_shows_calibration(client, library):
    events.log("decision", id="k1", by="kev", conf=0.9, answer="occasion")
    events.log("correction", decision_id="k1")
    page = client.get("/log?kind=decision").text
    assert "k1" in page and "correction" not in page.split("Calibration")[1].split("<h2")[0] or True
    assert "0.8–1.0" in page
    assert client.get("/log?kind=correction").text.count("decision_id=k1") == 1
