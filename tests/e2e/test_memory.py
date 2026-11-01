"""Browser memory (issue #69): a page left open keeps polling the status, and the user keeps
clicking; neither may grow the JS heap, the DOM or the number of event listeners. Measured
through the Chrome DevTools Protocol after a forced garbage collection, so what is counted is
what the page really holds on to."""
from __future__ import annotations

import httpx
import pytest

from tests.e2e.test_browser import (  # noqa: F401  (fixtures are found by name)
    _proposals,
    _wait_pending,
    browser,
    make_library,
    page,
    serve,
)

pw = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.e2e

HEAP_SLACK = 1_500_000          # bytes: V8 settles in steps; a leak per poll or per click is far more


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    base, lib = make_library(tmp_path_factory, "e2e-memory")
    with serve(base, lib) as s:
        yield s


def _measure(cdp) -> dict:
    for _ in range(2):                                   # twice: finalizers free more on the second pass
        cdp.send("HeapProfiler.collectGarbage")
    m = {x["name"]: x["value"] for x in cdp.send("Performance.getMetrics")["metrics"]}
    return {"heap": m["JSHeapUsedSize"], "nodes": m["Nodes"], "listeners": m["JSEventListeners"]}


def _cdp(page):
    cdp = page.context.new_cdp_session(page)
    cdp.send("Performance.enable")
    return cdp


def test_an_open_page_does_not_grow_while_it_polls(server, page):
    """The status poll runs every 2-5 s on every open page: after a dozen polls the page holds
    no more nodes, listeners or heap than after the first ones."""
    url = server["url"]
    httpx.post(url + "/run")
    _wait_pending(url, 3)
    page.goto(url + "/review")
    cdp = _cdp(page)
    page.wait_for_timeout(6_000)                         # the first polls, lazy chunks, thumbnails
    before = _measure(cdp)
    page.wait_for_timeout(40_000)                        # eight or more polls more
    after = _measure(cdp)
    assert after["nodes"] - before["nodes"] <= 5, (before, after)
    assert after["listeners"] - before["listeners"] <= 2, (before, after)
    assert after["heap"] - before["heap"] <= HEAP_SLACK, (before, after)


def test_repeated_clicks_viewer_and_lazy_lists_leave_nothing_behind(server, page):
    """Toggle a photo out and back in, open and close the full-size viewer, close and reopen a
    photo list: twenty rounds hold no more than the first two."""
    url, data = server["url"], server["data"]
    page.goto(url + "/review")
    trip = page.locator(f"#{_proposals(data)['trip']['id']}")
    trip.scroll_into_view_if_needed()
    figure = trip.locator("figure").first
    pw.expect(figure).to_be_visible()
    cdp = _cdp(page)

    def round_():
        figure.locator(".pick").click()                  # out ...
        figure.locator(".pick").click()                  # ... and back in
        figure.locator(".view").click()                  # the viewer
        pw.expect(page.locator(".viewer")).to_be_visible()
        page.keyboard.press("Escape")
        pw.expect(page.locator(".viewer")).to_be_hidden()
        trip.locator("details summary").click()          # the list closed and opened again
        trip.locator("details summary").click()
    for _ in range(2):
        round_()
    page.wait_for_timeout(1_500)                         # toggles are batched and sent
    before = _measure(cdp)
    for _ in range(18):
        round_()
    page.wait_for_timeout(1_500)
    after = _measure(cdp)
    assert after["nodes"] - before["nodes"] <= 20, (before, after)
    assert after["listeners"] - before["listeners"] <= 3, (before, after)
    assert after["heap"] - before["heap"] <= HEAP_SLACK, (before, after)
    excluded = _proposals(data)["trip"].get("excluded", [])
    assert excluded == []                                # every round put the photo back


def test_thumbnails_are_sized_for_the_page(server, page):
    """Every thumbnail declares its size and decodes off the main thread, and figures off screen
    are skipped by layout and paint (their decoded images need not stay in memory)."""
    url = server["url"]
    page.goto(url + "/review")
    imgs = page.locator(".thumbs img")
    pw.expect(imgs.first).to_be_attached()
    attrs = page.evaluate("""() => Array.from(document.querySelectorAll('.thumbs img')).map(i =>
        [i.getAttribute('width'), i.getAttribute('height'), i.decoding, i.loading])""")
    assert attrs and all(a == ["110", "82", "async", "lazy"] for a in attrs)
    cv = page.evaluate("() => getComputedStyle(document.querySelector('.thumbs figure')).contentVisibility")
    assert cv == "auto"
