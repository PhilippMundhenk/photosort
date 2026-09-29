"""Browser tests for the complete feature set, one scenario per test, against one server with
the synthetic library plus a May photo and a video: everyday selection and manual clusters,
keyboard, remembered places, approve all, the everyday move button, settings (dry-run dialog,
home detection, sidecars), log filters, month navigation, the video viewer, the dashboard."""
from __future__ import annotations

import json
import re
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import yaml

from tests import synth
from tests.e2e.test_browser import (  # noqa: F401  (fixtures are found by name)
    _proposals,
    _status,
    _wait_pending,
    browser,
    make_library,
    page,
    serve,
)

pw = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    base, lib = make_library(tmp_path_factory, "e2e-features")
    inbox = Path(lib.cfg.inboxes[0]["path"])
    lib.photo(inbox, synth.T0 - timedelta(days=20), synth.HOME)                 # a second month: May
    lib.video(inbox, synth.T0 + timedelta(days=1, hours=12), synth.HOME)      # a video at home
    with serve(base, lib) as s:
        yield s


def _run(url: str, pending: int) -> None:
    httpx.post(url + "/run", timeout=5)
    _wait_pending(url, pending)


def _wait(cond, timeout: float = 30) -> None:
    t0 = time.time()
    while not cond():
        assert time.time() - t0 < timeout, "condition never became true"
        time.sleep(0.2)


def _card(page, pid: str):
    return page.locator(f"#{pid}")


def test_everyday_selection_creates_and_extends_a_cluster(server, page):
    url, data = server["url"], server["data"]
    expect = pw.expect
    _run(url, 3)
    page.goto(url + "/everyday?month=2026-06")
    figs = page.locator(".thumbs figure")
    n = figs.count()
    assert n >= 10
    figs.nth(0).locator(".pick").click()
    figs.nth(1).locator(".pick").click()
    expect(page.locator("#selcount")).to_have_text("2")
    figs.nth(1).locator(".pick").click()                                    # unselect again
    expect(page.locator("#selcount")).to_have_text("1")
    figs.nth(0).locator(".pick").click()
    expect(page.locator("#selcount")).to_have_text("0")
    page.locator("[data-select-all]").first.click()                        # the whole first day
    day_n = page.locator(".card[data-day]").first.locator("figure").count()
    expect(page.locator("#selcount")).to_have_text(str(day_n))
    page.locator("select[name=kind]").select_option("home")
    page.locator("#assign input[name=name]").fill("2026-06 Grillabend")
    page.get_by_role("button", name="Assign").click()
    page.wait_for_url(re.compile(r"/review\?open="))
    props = json.loads((data / "proposals.json").read_text(encoding="utf-8"))
    manual = next(p for p in props.values() if p.get("manual"))
    assert manual["name"] == "2026-06 Grillabend" and manual["n"] == day_n and manual["kind"] == "home"
    card = _card(page, manual["id"])
    expect(card.get_by_text("by hand")).to_be_visible()
    expect(card.locator("figure")).to_have_count(day_n)

    page.goto(url + "/everyday?month=2026-06")                              # add one more to it
    page.locator(".thumbs figure").first.locator(".pick").click()
    page.locator("select[name=target]").select_option(manual["id"])
    page.get_by_role("button", name="Assign").click()
    page.wait_for_url(re.compile(r"/review\?open="))
    props = json.loads((data / "proposals.json").read_text(encoding="utf-8"))
    assert props[manual["id"]]["n"] == day_n + 1
    expect(_card(page, manual["id"]).locator("figure")).to_have_count(day_n + 1)
    _run(url, 4)                                                            # a run keeps the manual cluster
    assert json.loads((data / "proposals.json").read_text(encoding="utf-8"))[manual["id"]]["n"] == day_n + 1


