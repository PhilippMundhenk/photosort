"""Every conditional branch of the templates renders what it says: banner states, the
dashboard's warnings, the review page's sections for approved, moving, failed, ongoing,
manual and uncertain proposals in dry-run and live mode, the everyday page's move box in
each state, the cluster and clusters pages, the settings form with each option, the log."""
from __future__ import annotations

import time
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cluster, config, events, ingest, main, mover
from tests import synth


@pytest.fixture
def client(library):
    main._state.update({"last_run": None, "last_stats": {}, "running": False, "error": None, "progress": None,
                        "runs": 0})
    with TestClient(main.app, follow_redirects=False) as c:
        yield c
    main._state.update({"running": False, "progress": None, "error": None})
    main._applying.clear()


def _props() -> dict:
    return {p["kind"]: p for p in cluster.load_proposals().values()}


def _set(pid: str, **fields) -> None:
    props = cluster.load_proposals()
    props[pid].update(fields)
    cluster.save_proposals(props)


# --- base ------------------------------------------------------------------------------------

def test_header_and_banner_follow_the_run_state(client):
    html = client.get("/").text
    assert "running…" not in html and 'id="busy" class="busy" data-page="dashboard" data-runs="0" hidden' in html
    main._state.update(running=True, runs=3)
    html = client.get("/").text
    assert "running…" in html and 'data-runs="3"' in html and "Working…" in html
    assert 'data-page="dashboard" data-runs="3" >' in html or 'data-runs="3" >' in html   # not hidden
    cfg = config.load()
    cfg.dry_run = True
    config.save(cfg)
    assert 'title="nothing is moved, copied or deleted">dry-run' in client.get("/").text
    cfg.dry_run = False
    config.save(cfg)
    assert 'title="approved proposals are moved">LIVE' in client.get("/").text


# --- dashboard ----------------------------------------------------------------------------

def test_dashboard_warnings_and_empty_states(client, library):
    cfg = config.load()
    html = client.get("/").text
    assert "Nothing yet — press" in html and "Home location is not set" not in html
    assert "phone-a, phone-b" in html
    cfg.home_lat = cfg.home_lon = 0.0
    cfg.inboxes = [{"path": str(Path(cfg.root).parent / "absent"), "name": "absent"}]
    config.save(cfg)
    main._state.update(error="disk on fire", last_stats={"ingest": {"missing": ["/photos/absent"], "new": 0},
                                                        "cluster": {"photos": 0, "everyday": 0}, "applied": 0})
    events.log("run", trigger="test")
    html = client.get("/").text
    assert "Home location is not set" in html and "Last run failed: disk on fire" in html
    assert "Inbox folders not found: /photos/absent" in html and "0 new last run" in html
    assert "<code>trigger=test</code>" in html and "Nothing yet" not in html
    cfg.inbox_root = str(Path(cfg.root).parent / "nowhere")
    cfg.inboxes = []
    config.save(cfg)
    assert "(none found)" in client.get("/").text


# --- review -----------------------------------------------------------------------------------

def test_review_dry_run_notes_count_waiting_approvals(client, library):
    cfg = config.load()
    cfg.dry_run = True
    config.save(cfg)
    cluster.run(cfg)
    html = client.get("/review").text
    assert "Dry-run is on" in html and "approved proposals are moved once you switch dry-run off" in html
    assert "Approve all (3)" in html and "Approve &amp; move" not in html
    p = _props()
    _set(p["trip"]["id"], status="approved")
    html = client.get("/review").text
    assert "1 approved proposal waits for dry-run" in html and "not moved (dry-run)" in html
    assert 'data-poll="approved"' not in html and "Approved, waiting for dry-run to be switched off (1)" in html
    _set(p["local"]["id"], status="approved")
    assert "2 approved proposals wait for dry-run" in client.get("/review").text


