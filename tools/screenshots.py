"""Documentation screenshots (README, docs/): the real UI on the synthetic library, whose photos
are replaced by small generated pictures (sky, ground, sun; colours by place) so the grids look
like photos instead of placeholders. Runs in the test image:

    docker run --rm -v "$PWD/docs/screenshots:/out" photosort-test:local python -m tools.screenshots /out
"""
from __future__ import annotations

import random
import sys
import tempfile
import time
from pathlib import Path

import httpx
import yaml
from PIL import Image, ImageDraw

from app import geo, ingest
from tests import synth
from tests.e2e.test_browser import serve

# the synthetic library's home and day out are moved to a generic region for public pictures
SHOT_HOME, SHOT_DAY_OUT = (48.137, 11.575), (47.998, 11.340)          # Munich, Starnberg

PALETTES = {                       # sky, ground, accent per kind of place
    "home": ((150, 200, 240), (90, 160, 80), (230, 120, 90)),
    "Lisbon": ((90, 170, 230), (230, 200, 140), (250, 210, 80)),
    "Sevilla": ((120, 180, 235), (220, 150, 90), (250, 190, 70)),
    "Starnberg": ((150, 190, 235), (80, 130, 170), (200, 180, 140)),
    None: ((160, 170, 185), (120, 125, 135), (200, 200, 200)),
}


def picture(path: Path, place: str | None, seed: int) -> None:
    rnd = random.Random(seed)
    sky, ground, accent = PALETTES.get(place, PALETTES[None])
    jitter = lambda c: tuple(max(0, min(255, v + rnd.randint(-25, 25))) for v in c)   # noqa: E731
    im = Image.new("RGB", (480, 360), jitter(sky))
    d = ImageDraw.Draw(im)
    horizon = rnd.randint(170, 250)
    d.rectangle([0, horizon, 480, 360], fill=jitter(ground))
    for _ in range(rnd.randint(1, 3)):                                    # hills, roofs, waves
        x = rnd.randint(-80, 400)
        d.polygon([(x, horizon), (x + rnd.randint(80, 200), horizon - rnd.randint(30, 110)),
                   (x + rnd.randint(200, 320), horizon)], fill=jitter(ground))
    r = rnd.randint(18, 34)
    cx, cy = rnd.randint(60, 420), rnd.randint(40, horizon - 60)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=jitter(accent))
    im.save(path, "JPEG", quality=80)


def main(out: Path) -> None:
    from playwright.sync_api import sync_playwright
    out.mkdir(parents=True, exist_ok=True)
    synth.HOME, synth.RATINGEN = SHOT_HOME, SHOT_DAY_OUT
    base = Path("/photos")                                              # the paths a real setup shows
    try:
        base.mkdir()
    except OSError:
        base = Path(tempfile.mkdtemp(prefix="shots-"))
    (base / "data").mkdir()
    cfg = synth.make_config(base, sidecar_mode="beside", dry_run=True, home_lat=SHOT_HOME[0],
                            home_lon=SHOT_HOME[1], root=str(base / "sorted"))
    (base / "data" / "config.yaml").write_text(yaml.safe_dump(cfg.as_dict(), sort_keys=False), encoding="utf-8")
    lib = synth.populate(cfg)
    for i, p in enumerate(lib.paths):
        rec = ingest.read_sidecar(p, cfg) or {}
        place = None
        if rec.get("zone") == geo.ZONE_HOME:
            place = "home"
        elif rec.get("place"):
            place = rec["place"].get("place")
        if p.suffix.lower() == ".jpg":
            picture(p, place, i)
    with serve(base, lib) as s, sync_playwright() as pw:
        url = s["url"]
        httpx.post(url + "/run")
        t0 = time.time()
        while time.time() - t0 < 60:
            st = httpx.get(url + "/api/status").json()
            if st["pending"] >= 3 and not st["state"]["running"]:
                break
            time.sleep(0.5)
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860}, color_scheme="dark")

        def shot(path: str, name: str, wait: float = 1.5, full: bool = False) -> None:
            page.goto(url + path)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(int(wait * 1000))
            page.screenshot(path=str(out / f"{name}.png"), full_page=full)
            print("wrote", out / f"{name}.png")
        page.goto(url + "/review")
        page.wait_for_load_state("networkidle")
        trip = page.locator(".card", has=page.locator(".badge.trip")).first
        trip.scroll_into_view_if_needed()
        page.evaluate("document.querySelector('.badge.trip').closest('.card').scrollIntoView()")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out / "review.png"))
        print("wrote", out / "review.png")
        shot("/everyday?month=2026-06", "everyday")
        shot("/settings", "settings")
        page.set_viewport_size({"width": 390, "height": 780})            # a phone
        shot("/review", "review-phone")
        browser.close()


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "docs/screenshots"))
