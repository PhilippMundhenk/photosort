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


def test_pages_warn_when_inbox_and_root_are_on_different_mounts(client, library, monkeypatch):
    cfg = config.load()
    assert mover.cross_mount_note(cfg) is None                       # tmp_path: one filesystem
    assert "Slow moves" not in client.get("/settings").text
    real = Path.stat

    class Dev:
        def __init__(self, st, dev):
            self._st, self.st_dev = st, dev

        def __getattr__(self, k):
            return getattr(self._st, k)

    def stat(self, *a, **k):
        st = real(self, *a, **k)
        return Dev(st, 99) if str(self) == cfg.root else st
    monkeypatch.setattr(Path, "stat", stat)
    mover._mount_cache["at"] = 0.0
    note = mover.cross_mount_note(cfg)
    assert note and "different mounts" in note and "PHOTOS_BASE" in note
    for page in ("/", "/review", "/settings"):
        assert "Slow moves" in client.get(page).text
    assert "Slow moves" not in client.get("/everyday").text


def test_a_failed_move_is_not_retried_by_every_run(client, library, monkeypatch):
    """A proposal whose move failed waits for the retry button; the scheduled run must not
    hammer a read-only share every ten minutes (and log a failure each time)."""
    cfg = config.load()
    cfg.dry_run = False
    config.save(cfg)
    cluster.run(cfg)
    local = _kind("local")
    calls = []

    def failing(*a, **k):
        calls.append(1)
        raise PermissionError(13, "Permission denied", cfg.root)
    monkeypatch.setattr(mover, "apply", failing)
    client.post(f"/proposal/{local['id']}/approve")
    main.wait_for_apply()
    assert len(calls) == 1 and cluster.load_proposals()[local["id"]]["error"]
    stats = main.run_pipeline("schedule")
    main.wait_for_apply()
    assert stats["queued"] == 0 and len(calls) == 1                    # not queued again by the run
    client.post(f"/proposal/{local['id']}/approve")                   # retry: tried once more
    main.wait_for_apply()
    assert len(calls) == 2


def test_concurrent_toggles_from_many_tabs_during_a_run_end_consistent(client, library):
    """Eight 'tabs' toggle different photos of the same proposal while the pipeline runs; every
    click must land exactly once and nothing may error, whatever the interleaving."""
    import threading

    cfg = config.load()
    cfg.dry_run = True
    config.save(cfg)
    main.run_pipeline("test")
    trip = _kind("trip")
    paths = [p["path"] for p in trip["photos"][:40]]
    errors: list = []

    def tab(i: int):
        mine = paths[i::8]
        for path in mine:
            r = client.post(f"/proposal/{trip['id']}/toggle", data={"path": path}, headers=FETCH)
            if r.status_code != 200:
                errors.append((i, r.status_code))
    runner = threading.Thread(target=main.run_pipeline, args=("schedule",))
    tabs = [threading.Thread(target=tab, args=(i,)) for i in range(8)]
    runner.start()
    for t in tabs:
        t.start()
    for t in tabs + [runner]:
        t.join(60)
    main.wait_for_apply()
    assert not errors
    assert sorted(_kind("trip")["excluded"]) == sorted(paths)                # each photo toggled exactly once
    assert client.get("/review").status_code == 200


def test_names_with_html_and_unicode_are_escaped_and_survive(client, library):
    """A name typed by the user is shown escaped (never as markup) and, with umlauts and emoji,
    becomes the real folder name; an absurdly long name is cut to what file systems take."""
    cfg = config.load()
    cfg.dry_run = False
    config.save(cfg)
    cluster.run(cfg)
    local = _kind("local")
    typed = '2026-06-27 <script>alert(1)</script> Tom & "Jerry" Grüße 🙂'
    evil = cluster.sanitize(typed)                                          # < > " cannot be in a folder name
    assert evil == "2026-06-27 -script-alert(1)-script- Tom & -Jerry- Grüße 🙂"
    r = client.post(f"/proposal/{local['id']}/rename", data={"name": typed}, headers=FETCH)
    assert r.status_code == 200 and r.json()["name"] == evil
    html = client.get("/review").text
    assert "<script>" not in html and "Tom &amp; -Jerry- Grüße 🙂" in html   # escaped, umlauts and emoji intact
    client.post(f"/proposal/{local['id']}/approve")
    main.wait_for_apply()
    folder = Path(cfg.root) / evil
    assert folder.is_dir() and mover.read_manifest(folder)["name"] == evil
    page = client.get("/clusters/view", params={"folder": str(folder)}).text
    assert "Tom &amp; -Jerry- Grüße 🙂" in page and "<script>" not in page
    assert "Tom &amp; -Jerry- Grüße 🙂" in client.get("/clusters").text
    long = "2026-06 " + "Ä" * 300
    assert len(cluster.sanitize(long).encode("utf-8")) <= cluster.MAX_NAME_BYTES
    dst = mover.rename(cfg, folder, long)
    assert dst.is_dir() and len(dst.name.encode("utf-8")) <= cluster.MAX_NAME_BYTES