def test_review_live_moving_queued_failed_and_empty(client, library):
    cfg = config.load()
    cfg.dry_run = False
    config.save(cfg)
    cluster.run(cfg)
    p = _props()
    _set(p["trip"]["id"], status="approved")
    _set(p["local"]["id"], status="approved", error="Permission denied: /photos/sorted")
    main._applying[p["trip"]["id"]] = {"done": 3, "total": 67, "name": p["trip"]["name"],
                                       "current": {"file": "IMG_0001.jpg", "bytes": 5_000_000, "since": time.time()}}
    html = client.get("/review").text
    assert "Moving (2)" in html and 'data-poll="approved"' in html
    assert ">moving</span>" in html and "3 / 67" in html
    assert ">failed</span>" in html and "Permission denied" in html and ">retry</button>" in html
    assert "Approve &amp; move all (1)" in html
    main._applying.clear()
    html = client.get("/review").text
    assert ">queued</span>" in html and f'data-progress="{p["trip"]["id"]}">0 / {p["trip"]["n"]}' in html
    for pid in (p["trip"]["id"], p["local"]["id"], p["home"]["id"]):
        _set(pid, status="rejected")
    html = client.get("/review").text
    assert "No open proposals." in html and "Nothing waiting." in html and "Approve" not in html.split("<h2")[1]


def test_review_cards_show_manual_ongoing_uncertain_and_video(client, library):
    cfg = config.load()
    cluster.run(cfg)
    p = _props()
    _set(p["trip"]["id"], status="ongoing")
    props = cluster.load_proposals()
    local = props[p["local"]["id"]]
    local["manual"] = True
    local["photos"][0].update(uncertain=True, conf=0.6, media="video")
    local["n_uncertain"] = 1
    local["decision"]["answer"] = "yes"
    cluster.save_proposals(props)
    html = client.get("/review").text
    trip_card = html.split(f'id="{p["trip"]["id"]}"')[1].split('<div class="card"')[0]
    assert "ongoing – waiting for a home photo" in trip_card and "/approve" not in trip_card
    assert "Reject (keep as everyday)" in trip_card
    local_card = html.split(f'id="{p["local"]["id"]}"')[1].split('<div class="card"')[0]
    assert "by hand" in local_card and ", 1 uncertain" in local_card and "(yes)" in local_card
    assert '<span class="badge">video</span>' in local_card and "<details open>" in local_card
    assert 'name="remember_place"' in local_card
    home_card = html.split(f'id="{p["home"]["id"]}"')[1].split('<div class="card"')[0]
    assert 'name="remember_place"' not in home_card and "<details open>" in home_card
    assert "Approve all" not in html                                       # only the ongoing one... no: one ready
    assert "Approve &amp; move all (2)" in html


def test_review_open_parameter_and_home_warning(client, library):
    cfg = config.load()
    cluster.run(cfg)
    p = _props()
    html = client.get("/review", params={"open": p["local"]["id"]}).text
    local_card = html.split(f'id="{p["local"]["id"]}"')[1].split('<div class="card"')[0]
    assert "<details open>" in local_card
    trip_card = html.split(f'id="{p["trip"]["id"]}"')[1].split('<div class="card"')[0]
    assert "<details >" in trip_card or "<details>" in trip_card
    cfg.home_lat = cfg.home_lon = 0.0
    config.save(cfg)
    assert "Home location is not set" in client.get("/review").text


def test_review_lists_unnamed_bursts_with_thumbnails(client, library):
    cfg = config.load()
    cluster.run(cfg)
    home = _props()["home"]
    mover.apply(cfg, home, reviewed=True)
    html = client.get("/review").text
    assert "Bursts waiting for a name (1)" in html and f'value="{home["name"][:10]} "' in html
    assert html.count('<span class="view" role="button"') >= 8


# --- everyday -----------------------------------------------------------------------------

def test_everyday_move_box_in_every_state(client, library):
    cfg = config.load()
    cfg.dry_run = True
    config.save(cfg)
    cluster.run(cfg)
    html = client.get("/everyday").text
    assert "dry-run is on, nothing moves" in html and "Move " not in html.split("<form")[0]
    cfg.dry_run = False
    config.save(cfg)
    html = client.get("/everyday").text
    assert "everyday photos into YYYY/MM/</button>" in html and "older than 4 days" in html
    cfg.everyday_keep_days = 100_000
    config.save(cfg)
    assert "Nothing to move: every everyday photo is newer than 100000 days" in client.get("/everyday").text
    main._lock.acquire()                                                    # the worker blocks on the first job,
    main.queue_apply(main.EVERYDAY_JOB)                                     # the second one waits in the queue
    main.queue_apply(main.EVERYDAY_JOB)
    t0 = time.time()
    while list(main._apply_queue.queue) != [main.EVERYDAY_JOB] and time.time() - t0 < 5:
        time.sleep(0.05)
    assert "moving everyday photos starts after the current move" in client.get("/everyday").text
    main._lock.release()
    main.wait_for_apply()                                                   # nothing old enough: nothing moved
    assert all(p.exists() for p in library.paths)
    main._applying[main.EVERYDAY_JOB] = {"done": 2, "total": 9, "name": "everyday photos", "current": None}
    html = client.get("/everyday").text
    assert '<span class="badge warn">moving</span>' in html and "2 / 9" in html
    main._applying.clear()


