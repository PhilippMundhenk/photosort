"""Browser tests for what made the UI feel rough: a scan finishing while the user works (the
page must not reload and must not forget anything), rapid clicking, moves finishing in place,
and a library of thousands of photos."""
from __future__ import annotations

import json
import re
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from tests import synth
from tests.e2e.test_browser import (  # noqa: F401  (fixtures are found by name)
    _proposals,
    _status,
    _wait_pending,
    browser,
    make_library,
    page,
    serve,
    server,
)

pw = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.e2e


def _run_and_wait(url: str, timeout: float = 120) -> None:
    runs = _status(url)["state"]["runs"]
    httpx.post(url + "/run", timeout=5)
    t0 = time.time()
    while True:
        s = _status(url)
        if s["state"]["runs"] > runs and not s["state"]["running"]:
            return
        assert time.time() - t0 < timeout, f"run did not finish: {s}"
        time.sleep(0.2)


def test_run_finishing_keeps_what_the_user_is_doing(server, page):
    url, data = server["url"], server["data"]
    expect = pw.expect
    _run_and_wait(url)
    page.goto(url + "/review")
    props = _proposals(data)
    trip = page.locator(f"#{props['trip']['id']}")
    page.evaluate("window.__stay = 1")
    trip.locator("details summary").click()
    figs = trip.locator("figure")
    for i in range(4):                                                  # four quick clicks, no waiting
        figs.nth(i).locator(".pick").click()
    for i in range(4):
        expect(figs.nth(i)).to_have_class(re.compile(r"(^|\s)excluded(\s|$)"))
    trip.locator("input[name=name]").fill("2026-06 Portugal")
    expect(trip.locator(".save-state")).to_have_text("saved")
    expected = sorted(p["path"] for p in props["trip"]["photos"][:4])
    t0 = time.time()
    while sorted(_proposals(data)["trip"]["excluded"]) != expected:      # all four arrived, in order
        assert time.time() - t0 < 10
        time.sleep(0.1)

    _run_and_wait(url)                                                  # a scan finishes meanwhile
    expect(page.locator("#busy-hint")).to_be_visible()
    expect(page.locator("#busy-hint")).to_contain_text("Run finished")
    assert page.evaluate("window.__stay") == 1                          # no reload happened
    expect(trip.locator("input[name=name]")).to_have_value("2026-06 Portugal")
    for i in range(4):
        expect(figs.nth(i)).to_have_class(re.compile(r"(^|\s)excluded(\s|$)"))
    expect(trip.locator("details")).to_have_attribute("open", "")
    assert sorted(_proposals(data)["trip"]["excluded"]) == expected      # and the run kept them

    page.locator("#busy-hint").click()                                  # reload, when the user wants it
    page.wait_for_load_state("load")
    assert page.evaluate("window.__stay") is None
    trip = page.locator(f"#{props['trip']['id']}")
    expect(trip.locator("input[name=name]")).to_have_value("2026-06 Portugal")
    expect(page.locator("#busy")).to_be_hidden()
    trip.locator("details summary").click()
    for i in range(4):                                                  # server-side state, as shown after reload
        expect(trip.locator("figure").nth(i)).to_have_class(re.compile(r"(^|\s)excluded(\s|$)"))
        trip.locator("figure").nth(i).locator(".pick").click()         # and back in
    t0 = time.time()
    while _proposals(data)["trip"]["excluded"]:
        assert time.time() - t0 < 10
        time.sleep(0.1)


def test_dashboard_reloads_itself_other_pages_do_not(server, page):
    url = server["url"]
    expect = pw.expect
    page.goto(url + "/")
    page.evaluate("window.__stay = 1")
    _run_and_wait(url)
    expect(page.locator(".stat").first).to_be_visible()
    t0 = time.time()
    while True:                                                         # the dashboard has nothing to lose
        try:
            if page.evaluate("window.__stay") != 1:
                break
        except pw.Error:                                                # evaluated mid-reload: reloaded
            break
        assert time.time() - t0 < 15, "dashboard did not refresh after the run"
        time.sleep(0.2)
    page.wait_for_load_state("load")
    page.wait_for_timeout(1000)                                         # the reload has committed
    page.goto(url + "/everyday")
    page.evaluate("window.__stay = 1")
    first = page.locator(".thumbs figure").first
    first.locator(".pick").click()
    expect(page.locator("#selcount")).to_have_text("1")
    _run_and_wait(url)
    expect(page.locator("#busy-hint")).to_be_visible()
    assert page.evaluate("window.__stay") == 1
    expect(first).to_have_class(re.compile(r"(^|\s)selected(\s|$)"))     # the tick survived the run
    expect(page.locator("#selcount")).to_have_text("1")