def test_the_review_page_works_with_the_keyboard_alone(server, browser):
    """No mouse: Tab reaches the first proposal's name and its buttons, Enter and Space act on
    photos and buttons, Escape closes the viewer. The order follows the page."""
    url = server["url"]
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.set_default_timeout(15_000)
    expect = pw.expect
    try:
        httpx.post(url + "/run", timeout=5)
        _wait(lambda: not _status(url)["state"]["running"], 60)
        pg.goto(url + "/review")
        pending = _status(url)["pending"]
        if pending == 0:
            pytest.skip("nothing left to review in this module's library")
        tid = _proposals(server["data"])["trip"]["id"]                          # the trip: a name it may keep
        first = pg.locator(f"#{tid}")
        where = ("[document.activeElement.getAttribute('name'), "
                 "(document.activeElement.closest('.card') || {}).id || '']")
        pg.locator("body").press("Tab")                                          # header links first
        for _ in range(120):
            if pg.evaluate(where) == ["name", tid]:
                break
            pg.keyboard.press("Tab")
        assert pg.evaluate(where) == ["name", tid]                               # the trip's name field
        original = pg.evaluate("document.activeElement.value")
        pg.keyboard.press("End")                                                 # Tab selected the whole value
        pg.keyboard.type(" (keyboard)")
        pg.keyboard.press("Tab")                                                 # leaving the field saves it
        expect(first.locator(".save-state")).to_have_text(re.compile(r"saved|"), timeout=5_000)
        _wait(lambda: any(p["name"] == original + " (keyboard)" for p in _proposals(server["data"]).values()))
        summary = "[document.activeElement.tagName, (document.activeElement.closest('.card') || {}).id || '']"
        for _ in range(30):                                                      # the collapsed photo list
            if pg.evaluate(summary) == ["SUMMARY", tid]:
                break
            pg.keyboard.press("Tab")
        assert pg.evaluate(summary) == ["SUMMARY", tid]
        pg.keyboard.press("Enter")                                               # opens it
        expect(first.locator("details")).to_have_attribute("open", "")
        pick = ("[document.activeElement.classList.contains('pick'), "
                "(document.activeElement.closest('.card') || {}).id || '']")
        for _ in range(30):                                                      # on to the first photo
            if pg.evaluate(pick) == [True, tid]:
                break
            pg.keyboard.press("Tab")
        assert pg.evaluate(pick) == [True, tid]
        pg.keyboard.press("Space")                                               # exclude it
        fig = first.locator("figure").first
        expect(fig).to_have_class(re.compile(r"(^|\s)excluded(\s|$)"))
        pg.keyboard.press("Enter")                                               # and back in
        expect(fig).not_to_have_class(re.compile(r"excluded"))
        pg.keyboard.press("Tab")                                                 # the view control
        assert pg.evaluate("document.activeElement.classList.contains('view')")
        pg.keyboard.press("Enter")
        expect(pg.locator(".viewer")).to_be_visible()
        pg.keyboard.press("Escape")
        expect(pg.locator(".viewer")).to_be_hidden()
        first.locator("input[name=name]").fill(original)                         # leave the name as it was
        first.locator("input[name=name]").press("Tab")
        _wait(lambda: any(p["name"] == original for p in _proposals(server["data"]).values()))
    finally:
        ctx.close()


def test_keyboard_toggles_and_the_viewer(server, page):
    url, data = server["url"], server["data"]
    expect = pw.expect
    page.goto(url + "/review")
    trip = _proposals(data)["trip"]
    card = _card(page, trip["id"])
    card.locator("details summary").click()
    fig = card.locator("figure").nth(2)
    fig.locator(".pick").focus()
    page.keyboard.press("Space")
    expect(fig).to_have_class(re.compile(r"(^|\s)excluded(\s|$)"))
    _wait(lambda: _proposals(data)["trip"]["excluded"] == [trip["photos"][2]["path"]])
    page.keyboard.press("Enter")
    expect(fig).not_to_have_class(re.compile(r"excluded"))
    _wait(lambda: _proposals(data)["trip"]["excluded"] == [])
    fig.hover()
    fig.locator(".view").click()
    expect(page.locator(".viewer")).to_be_visible()
    expect(page.locator(".viewer .v-caption")).to_contain_text("(3/")
    page.keyboard.press("Escape")
    expect(page.locator(".viewer")).to_be_hidden()


def test_video_opens_in_the_viewer(server, page):
    url = server["url"]
    expect = pw.expect
    page.goto(url + "/everyday?month=2026-06")
    video = page.locator(".thumbs figure", has=page.locator(".badge", has_text="video")).first
    expect(video).to_be_visible()
    video.hover()
    video.locator(".view").click()
    expect(page.locator(".viewer video")).to_have_attribute("src", re.compile(r"^/media\?path="))
    page.keyboard.press("Escape")