def test_everyday_captions_and_empty_library(client, library):
    cfg = config.load()
    lib = synth.Library(cfg)
    lib.video(Path(cfg.inboxes[0]["path"]), synth.T0 + timedelta(days=1, hours=12), synth.HOME)
    cluster.run(cfg)
    html = client.get("/everyday?month=2026-06").text
    assert '<span class="badge">video</span>' in html and "· Bietigheim-Bissingen" in html
    assert "home · Bietigheim-Bissingen" in html                            # zones and places per day
    for p in library.paths + lib.paths:
        p.unlink()
    ingest.changed["all"] = True
    html = client.get("/everyday").text
    assert "Nothing indexed yet" in html and "No everyday photos." in html


# --- clusters and cluster -----------------------------------------------------------------

def test_clusters_list_flags_unnamed_and_in_review(client, library):
    cfg = config.load()
    cluster.run(cfg)
    p = _props()
    props = cluster.load_proposals()
    props[p["local"]["id"]]["photos"][0].update(uncertain=True, conf=0.6, media="video")
    cluster.save_proposals(props)
    mover.apply(cfg, props[p["local"]["id"]], reviewed=False)                # unreviewed: _review used
    mover.apply(cfg, p["home"], reviewed=True)                                  # unnamed burst
    html = client.get("/clusters").text
    assert '<span class="badge warn">unnamed</span>' in html and "(1 in review)" in html
    folder = mover.target_folder(cfg, props[p["local"]["id"]])
    html = client.get("/clusters/view", params={"folder": str(folder)}).text
    assert '<span class="badge">video</span>' in html and "· phone-a" in html and 'name="remember_place"' in html
    home_folder = mover.target_folder(cfg, p["home"])
    html = client.get("/clusters/view", params={"folder": str(home_folder)}).text
    assert 'name="remember_place"' not in html and "Removed from this cluster" not in html
    mover.move_out(cfg, home_folder, Path(mover.read_manifest(home_folder)["photos"][0]["dst"]))
    assert "Removed from this cluster" in client.get("/clusters/view", params={"folder": str(home_folder)}).text


# --- settings and log -------------------------------------------------------------------------

def test_settings_reflects_every_option(client, library):
    cfg = config.load()
    cfg.inbox_root = str(Path(cfg.root).parent)                                  # phone-a/, phone-b/ are found
    config.save(cfg)
    html = client.get("/settings").text
    assert 'id="sbs" name="subfolder_by_source" checked' in html                # synth config
    assert 'id="dry" name="dry_run"  data-dry-run' in html                       # live in tests
    assert 'value="central" selected' in html and 'value="after_move" selected' in html
    assert "<code>phone-a=" in html and "Found now:" in html
    cfg.dry_run = cfg.copy_instead_of_move = cfg.auto_apply_trips = cfg.auto_apply_local = True
    cfg.auto_apply_home = cfg.auto_apply_everyday = cfg.write_xmp_sidecar = True
    cfg.subfolder_by_source = cfg.generate_thumbnails = cfg.name_multiday_by_month = cfg.warn_cross_mount = False
    cfg.sidecar_mode, cfg.sidecar_cleanup, cfg.everyday_layout = "beside", "never", "leave"
    cfg.inbox_root = str(Path(cfg.root).parent / "nowhere")
    cfg.inboxes = []
    config.save(cfg)
    html = client.get("/settings").text
    for box in ("dry", "copy", "at", "al", "ah", "ae", "xmp"):
        assert f'id="{box}"' in html and html.split(f'id="{box}"')[1].split(">")[0].count("checked") == 1
    for box in ("sbs", "gt", "nmm", "wcm"):
        assert html.split(f'id="{box}"')[1].split(">")[0].count("checked") == 0
    assert 'value="beside" selected' in html and 'value="never" selected' in html
    assert "layout is <code>leave</code>, so nothing is moved either way" in html
    assert "nothing (no subfolders or files in" in html
    assert "Sidecars: 0 moved" in client.get("/settings", params={"msg": "Sidecars: 0 moved"}).text


