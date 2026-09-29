"""How much of ui.js the browser executes: Chromium's precise JS coverage over one session that
goes through every handler of the script (toggles by click and key, the viewer, autosave and
its failure, approve with a name, approve all, the busy banner and its reload offer, everyday
selection and assignment, the everyday move, cluster actions, the dry-run dialog, tab
visibility). The functions left unexecuted are printed; the executed share must stay high.

Async Playwright, because the coverage snapshot has to be taken from inside a route handler
(the moment a navigation request leaves, the handler that caused it has run and the document
still exists); the sync API cannot call the protocol from a handler."""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from tests import synth
from tests.e2e.test_browser import ROOT, _proposals, _status, make_library, serve

pw = pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

MIN_SHARE = 0.99
STATUS = re.compile(r"/api/status$")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """The synthetic library plus a big trip and many everyday photos: moves must take seconds so
    the pages get to show their progress and the finished card."""
    base, lib = make_library(tmp_path_factory, "e2e-jscov")
    inbox = Path(lib.cfg.inboxes[0]["path"])
    day = synth.T0 + timedelta(days=3)
    for i in range(2000):                                                 # inside the Lisbon trip
        lib.photo(inbox, day + timedelta(days=(i % 14), minutes=10 + (i // 14) * 9), synth.LISBON)
    for i in range(1500):                                                 # everyday, four a day, no bursts
        lib.photo(inbox, synth.T0 - timedelta(days=400) + timedelta(hours=6 * i), synth.HOME)
    lib.video(inbox, synth.T0 + timedelta(days=5, hours=12), synth.LISBON)   # a video inside the trip
    with serve(base, lib) as s:
        yield s


def _share(entries: list[dict], source: str) -> tuple[float, list[str]]:
    """Executed share of ui.js code bytes over every load of it, and the functions never executed.
    Ranges come outer to inner per function; the innermost count decides for a byte."""
    hit = bytearray(len(source))
    runs: dict[tuple[str, int], int] = {}
    for e in entries:
        if not e["url"].split("?")[0].endswith("/static/ui.js"):
            continue
        this = bytearray(len(source))
        for f in e["functions"]:
            for r in f["ranges"]:
                a, b = r["startOffset"], r["endOffset"]
                this[a:b] = bytes([1 if r["count"] > 0 else 0]) * (b - a)
            if f["ranges"]:
                key = (f["functionName"] or "(anonymous)", f["ranges"][0]["startOffset"])
                runs[key] = max(runs.get(key, 0), f["ranges"][0]["count"])
        hit = bytearray(a | b for a, b in zip(hit, this, strict=True))
    code = [i for i, ch in enumerate(source) if not ch.isspace()]
    share = sum(hit[i] for i in code) / max(1, len(code))
    lines = source.splitlines()
    never = []
    missed_lines = sorted({source[:i].count(chr(10)) + 1 for i in code if not hit[i]})
    for n in missed_lines[:40]:
        never.append(f"line {n} not fully executed: {lines[n - 1].strip()[:90]}")
    for (name, off), n in sorted(runs.items(), key=lambda kv: kv[0][1]):
        if n == 0:
            line = source[:off].count(chr(10)) + 1
            never.append(f"{name} at line {line}: {lines[line - 1].strip()[:70]}")
    return share, never


async def _await(page, cond, timeout: float = 60) -> None:
    """Wait for a condition without blocking the event loop: every request of the page passes
    through the test's route handler, which can only run while the loop runs."""
    t0 = time.time()
    while not cond():
        assert time.time() - t0 < timeout, "condition never became true"
        await page.wait_for_timeout(200)


async def _arun(page, url: str) -> None:
    runs = _status(url)["state"]["runs"]
    httpx.post(url + "/run", timeout=5)
    await _await(page, lambda: _status(url)["state"]["runs"] > runs and not _status(url)["state"]["running"])


async def _session(url: str, data: Path, cfg) -> list[dict]:
    expect = pw.expect
    async with pw.async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context()
        page = await ctx.new_page()
        page.set_default_timeout(15_000)
        expect.set_options(timeout=15_000)
        cdp = await ctx.new_cdp_session(page)
        await cdp.send("Profiler.enable")
        await cdp.send("Profiler.startPreciseCoverage", {"callCount": True, "detailed": True})
        entries: list[dict] = []
        nav: dict = {"allow": False, "last": None}

        async def snap() -> None:
            entries.extend((await cdp.send("Profiler.takePreciseCoverage"))["result"])

        # A navigation destroys the document and with it what its scripts executed, and the
        # profiler cannot be asked while a navigation is paused. So every navigation the page
        # starts by itself (a form submit, a reload) is answered with 204, which browsers drop
        # while keeping the document; the test then replays the request, snapshots, and loads
        # the target itself. Only the test's own goto() navigations pass through.
        async def gate(route, request):
            if request.is_navigation_request() and not nav["allow"]:
                nav["last"] = (request.method, request.url, request.post_data, request.headers.get("content-type"))
                await route.fulfill(status=204)
            else:
                await route.continue_()
        await page.route(lambda u: True, gate)

        async def go(u: str) -> None:
            await snap()
            nav["allow"], nav["last"] = True, None
            try:
                await page.goto(u)
            finally:
                nav["allow"] = False

        async def follow() -> None:
            """The navigation the page just tried: replay it on the server, then load where it leads."""
            t0 = time.time()
            while nav["last"] is None:
                assert time.time() - t0 < 15, "the page did not try to navigate"
                await page.wait_for_timeout(100)
            method, u, body, ctype = nav["last"]
            target = u
            if method == "POST":
                r = httpx.post(u, content=body or "", headers={"content-type": ctype or ""},
                               follow_redirects=False, timeout=30)
                target = r.headers.get("location", u)
                if target.startswith("/"):
                    target = url + target
            await go(target)

        async def status_fails(route):
            await route.abort()

        def accept(d):
            return d.accept()

        def dismiss(d):
            return d.dismiss()

        # dashboard: the banner poll, a run, the self-reload of a stateless page (dropped, replayed)
        await go(url + "/")
        await _arun(page, url)
        await follow()
        await _await(page, lambda: _status(url)["pending"] == 3 and not _status(url)["state"]["running"])

        # review: toggles by click and key, a failed request, the viewer
        await go(url + "/review")
        props = _proposals(data)
        trip = page.locator(f"#{props['trip']['id']}")
        await trip.locator("details summary").click()
        figs = trip.locator("figure")
        await figs.nth(0).locator(".pick").click()
        await figs.nth(1).locator(".pick").click()
        await figs.nth(1).locator(".pick").click()
        await _await(page, lambda: _proposals(data)["trip"]["excluded"] == [props["trip"]["photos"][0]["path"]])
        await figs.nth(0).locator(".pick").focus()
        await page.keyboard.press("Enter")
        await _await(page, lambda: _proposals(data)["trip"]["excluded"] == [])
        toggle = re.compile(r"/proposal/.*/toggle$")
        await page.route(toggle, lambda route: route.abort())
        await figs.nth(2).locator(".pick").click()
        await expect(trip).to_have_class(re.compile(r"unsaved"))
        await page.unroute(toggle)
        await page.route(toggle, lambda route: route.fulfill(status=500, body="down"))   # an error answer
        await figs.nth(2).locator(".pick").click()
        await page.wait_for_timeout(300)
        await page.unroute(toggle)
        await page.route(toggle, lambda route: route.fulfill(status=200, content_type="application/json",
                                                             body="{}"))                 # an answer without paths
        await figs.nth(3).locator(".pick").click()
        await expect(trip).not_to_have_class(re.compile(r"unsaved"))
        await page.unroute(toggle)
        await figs.nth(3).locator(".pick").click()                            # for real now: out and back in
        await figs.nth(3).locator(".pick").click()
        await figs.nth(2).locator(".pick").click()
        await figs.nth(2).locator(".pick").click()
        await _await(page, lambda: _proposals(data)["trip"]["excluded"] == [])
        await figs.nth(0).hover()
        await figs.nth(0).locator(".view").click()
        await page.keyboard.press("ArrowLeft")                                # before the first: stays
        await expect(page.locator(".viewer .v-caption")).to_contain_text("(1/")
        await page.locator(".viewer .v-next").click()
        await page.locator(".viewer .v-prev").click()
        await page.keyboard.press("ArrowRight")
        await page.keyboard.press("ArrowLeft")
        await page.locator(".viewer").click(position={"x": 5, "y": 5})
        await expect(page.locator(".viewer")).to_be_hidden()
        video = trip.locator("figure", has=page.locator(".badge", has_text="video")).first
        await video.locator(".view").dispatch_event("click")                  # a video in the viewer (deep in a
        await expect(page.locator(".viewer")).to_be_visible()                 # 2000-photo grid: no hover, no scroll)
        await expect(page.locator(".viewer video")).to_have_attribute("src", re.compile(r"^/media"))
        await page.keyboard.press("Escape")
        await figs.nth(0).locator(".view").click()
        await page.locator(".viewer .v-close").click()
        await figs.nth(0).locator(".view").click()
        await page.keyboard.press("Escape")
        await expect(page.locator(".viewer")).to_be_hidden()

        # autosave: as you type, on Enter, a failing save, the fading note, a failing poll
        rename = re.compile(r"/proposal/.*/rename$")
        await page.route(rename, lambda route: route.fulfill(status=500, content_type="application/json",
                                                             body='{"error": "boom"}'))
        await trip.locator("input[name=name]").fill("2026-06 Fails")
        await expect(trip.locator(".save-state")).to_have_text("not saved: boom")
        await page.unroute(rename)
        await trip.locator("input[name=name]").fill("2026-06 Portugal")
        await expect(trip.locator(".save-state")).to_have_text("saved")
        await expect(trip.locator(".save-state")).to_have_text("")
        await trip.locator("input[name=name]").press("Enter")
        await page.route(STATUS, status_fails)
        await page.wait_for_timeout(5500)
        await page.unroute(STATUS)

        # approve the big trip with the name and the remember box; its card while moving, then
        # moved; the reload offer
        local = page.locator(f"#{props['local']['id']}")
        await local.locator("input[name=name]").fill("2026-06-27 Barockstadt")
        await expect(local.locator(".save-state")).to_have_text("saved")
        await trip.locator("input[name=remember_place]").check()
        await trip.locator("input[name=name]").fill("2026-06 Portugal & Spain")
        await trip.get_by_role("button", name="Approve & move").click()
        await follow()
        await expect(page.get_by_role("heading", name="Moving (1)")).to_be_visible()
        await expect(trip.locator("[data-progress]")).to_contain_text(re.compile(r"\d+ / 2"))
        await expect(trip.locator(".badge.ok")).to_have_text("moved", timeout=120_000)
        await go(url + "/review")

        # the tab hidden and shown again; a run finishing while the page is open
        await page.evaluate("Object.defineProperty(document, 'hidden', {value: true, configurable: true});"
                            "document.dispatchEvent(new Event('visibilitychange'))")
        await page.wait_for_timeout(300)
        await page.evaluate("Object.defineProperty(document, 'hidden', {value: false, configurable: true});"
                            "document.dispatchEvent(new Event('visibilitychange'))")
        await _arun(page, url)
        await expect(page.locator("#busy-hint")).to_be_visible()

        # approve all with names, then the moved cards
        await go(url + "/review")
        await page.locator(f"#{props['home']['id']} input[name=name]").fill("2026-06-30 Hannas Geburtstag")
        await page.locator(f"#{props['local']['id']} input[name=remember_place]").check()   # travels along
        page.once("dialog", accept)
        await page.get_by_role("button", name=re.compile(r"Approve & move all")).click()
        await follow()
        await _await(page, lambda: all(p["status"] == "applied" for p in _proposals(data).values()), 120)

        # everyday: pick, select all by click and key, assign; the move button and its card
        await go(url + "/everyday")
        await page.locator(".thumbs figure").first.locator(".pick").click()
        await page.locator("[data-select-all]").first.click()
        await page.locator("[data-select-all]").first.focus()
        await page.keyboard.press("Space")
        await expect(page.locator("#selcount")).not_to_have_text("0")
        await page.locator("#assign input[name=name]").fill("2026-06 Grillabend")
        await page.get_by_role("button", name="Assign").click()
        await page.get_by_role("button", name="Assign").click()               # a second submit replaces its inputs
        await follow()
        assert "/review?open=" in page.url
        await go(url + "/everyday")
        page.once("dialog", accept)
        await page.get_by_role("button", name=re.compile(r"^Move \d+ everyday photos")).click()
        await follow()
        await expect(page.locator("[data-progress=everyday]")).to_contain_text(re.compile(r"\d+ / 1"))
        await expect(page.locator("[data-progress=everyday]")).to_have_text("done", timeout=120_000)
        await expect(page.locator("#busy-hint")).to_be_hidden()

        # cluster page: rename on leaving the field, remove a photo (declined, then done), put back
        folder = Path(cfg.root) / "2026-06-27 Barockstadt"
        await go(url + "/clusters/view?folder=" + str(folder))
        await page.locator("input[name=name]").fill("2026-06-27 Barock")
        await page.locator("input[name=name]").press("Tab")
        await follow()
        assert page.url.endswith("Barock")
        page.once("dialog", dismiss)
        await page.get_by_role("button", name="remove from cluster").first.click()
        await page.wait_for_timeout(300)
        page.once("dialog", accept)
        await page.get_by_role("button", name="remove from cluster").first.click()
        await follow()
        await expect(page.get_by_text("Removed from this cluster")).to_be_visible()
        await page.get_by_role("button", name="put back").first.click()
        await follow()
        await expect(page.get_by_text("Removed from this cluster")).to_have_count(0)

        # settings: the dry-run question, dismissed and accepted
        await go(url + "/settings")
        await page.locator("#dry").check()
        await page.get_by_role("button", name="Save settings").click()
        await follow()
        await expect(page.locator("header .badge", has_text="dry-run")).to_be_visible()
        await page.locator("#dry").uncheck()
        page.once("dialog", dismiss)
        await page.get_by_role("button", name="Save settings").click()
        await page.wait_for_timeout(300)
        assert nav["last"] is None                                            # declined: no navigation
        page.once("dialog", accept)
        await page.get_by_role("button", name="Save settings").click()
        await follow()
        await expect(page.locator("header .badge", has_text="LIVE")).to_be_visible()

        await snap()
        await ctx.close()
        await browser.close()
    return entries


def test_ui_script_is_exercised_end_to_end(server):
    url, data, cfg = server["url"], server["data"], server["cfg"]
    entries = asyncio.run(_session(url, data, cfg))
    share, never = _share(entries, (ROOT / "app" / "static" / "ui.js").read_text(encoding="utf-8"))
    (data / "ui-coverage.json").write_text(json.dumps({"share": share, "never": never}), encoding="utf-8")
    print(f"\n  ui.js executed: {share * 100:.1f} % of its code; functions never run: {len(never)}")
    for line in never:
        print("   -", line)
    assert share >= MIN_SHARE, f"only {share * 100:.1f} % of ui.js executed; never run: {never}"