def test_remember_this_place_from_a_rename(server, page):
    url, data = server["url"], server["data"]
    expect = pw.expect
    page.goto(url + "/review")
    local = _proposals(data)["local"]
    card = _card(page, local["id"])
    card.locator("input[name=remember_place]").check()
    card.locator("input[name=name]").fill("2026-06-27 Barockstadt")
    expect(card.locator(".save-state")).to_have_text("saved")
    _wait(lambda: any(p["name"] == "Barockstadt" for p in yaml.safe_load(
        (data / "config.yaml").read_text(encoding="utf-8")).get("named_places", [])))
    page.goto(url + "/settings")
    expect(page.locator("textarea[name=named_places]")).to_have_value(re.compile(r"Barockstadt = 48\.\d+, 9\.\d+"))


def test_settings_dry_run_dialog_and_detect_home(server, page):
    url, data = server["url"], server["data"]
    expect = pw.expect
    page.goto(url + "/settings")
    page.locator("#dry").check()                                            # switching ON: no question
    page.get_by_role("button", name="Save settings").click()
    page.wait_for_url(re.compile(r"/settings"))
    expect(page.locator("header .badge", has_text="dry-run")).to_be_visible()
    page.locator("#dry").uncheck()                                          # switching OFF: asks; dismiss
    page.once("dialog", lambda d: d.dismiss())
    page.get_by_role("button", name="Save settings").click()
    page.wait_for_timeout(500)
    assert yaml.safe_load((data / "config.yaml").read_text(encoding="utf-8"))["dry_run"] is True
    page.once("dialog", lambda d: d.accept())                               # asks; accept
    page.get_by_role("button", name="Save settings").click()
    page.wait_for_url(re.compile(r"/settings"))
    expect(page.locator("header .badge", has_text="LIVE")).to_be_visible()
    expect(page.get_by_text(re.compile(r"^Dry-run is off\."))).to_be_visible()
    # detect home: in this library the trip has more distinct days than home, so it finds Lisbon
    page.get_by_role("button", name="Detect home from photos").click()
    found = page.get_by_text(re.compile(r"Home set to 38\.72\d\d, -9\.14\d\d: \d+ photos on \d+ different days"))
    expect(found).to_be_visible()
    expect(page.locator("input[name=home_lat]")).to_have_value(re.compile(r"^38\.72"))
    page.locator("input[name=home_lat]").fill(str(synth.HOME[0]))              # and back to the real home
    page.locator("input[name=home_lon]").fill(str(synth.HOME[1]))
    page.get_by_role("button", name="Save settings").click()
    page.wait_for_url(re.compile(r"/settings"))
    expect(page.locator("input[name=home_lat]")).to_have_value(re.compile(r"^48\.944"))
    assert yaml.safe_load((data / "config.yaml").read_text(encoding="utf-8"))["home_lat"] == synth.HOME[0]


def test_reject_then_approve_all_moves_the_rest(server, page):
    url, data, cfg = server["url"], server["data"], server["cfg"]
    expect = pw.expect
    page.goto(url + "/review")
    props = _proposals(data)
    open_before = _status(url)["pending"]
    _card(page, props["local"]["id"]).get_by_role("button", name="Reject (keep as everyday)").click()
    expect(page.get_by_role("heading", name=f"Proposed clusters ({open_before - 1})")).to_be_visible()
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("button", name=re.compile(r"Approve & move all")).click()
    expect(page.get_by_role("heading", name=f"Moving ({open_before - 1})")).to_be_visible()
    for pid in (props["trip"]["id"], props["home"]["id"]):
        expect(_card(page, pid).locator(".badge.ok")).to_have_text("moved", timeout=60_000)
    _wait(lambda: all(p["status"] == "applied" for p in json.loads(
        (data / "proposals.json").read_text(encoding="utf-8")).values() if p["status"] != "rejected"), 60)
    assert (Path(cfg.root) / props["trip"]["name"]).is_dir()
    assert (Path(cfg.root) / "2026-06 Grillabend").is_dir()                 # a named manual burst: root
    assert (Path(cfg.root) / cfg.unnamed_dir / props["home"]["name"]).is_dir()   # unnamed burst: _unnamed
    assert all(Path(p["path"]).exists() for p in props["local"]["photos"])   # rejected: still in the inbox
    page.goto(url + "/clusters")
    expect(page.get_by_role("link", name=props["trip"]["name"])).to_be_visible()
    expect(page.get_by_text("2026-06 Grillabend")).to_be_visible()