def test_log_filters_and_empty_log(client, library):
    assert "<td class=\"muted\">empty</td>" in client.get("/log").text
    cluster.run(config.load())
    events.log("review", action="approve", name="x")
    html = client.get("/log", params={"kind": "review"}).text
    assert 'class="badge ok" href="/log?kind=review"' in html and "<code>action=approve</code>" in html
    assert html.count('<span class="badge">review</span>') >= 1 and '<span class="badge">run</span>' not in html
    assert 'class="badge ok" href="/log?kind="' in client.get("/log").text


# --- accessibility basics on every page ------------------------------------------------------------

class _A11y(HTMLParser):
    """Collects what an accessibility check needs: images without alt, controls without a name,
    duplicate ids, the document language, headings."""

    def __init__(self):
        super().__init__()
        self.img_no_alt, self.unnamed, self.ids, self.dups, self.h1 = [], [], set(), [], 0
        self.lang = None
        self._open_named = []                      # (tag, attrs, text) for elements that need visible text
        self._labels = 0                           # inside a <label>: a wrapped control is named by it

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang")
        if tag == "h1":
            self.h1 += 1
        if "id" in a:
            if a["id"] in self.ids:
                self.dups.append(a["id"])
            self.ids.add(a["id"])
        if tag == "img" and "alt" not in a:
            self.img_no_alt.append(a.get("src", "?"))
        if tag == "label":
            self._labels += 1
        if tag in ("input", "select", "textarea") and a.get("type") not in ("hidden", "submit"):
            if not (a.get("title") or a.get("aria-label") or a.get("placeholder") or a.get("id") or self._labels):
                self.unnamed.append(f"{tag} {a}")
        if tag == "button" or a.get("role") in ("button", "checkbox", "link"):
            if a.get("title") or a.get("aria-label"):
                return
            self._open_named.append([tag, a, ""])

    def handle_data(self, data):
        for item in self._open_named:
            item[2] += data

    def handle_endtag(self, tag):
        if tag == "label":
            self._labels -= 1
        if self._open_named and self._open_named[-1][0] == tag:
            t, a, text = self._open_named.pop()
            if not text.strip():
                self.unnamed.append(f"{t} {a}")


def _check(html: str, page: str) -> None:
    p = _A11y()
    p.feed(html)
    assert p.lang == "en", page
    assert p.h1 == 1, f"{page}: {p.h1} h1 elements"
    assert not p.img_no_alt, f"{page}: images without alt: {p.img_no_alt[:3]}"
    assert not p.unnamed, f"{page}: controls without a name: {p.unnamed[:3]}"
    assert not p.dups, f"{page}: duplicate ids: {p.dups[:3]}"


def test_every_page_passes_the_accessibility_basics(client, library):
    """One h1, a document language, alt on every image, a name on every control (visible text,
    title or aria-label), no duplicate ids: on every page, in every state the suite can produce."""
    cfg = config.load()
    cfg.dry_run = False
    config.save(cfg)
    cluster.run(cfg)
    p = _props()
    _set(p["trip"]["id"], status="approved", error="Permission denied")
    mover.apply(cfg, p["local"], reviewed=True)
    folder = mover.target_folder(cfg, p["local"])
    mover.move_out(cfg, folder, Path(mover.read_manifest(folder)["photos"][0]["dst"]))
    mover.apply(cfg, p["home"], reviewed=True)
    for path in ("/", "/review", f"/review?open={p['home']['id']}", "/everyday", "/everyday?month=2026-06",
                 "/clusters", f"/clusters/view?folder={folder}", "/log", "/log?kind=review",
                 "/settings", "/settings?msg=Saved"):
        r = client.get(path)
        assert r.status_code == 200, path
        _check(r.text, path)
    cfg.dry_run = True
    config.save(cfg)
    for path in ("/", "/review", "/everyday", "/settings"):
        _check(client.get(path).text, path + " (dry-run)")