def test_media_supports_range_requests_for_video_seeking(client, library):
    """The viewer's <video> seeks with Range requests; the original must answer them."""
    from PIL import Image
    photo = library.paths[0]
    Image.new("RGB", (64, 48), "blue").save(photo, "JPEG")
    full = client.get("/media", params={"path": str(photo)})
    assert full.status_code == 200 and full.headers["content-type"] == "image/jpeg"
    part = client.get("/media", params={"path": str(photo)}, headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and len(part.content) == 10
    assert part.headers["content-range"].startswith("bytes 0-9/") and part.headers.get("accept-ranges") == "bytes"
    assert part.content == full.content[:10]


def test_same_file_names_from_two_phones_end_up_apart_and_come_back_right(client, library):
    """Both phones name their files IMG_0001.jpg. In one cluster folder they must not overwrite
    each other, and undo must return each to its own inbox, with per-source subfolders on or off."""
    cfg = config.load()
    cfg.dry_run, cfg.sidecar_cleanup = False, "never"       # fake JPEGs: their records must survive the round trip
    config.save(cfg)
    lib = synth.Library(cfg)
    a, b = Path(cfg.inboxes[0]["path"]), Path(cfg.inboxes[1]["path"])
    day = synth.T0 + timedelta(days=40)
    twins = []
    for i in range(12):                                                    # a day out, both phones, same names
        for inbox in (a, b):
            p = inbox / f"DSC_{i:03d}.jpg"
            lib.n += 1
            p.write_bytes(b"\xff\xd8" + inbox.name.encode() + b"\xff\xd9")
            from app import ingest
            tags = {"DateTimeOriginal": (day + timedelta(minutes=10 * i)).strftime("%Y:%m:%d %H:%M:%S"),
                    "GPSLatitude": synth.LUDWIGSBURG[0], "GPSLongitude": synth.LUDWIGSBURG[1]}
            rec = ingest.build_record(cfg, p, tags, source=inbox.name)
            rec.update(source=inbox.name, inbox=str(inbox))
            ingest.write_sidecar(p, rec, cfg)
            twins.append(p)
    for layout in (True, False):
        cfg.subfolder_by_source = layout
        config.save(cfg)
        main.run_pipeline("test")
        props = cluster.load_proposals()
        found = [(p["kind"], p["n"], p["status"], p["name"]) for p in props.values()]
        dsc = [(r["file"], r["zone"], r.get("source"), r["ts"])
               for r in cluster.records_filled(cfg) if "DSC_" in r["file"]]
        local = next((p for p in props.values() if p["kind"] == "local" and p["n"] == 24), None)
        assert local, (found, dsc[:4])
        client.post(f"/proposal/{local['id']}/approve")
        main.wait_for_apply()
        folder = mover.target_folder(cfg, local)
        files = sorted(f.name for f in folder.rglob("*.jpg"))
        contents = [f.read_bytes() for f in folder.rglob("*.jpg")]
        assert len(files) == 24 and contents.count(b"\xff\xd8phone-a\xff\xd9") == 12          # nothing overwritten
        assert contents.count(b"\xff\xd8phone-b\xff\xd9") == 12
        if not layout:
            assert sum(1 for n in files if n.endswith("_1.jpg")) == 12                             # clashes renamed
        client.post("/cluster/undo", data={"folder": str(folder)})
        assert all(p.exists() for p in twins)
        assert all(p.read_bytes() == b"\xff\xd8" + p.parent.name.encode() + b"\xff\xd9" for p in twins)   # each home
        props = cluster.load_proposals()
        props[local["id"]]["status"] = "pending"
        cluster.save_proposals(props)


def test_a_file_deleted_by_hand_leaves_proposals_and_moves_intact(client, library):
    """The user deletes a photo in the inbox with another tool: the next run drops it from its
    proposal, and approving a proposal that still lists a vanished photo just skips it."""
    cfg = config.load()
    cfg.dry_run = False
    config.save(cfg)
    main.run_pipeline("test")
    trip = _kind("trip")
    gone = Path(trip["photos"][5]["path"])
    gone.unlink()
    main.run_pipeline("test")
    again = _kind("trip")
    assert again["n"] == trip["n"] - 1 and str(gone) not in {p["path"] for p in again["photos"]}
    assert client.get("/everyday").status_code == 200 and client.get("/review").status_code == 200
    local = _kind("local")
    Path(local["photos"][0]["path"]).unlink()                              # vanishes after the proposal was made
    client.post(f"/proposal/{local['id']}/approve")
    main.wait_for_apply()
    pr = cluster.load_proposals()[local["id"]]
    assert pr["status"] == "applied" and pr.get("error") is None
    assert len(mover.read_manifest(mover.target_folder(cfg, local))["photos"]) == local["n"] - 1


def test_photos_taken_in_the_same_second_keep_a_stable_order(client, library):
    cfg = config.load()
    lib = synth.Library(cfg)
    lib.n = 5000                                                           # names apart from the fixture's files
    a = Path(cfg.inboxes[0]["path"])
    when = synth.T0 + timedelta(days=45, hours=12)
    burst = [lib.photo(a, when, synth.HOME) for _ in range(20)]           # 20 frames, one second
    main.run_pipeline("test")
    home = next(p for p in cluster.load_proposals().values() if p["kind"] == "home" and p["n"] == 20)
    assert [p["path"] for p in home["photos"]] == sorted(str(p) for p in burst)   # by name within the second
    main.run_pipeline("test")
    assert home["id"] in cluster.load_proposals()                            # and the id does not flip
    html = client.get("/review").text
    assert html.count(f'data-path="{a}') >= 20
