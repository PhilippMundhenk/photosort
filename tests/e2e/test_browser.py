"""Browser end-to-end tests: a real uvicorn server, a real (headless) Chromium via Playwright,
the synthetic library on disk. Run: pytest -m e2e   (needs `playwright install chromium`;
the test Docker image has it)."""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml

from tests import synth

pw = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    base = tmp_path_factory.mktemp("e2e")
    data = base / "data"
    data.mkdir()
    cfg = synth.make_config(base, sidecar_mode="beside")   # records must be visible to the server process
    (data / "config.yaml").write_text(yaml.safe_dump(cfg.as_dict(), sort_keys=False), encoding="utf-8")
    lib = synth.populate(cfg)

    port = _free_port()
    env = {**os.environ, "PHOTOSORT_DATA": str(data)}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port),
                             "--host", "127.0.0.1", "--log-level", "warning"], cwd=ROOT, env=env)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(url + "/api/status", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None:
                raise RuntimeError("uvicorn exited early")
            time.sleep(0.2)
        else:
            raise RuntimeError("server did not start")
        yield {"url": url, "data": data, "cfg": cfg, "lib": lib}
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # noqa: BLE001  (browser binaries missing)
            pytest.skip(f"chromium not available: {e}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.set_default_timeout(15_000)
    yield pg
    ctx.close()


def _status(url: str) -> dict:
    return httpx.get(url + "/api/status", timeout=5).json()


def _wait_pending(url: str, n: int, timeout: float = 60) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = _status(url)
        if s["pending"] == n and not s["state"]["running"]:
            return
        time.sleep(0.3)
    raise AssertionError(f"pending never reached {n}: {_status(url)}")


def _proposals(data: Path) -> dict:
    return {p["kind"]: p for p in json.loads((data / "proposals.json").read_text(encoding="utf-8")).values()}


def test_full_review_flow(server, page):
    url, data, cfg = server["url"], server["data"], server["cfg"]
    expect = pw.expect

    # dashboard, run now
    page.goto(url + "/")
    expect(page.get_by_role("heading", name="Dashboard")).to_be_visible()
    expect(page.get_by_text("dry-run")).to_be_visible()
    page.get_by_role("button", name="Run now").click()
    _wait_pending(url, 3)
    page.reload()
    expect(page.locator(".stat").first).to_have_text("3")

    # review queue: rename, toggle a photo, approve the trip
    page.goto(url + "/review")
    expect(page.get_by_role("heading", name="Proposed clusters (3)")).to_be_visible()
    props = _proposals(data)
    trip = page.locator(f"#{props['trip']['id']}")
    expect(trip.locator("input[name=name]")).to_have_value("2026-06 Lisbon, Sevilla")
    trip.locator("input[name=name]").fill("2026-06 Portugal & Spain")       # no button: saved as you type
    expect(trip.locator(".save-state")).to_have_text("saved")
    page.reload()
    trip = page.locator(f"#{props['trip']['id']}")
    expect(trip.locator("input[name=name]")).to_have_value("2026-06 Portugal & Spain")
    expect(trip.get_by_role("button", name="rename")).to_have_count(0)

    trip.locator("details summary").click()
    page.wait_for_load_state("networkidle")                            # thumbnails loaded
    first_fig = trip.locator("figure").first
    first_fig.locator("button.pick").click()
    expect(page.locator(f"#{props['trip']['id']} figure").first).to_have_class(re.compile(r"(^|\s)excluded(\s|$)"))
    expect(page.locator(f"#{props['trip']['id']} details")).to_have_attribute("open", "")   # no reload, stays open
    assert page.url.endswith("/review")                                                    # no navigation happened
    assert _proposals(data)["trip"]["excluded"] == [props["trip"]["photos"][0]["path"]]
    first_fig.locator("button.pick").click()                                               # and back in
    expect(page.locator(f"#{props['trip']['id']} figure").first).not_to_have_class(re.compile(r"excluded"))
    assert _proposals(data)["trip"]["excluded"] == []

    # full-size viewer: opens on the eye button, arrows navigate, Esc closes
    trip.locator("figure").first.hover()
    trip.locator("button.view").first.click()
    expect(page.locator(".viewer")).to_be_visible()
    expect(page.locator(".viewer img")).to_have_attribute("src", re.compile(r"^/media\?path="))
    expect(page.locator(".viewer .v-caption")).to_contain_text("(1/")
    page.keyboard.press("ArrowRight")
    expect(page.locator(".viewer .v-caption")).to_contain_text("(2/")
    page.keyboard.press("Escape")
    expect(page.locator(".viewer")).to_be_hidden()

    page.locator(f"#{props['trip']['id']}").get_by_role("button", name="Approve & move").click()
    expect(page.get_by_role("heading", name="Proposed clusters (2)")).to_be_visible()
    expect(page.get_by_role("heading", name="Moving (1)")).to_be_visible()     # files move in the background
    folder = Path(cfg.root) / "2026-06 Portugal & Spain"
    expect(page.get_by_role("heading", name="Moving (1)")).to_be_hidden(timeout=60_000)   # page reloads when done
    assert folder.is_dir() and (folder / "manifest.json").exists()
    assert not Path(props["trip"]["photos"][0]["path"]).exists()      # toggled back in above, so it moved
    assert not list(folder.rglob(cfg.review_dir))                      # manual approval: no _review

    # reject the day out
    page.locator(f"#{props['local']['id']}").get_by_role("button", name="Reject (keep as everyday)").click()
    expect(page.get_by_role("heading", name="Proposed clusters (1)")).to_be_visible()

    # approve the home burst, then name it from the "waiting for a name" list
    page.wait_for_load_state("networkidle")   # Windows cannot move files the browser is still reading
    page.locator(f"#{props['home']['id']}").get_by_role("button", name="Approve & move").click()
    expect(page.get_by_role("heading", name="Moving (1)")).to_be_hidden(timeout=60_000)
    expect(page.get_by_role("heading", name="Bursts waiting for a name (1)")).to_be_visible()
    name_box = page.get_by_placeholder("What was this?")
    name_box.fill("2026-06-30 Hannas Geburtstag")
    page.wait_for_load_state("networkidle")
    page.get_by_role("button", name="Name it").click()
    expect(page.get_by_role("heading", name="2026-06-30 Hannas Geburtstag")).to_be_visible()
    assert (Path(cfg.root) / "2026-06-30 Hannas Geburtstag").is_dir()
    assert not (Path(cfg.root) / cfg.unnamed_dir / props["home"]["name"]).exists()

    # cluster page: move one photo out, then undo the whole cluster (confirm dialog)
    page.wait_for_load_state("networkidle")
    page.get_by_role("button", name="not this trip").first.click()
    expect(page.get_by_text("Corrections")).to_be_visible()
    page.wait_for_load_state("networkidle")
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("button", name="Undo whole cluster").click()
    page.wait_for_url("**/clusters")
    expect(page.get_by_role("link", name="2026-06 Portugal & Spain")).to_be_visible()
    expect(page.get_by_text("Hannas Geburtstag")).to_have_count(0)
    assert not (Path(cfg.root) / "2026-06-30 Hannas Geburtstag").exists()

    # log shows the trail
    page.goto(url + "/log")
    expect(page.get_by_role("heading", name="Log")).to_be_visible()
    for kind in ("apply", "label", "correction", "undo"):
        expect(page.locator("span.badge", has_text=kind).first).to_be_visible()


def test_settings_roundtrip(server, page):
    url = server["url"]
    expect = pw.expect
    page.goto(url + "/settings")
    page.locator("input[name=scan_interval_min]").fill("7")
    page.get_by_label("Copy instead of move", exact=False).check()        # label[for] -> checkbox
    page.get_by_role("button", name="Save settings").click()
    page.wait_for_url("**/settings")
    expect(page.locator("input[name=scan_interval_min]")).to_have_value("7")
    expect(page.locator("#copy")).to_be_checked()
    saved = yaml.safe_load((server["data"] / "config.yaml").read_text(encoding="utf-8"))
    assert saved["scan_interval_min"] == 7 and saved["copy_instead_of_move"] is True
    expect(page.locator("#dry")).to_be_checked()                          # was ticked before, still is
    expect(page.locator("#at")).not_to_be_checked()