def test_month_navigation_and_dashboard(server, page):
    url = server["url"]
    expect = pw.expect
    page.goto(url + "/everyday")
    expect(page.locator(".monthnav .month")).to_have_count(2)
    page.get_by_role("link", name=re.compile(r"^← 2026-05")).click()
    page.wait_for_url(re.compile(r"month=2026-05"))
    expect(page.locator(".monthnav .current b")).to_have_text("2026-05")
    expect(page.locator(".thumbs figure")).to_have_count(1)
    page.get_by_role("link", name=re.compile(r"2026-06 →$")).click()
    page.wait_for_url(re.compile(r"month=2026-06"))
    page.goto(url + "/")
    stats = page.locator(".stat")
    assert stats.count() == 4
    expect(page.get_by_text("Recent activity")).to_be_visible()
    assert page.locator("table span.badge").count() >= 5
    runs = _status(url)["state"]["runs"]
    page.get_by_role("button", name="Run now").click()
    page.wait_for_url(re.compile(r"/$"))
    _wait(lambda: _status(url)["state"]["runs"] > runs and not _status(url)["state"]["running"])


def test_everyday_move_button_moves_only_old_unclustered_photos(server, page):
    url, data, cfg = server["url"], server["data"], server["cfg"]
    expect = pw.expect
    page.goto(url + "/everyday")
    button = page.get_by_role("button", name=re.compile(r"^Move \d+ everyday photos"))
    expect(button).to_be_visible()
    n = int(re.search(r"Move (\d+)", button.inner_text()).group(1))
    page.once("dialog", lambda d: d.accept())
    button.click()
    page.wait_for_url(re.compile(r"/everyday$"))
    _wait(lambda: not _status(url)["applying"] and not _status(url)["everyday_queued"], 60)
    moved = [p for p in (Path(cfg.root) / "2026").rglob("*") if p.is_file() and p.suffix in (".jpg", ".mp4")]
    assert len(moved) == n
    rejected = _proposals(data)["local"]
    assert not any(Path(p["path"]).exists() for p in rejected["photos"])   # rejected = everyday: moved too
    page.reload()
    expect(page.get_by_text("Nothing to move")).to_be_visible()


def test_sidecar_buttons_and_log_filters(server, page):
    url = server["url"]
    expect = pw.expect
    page.goto(url + "/settings")
    page.get_by_role("button", name="Move existing records to this location").click()
    expect(page.get_by_text(re.compile(r"Sidecars: \d+ moved"))).to_be_visible()
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("button", name="Remove stale records now").click()
    expect(page.get_by_text(re.compile(r"Sidecars removed: \d+ in the sorted tree"))).to_be_visible()
    page.goto(url + "/log?kind=review")
    badges = page.locator("table span.badge")
    assert badges.count() > 0
    assert set(badges.all_inner_texts()) == {"review"}
    page.goto(url + "/log?kind=apply")
    assert set(page.locator("table span.badge").all_inner_texts()) == {"apply"}
    page.goto(url + "/log")
    assert len(set(page.locator("table span.badge").all_inner_texts())) >= 3


def test_pages_fit_a_phone_screen(server, browser):
    """At 400 px width nothing scrolls sideways and the main controls stay reachable."""
    url = server["url"]
    ctx = browser.new_context(viewport={"width": 400, "height": 800}, device_scale_factor=2)
    pg = ctx.new_page()
    pg.set_default_timeout(15_000)
    try:
        for path in ("/", "/review", "/everyday", "/clusters", "/log", "/settings"):
            pg.goto(url + path)
            pg.wait_for_load_state("load")
            width, inner = pg.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
            assert width <= inner + 1, f"{path} scrolls sideways: {width} > {inner}"
            assert pg.get_by_role("button", name="Run now").is_visible()
        pg.goto(url + "/review")
        cards = pg.locator(".card[id]")
        if cards.count():
            box = cards.first.bounding_box()
            assert box and box["x"] >= 0 and box["x"] + box["width"] <= 400 + 1
    finally:
        ctx.close()
