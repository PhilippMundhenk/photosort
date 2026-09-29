"""Whole-pipeline scenarios in the browser that the synthetic library alone does not show:
copy mode from the settings page to undo, and two phones on different continents at the same
time (scan, clustering, naming, review) with the partner's photos at home in between."""
from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import yaml

from tests import synth
from tests.e2e.test_browser import _proposals, _wait_pending, browser, page, serve  # noqa: F401

pw = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.e2e

SINGAPORE = (1.29, 103.85)


def _library(tmp_path_factory, name: str, **cfg_overrides):
    base = tmp_path_factory.mktemp(name)
    data = base / "data"
    data.mkdir()
    for sub in ("phone-a", "phone-b", "sorted"):
        (base / sub).mkdir()
    cfg = synth.make_config(base, sidecar_mode="beside", **cfg_overrides)
    (data / "config.yaml").write_text(yaml.safe_dump(cfg.as_dict(), sort_keys=False), encoding="utf-8")
    return base, synth.Library(cfg)


@pytest.fixture
def copy_server(tmp_path_factory):
    base, lib = _library(tmp_path_factory, "e2e-copy", copy_instead_of_move=True)
    synth.populate(lib.cfg)
    with serve(base, lib) as s:
        yield s


def test_copy_mode_end_to_end(copy_server, page):
    """Approve in copy mode: the copies appear in the sorted tree, the originals stay in the
    inbox and are never proposed again; undo deletes the copies and frees the originals."""
    url, data, cfg = copy_server["url"], copy_server["data"], copy_server["cfg"]
    expect = pw.expect
    httpx.post(url + "/run", timeout=5)
    _wait_pending(url, 3)
    page.goto(url + "/settings")
    expect(page.locator("#copy")).to_be_checked()
    page.goto(url + "/review")
    local = _proposals(data)["local"]
    originals = [Path(p["path"]) for p in local["photos"]]
    page.locator(f"#{local['id']}").get_by_role("button", name="Approve & move").click()
    expect(page.locator(f"#{local['id']} .badge.ok")).to_have_text("moved", timeout=60_000)
    folder = Path(cfg.root) / local["name"]
    assert all(p.exists() for p in originals)                                 # originals stay
    copies = [f for f in folder.rglob("*.jpg")]
    assert len(copies) == len(originals)
    assert (folder / "manifest.json").exists()
    manifest = yaml.safe_load((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "copy"
    httpx.post(url + "/run", timeout=5)                                        # copied originals: not proposed again
    _wait_pending(url, 2)
    page.goto(url + "/everyday?month=2026-06")
    assert page.locator(".thumbs figure").count() < 12                         # nor shown as everyday
    page.goto(url + "/clusters/view?folder=" + str(folder))
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("button", name="Undo whole cluster").click()
    page.wait_for_url(re.compile(r"/clusters$"))
    assert not folder.exists() and all(p.exists() for p in originals)          # copies gone, originals kept
    httpx.post(url + "/run", timeout=5)
    _wait_pending(url, 2)                                                      # undone = rejected: everyday now
    page.goto(url + "/everyday?month=2026-06")
    assert page.locator(".thumbs figure").count() >= 12 + 10                   # the day out's photos are back


@pytest.fixture
def two_phones_server(tmp_path_factory):
    """Phone A in Lisbon for a week while phone B stays home and photographs daily, then phone B
    in Singapore for a week while phone A is home; a shared day out in between."""
    base, lib = _library(tmp_path_factory, "e2e-phones")
    a, b = Path(lib.cfg.inboxes[0]["path"]), Path(lib.cfg.inboxes[1]["path"])
    t = synth.T0
    for d in range(40):                                                        # daily life at home, by whoever is home
        if not 3 <= d < 10:
            lib.photo(a, t + timedelta(days=d, hours=7), synth.HOME)
        if not 20 <= d < 27:
            lib.photo(b, t + timedelta(days=d, hours=19), synth.HOME)
    for d in range(3, 10):                                                     # A abroad, B at home
        for h in (10, 14, 17):
            lib.photo(a, t + timedelta(days=d, hours=h), synth.LISBON)
    for d in range(20, 27):                                                    # B abroad, A at home
        for h in (9, 13, 18):
            lib.photo(b, t + timedelta(days=d, hours=h), SINGAPORE, cam="phone-b")
    for i in range(12):                                                        # a shared day out
        lib.photo(a if i % 2 else b, t + timedelta(days=33, hours=11, minutes=15 * i), synth.LUDWIGSBURG)
    with serve(base, lib) as s:
        yield s


def test_two_phones_far_apart_are_two_trips_end_to_end(two_phones_server, page):
    url, data = two_phones_server["url"], two_phones_server["data"]
    expect = pw.expect
    httpx.post(url + "/run", timeout=5)
    _wait_pending(url, 3)
    props = list(yaml.safe_load((data / "proposals.json").read_text(encoding="utf-8")).values())
    trips = sorted((p for p in props if p["kind"] == "trip"), key=lambda p: p["start"])
    assert [p["name"] for p in trips] == ["2026-06 Lisbon", "2026-06 Singapore"]
    assert {ph["source"] for ph in trips[0]["photos"]} == {"phone-a"}
    assert {ph["source"] for ph in trips[1]["photos"]} == {"phone-b"}
    assert trips[0]["n"] == 21 and trips[1]["n"] == 21                          # none of the home photos
    local = next(p for p in props if p["kind"] == "local")
    assert local["n"] == 12 and {ph["source"] for ph in local["photos"]} == {"phone-a", "phone-b"}
    page.goto(url + "/review")
    expect(page.get_by_role("heading", name="Proposed clusters (3)")).to_be_visible()
    expect(page.locator(f"#{trips[0]['id']} input[name=name]")).to_have_value("2026-06 Lisbon")
    expect(page.locator(f"#{trips[1]['id']} input[name=name]")).to_have_value("2026-06 Singapore")
    card = page.locator(f"#{trips[1]['id']}")
    card.locator("details summary").click()
    expect(card.locator("figcaption", has_text="phone-b")).to_have_count(21)
    page.goto(url + "/everyday?month=2026-06")
    assert page.locator(".thumbs figure").count() >= 40                        # the daily home photos of June


@pytest.fixture
def berlin_server(tmp_path_factory):
    """Home zone Europe/Berlin: photos carry naive EXIF wall times, videos carry UTC."""
    base, lib = _library(tmp_path_factory, "e2e-berlin", timezone="Europe/Berlin")
    a = Path(lib.cfg.inboxes[0]["path"])
    from datetime import datetime
    from datetime import timezone as tz
    for d in range(6):                                                         # daily life, naive 14:22 wall time
        lib.photo(a, datetime(2026, 6, 1 + d, 14, 22, 31), synth.HOME)
    lib.video(a, datetime(2026, 6, 3, 12, 22, 31, tzinfo=tz.utc), synth.HOME)    # 12:22Z = 14:22 in Berlin
    from zoneinfo import ZoneInfo
    lib.video(a, datetime(2026, 6, 4, 14, 22, 31, tzinfo=ZoneInfo("Europe/Berlin")), synth.HOME, kind="iphone")
    with serve(base, lib) as s:
        yield s


def test_wall_times_in_the_home_zone_end_to_end(berlin_server, page):
    """The everyday page shows the time the photo was taken by the clock on the wall: naive EXIF
    times as they are, UTC video times converted to the home zone."""
    url = berlin_server["url"]
    expect = pw.expect
    httpx.post(url + "/run", timeout=5)
    _wait_pending(url, 0)
    page.goto(url + "/everyday?month=2026-06")
    captions = page.locator(".thumbs figcaption").all_inner_texts()
    assert len(captions) == 8 and all("14:22" in c for c in captions), captions      # never 12:22
    expect(page.locator(".thumbs figure", has=page.locator(".badge", has_text="video"))).to_have_count(2)
    page.goto(url + "/settings")
    expect(page.locator("input[name=timezone]")).to_have_value("Europe/Berlin")


def test_a_named_place_from_settings_names_the_day_out(tmp_path_factory, page):
    """Your own place name entered in Settings beats the geocoder in the folder name."""
    base, lib = _library(tmp_path_factory, "e2e-place")
    synth.populate(lib.cfg)
    with serve(base, lib) as s:
        url, data = s["url"], s["data"]
        expect = pw.expect
        page.goto(url + "/settings?mode=advanced")                     # place names are an advanced setting
        page.locator("textarea[name=named_places]").fill(
            f"Barockstadt = {synth.LUDWIGSBURG[0]}, {synth.LUDWIGSBURG[1]}, 3\nBlack Forest = 48.0, 8.2, 40")
        page.get_by_role("button", name="Save settings").click()
        page.wait_for_url(re.compile(r"/settings"))
        expect(page.locator("textarea[name=named_places]")).to_have_value(re.compile(r"Barockstadt = 48\.897"))
        httpx.post(url + "/run", timeout=5)
        _wait_pending(url, 3)
        local = _proposals(data)["local"]
        assert local["name"] == "2026-06-27 Barockstadt"                         # not "Ludwigsburg"
        page.goto(url + "/review")
        expect(page.locator(f"#{local['id']} input[name=name]")).to_have_value("2026-06-27 Barockstadt")
        page.goto(url + "/everyday?month=2026-06")
        expect(page.get_by_text("Bietigheim-Bissingen").first).to_be_visible()   # unnamed spots keep the geocoder


def test_config_edited_on_disk_applies_without_a_restart(tmp_path_factory, page):
    """config.yaml is edited by hand while the service runs (a NAS user with a text editor): the
    next request sees it, pages and the run use it, and nothing needs restarting."""
    base, lib = _library(tmp_path_factory, "e2e-hotconfig")
    synth.populate(lib.cfg)
    with serve(base, lib) as s:
        url, data = s["url"], s["data"]
        expect = pw.expect
        page.goto(url + "/")
        expect(page.locator("header .badge", has_text="LIVE")).to_be_visible()
        cfg_file = data / "config.yaml"
        raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        raw["dry_run"], raw["scan_interval_min"], raw["dayout_min_photos"] = True, 42, 100
        cfg_file.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        page.reload()
        expect(page.locator("header .badge", has_text="dry-run")).to_be_visible()       # seen at once
        page.goto(url + "/settings")
        expect(page.locator("input[name=scan_interval_min]")).to_have_value("42")
        httpx.post(url + "/run", timeout=5)
        _wait_pending(url, 2)                                                            # a day out needs 100 now
        assert {p["kind"] for p in _proposals(data).values()} == {"trip", "home"}
        cfg_file.write_text("dry_run: [broken", encoding="utf-8")                        # a slip of the editor
        assert httpx.get(url + "/api/status", timeout=10).status_code == 200            # still up, on defaults
        assert httpx.get(url + "/settings", timeout=10).status_code == 200
        raw["dry_run"] = False
        cfg_file.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        page.goto(url + "/")
        expect(page.locator("header .badge", has_text="LIVE")).to_be_visible()