def test_polling_backs_off_when_the_tab_is_hidden(server, page):
    url = server["url"]
    page.goto(url + "/review")
    hits = []
    page.on("request", lambda r: hits.append(r.url) if r.url.endswith("/api/status") else None)
    page.wait_for_timeout(6000)                                         # (events arrive during Playwright calls)
    assert len(hits) >= 1
    page.evaluate("Object.defineProperty(document, 'hidden', {value: true, configurable: true});"
                  "document.dispatchEvent(new Event('visibilitychange'))")
    hits.clear()
    page.wait_for_timeout(6000)
    assert len(hits) <= 1                                               # a hidden tab hardly asks


@pytest.fixture(scope="module")
def big_server(tmp_path_factory):
    """The synthetic library plus 4000 everyday photos at home over ~2.7 years, one every six
    hours (below any burst threshold), records beside the photos."""
    base, lib = make_library(tmp_path_factory, "e2e-big")
    inbox = Path(lib.cfg.inboxes[0]["path"])
    start = synth.T0 - timedelta(days=1000)
    for i in range(4000):
        lib.photo(inbox, start + timedelta(hours=6 * i), synth.HOME)
    with serve(base, lib) as s:
        yield s


def test_large_library_stays_responsive(big_server, page):
    url = big_server["url"]
    expect = pw.expect
    runs = _status(url)["state"]["runs"]
    httpx.post(url + "/run", timeout=5)
    latencies = []
    for _ in range(10):                                                 # while it scans and clusters
        t0 = time.time()
        assert httpx.get(url + "/api/status", timeout=10).status_code == 200
        latencies.append(time.time() - t0)
    assert max(latencies) < 1.0, latencies
    t0 = time.time()
    while not (_status(url)["state"]["runs"] > runs and not _status(url)["state"]["running"]):
        assert time.time() - t0 < 180
        time.sleep(0.5)
    assert _status(url)["pending"] == 3

    t0 = time.time()
    page.goto(url + "/everyday")
    expect(page.get_by_role("heading", name="Everyday photos")).to_be_visible()
    assert time.time() - t0 < 6
    assert 0 < page.locator("figure").count() <= 400                    # one month, not all 4000
    assert page.locator(".monthnav .month").count() >= 30

    t0 = time.time()
    page.goto(url + "/review")
    expect(page.get_by_role("heading", name="Proposed clusters (3)")).to_be_visible()
    assert time.time() - t0 < 6
    t0 = time.time()
    assert httpx.get(url + "/api/status", timeout=10).status_code == 200  # thumbnails loading: still quick
    assert time.time() - t0 < 1.0

    trip_id = next(k for k, p in json.loads((big_server["data"] / "proposals.json")
                                                          .read_text(encoding="utf-8")).items()
                   if p["kind"] == "trip")
    trip = page.locator(f"#{trip_id}")
    trip.locator("details summary").click()
    figs = trip.locator("figure")
    t0 = time.time()
    for i in range(8):
        figs.nth(i).locator(".pick").click()
    for i in range(8):
        expect(figs.nth(i)).to_have_class(re.compile(r"(^|\s)excluded(\s|$)"))
    assert time.time() - t0 < 5                                         # instant on the page
    props = json.loads((big_server["data"] / "proposals.json").read_text(encoding="utf-8"))
    t0 = time.time()
    while len(props[trip_id]["excluded"]) != 8:                          # and saved shortly after
        assert time.time() - t0 < 10
        time.sleep(0.2)
        props = json.loads((big_server["data"] / "proposals.json").read_text(encoding="utf-8"))
