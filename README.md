# photosort

[![CI](https://github.com/PhilippMundhenk/photosort/actions/workflows/ci.yml/badge.svg)](https://github.com/PhilippMundhenk/photosort/actions/workflows/ci.yml)

Sorts incoming photos from phones and cameras into a folder tree by **trip**, **day out** and
**occasion at home**, fully locally, on weak hardware (built for a CPU-only ThinkPad T430).
The filesystem is the only state, the rules are the whole logic, and a web UI reviews every
move. (A "System One" decision model was tried for the one judgment call the rules cannot
make and dropped again; see [docs/DESIGN.md](docs/DESIGN.md) section 10.)

See [docs/DESIGN.md](docs/DESIGN.md) for scope, the rules and why they were chosen.

## What it does

```
inbox/phone-a/**.jpg ─┐                              sorted/2026-06-04..24 Lisbon, Sevilla/
inbox/phone-b/**.jpg ─┼─ scan ─ zone ─ cluster ─►    sorted/2026-06-27 Ludwigsburg/
inbox/camera/**.dng  ─┘                              sorted/_unnamed/2026-06-30 (25 Fotos)/   ← you name it
                                                     sorted/2026/06/                          ← everything else
```

1. **Scan** (every N minutes): every photo and video in every input folder (recursive) gets a
   JSON record (by default in `data/index/`, optionally as a sidecar next to the file; see
   Settings → Sidecars) with timestamp, GPS, offline reverse-geocoded place and
   its *zone* relative to your home: `home` (< 0.5 km), `local` (< 20 km), `away` (beyond).
   Photos without GPS take the position of the nearest photo in time that has one.
   Videos (iPhone and Android MP4/MOV) are read the same way: GPS from the QuickTime keys,
   the timestamp from `CreationDate` (iPhone, with offset) or the spec-UTC `CreateDate`
   converted to your home time zone (`TZ` / Settings), the device from the metadata or, when
   the video carries none (Android), from the inbox name.
2. **Cluster** (deterministic):
   - **Trip** = a run of photos away from home spanning at least 20 hours. Any `home` photo
     ends it, no matter how short the stay. No-GPS photos inside the run ride along.
     Lisbon → Seville → Lisbon is one trip. Name: `YYYY-MM-DD..DD Place, Place` (places in
     order of first appearance; countries if more than four; "Multiple" beyond that). Places
     are your own named places (Settings, or "remember this place" when renaming), else the
     town from the offline geocoder.
   - **Day out** = a run of not-at-home photos shorter than 20 hours: `YYYY-MM-DD Place`.
     (Trip and day out are the same rule; only the duration differs.)
   - **Occasion at home** = a burst at home well above your normal photos/day. Every such burst
     is proposed and goes to `_unnamed/` until you name it (`2026-06-30 Hannas Geburtstag`) or
     reject it (then it stays everyday).
   - Everything else → `YYYY/MM/`.
3. **Review** in the web UI: approve, reject, rename, toggle single photos, name unnamed
   bursts, undo whole clusters, move single photos back out. **Everyday** lists every photo
   that is in no cluster, by month and day; tick photos to add them to a pending proposal or
   to create a cluster by hand (kept across runs, marked "by hand"). Every action is logged and every
   folder gets a `manifest.json` (source paths, confidences, corrections). Approving in the
   UI settles every photo; only auto-applied clusters park their low-confidence photos in
   `<folder>/_review/` for a later look.

**Dry-run is on by default.** Nothing moves until you approve it or switch a rule to auto-apply.

## Deploy

```bash
git clone … photosort && cd photosort
cp .env.example .env          # set PUID/PGID (the NAS user) and the three mount paths
docker compose pull && docker compose up -d      # image from ghcr.io/philippmundhenk/photosort
# or build locally:  docker compose up -d --build
```

Open `http://<host>:8080`, go to **Settings**, set home lat/lon, check the input folders, save.
Press **Run now**. The first scan runs `exiftool` over every photo once (a few minutes for
20k files); afterwards only new files are read.

- Photos are mounted, not copied. Moves are `rename()` on the same mount; keep inbox and
  target on the same share. **Copy instead of move** (Settings → Behaviour) copies into the
  sorted tree and leaves the originals in the inbox; their sidecar records `copied_to`, so they
  are not proposed again. Undo and "move out" then delete the copy and clear that mark.
- Config lives in `data/config.yaml`, decisions in `data/events.jsonl`, current proposals in
  `data/proposals.json`. Losing `data/` loses history and pending proposals, never photos.
- Behind Traefik/Authelia: uncomment the labels in `docker-compose.yml`. There is no
  built-in auth.
- Thumbnails come from the Synology `@eaDir` folders (pattern configurable); the T430 never
  decodes an image. Without thumbnails the UI shows a placeholder (small JPEGs are served
  directly).

### Input folders

`Settings → Input folders`, one per line, `name=/path`. Each is scanned recursively. With
**Per-source subfolders** on, a trip folder gets one subfolder per input
(`2026-10 Lisbon/phone-a/`, `2026-10 Lisbon/phone-b/`); the everyday tree does the same.

### Home occasions

A burst at home is proposed when it has at least `burst_min_photos` files and exceeds your
median photos/day by `burst_baseline_factor`. Nothing judges whether it *was* an occasion:
metadata cannot tell a birthday from twenty shots of the same thing, and neither could the
decision model that was tried for exactly this (DESIGN.md, section 10). You name it or reject it.

## Layout

```
app/
  config.py    dataclass + YAML load/save + form coercion
  geo.py       haversine, zones, offline reverse geocoding (reverse-geocode)
  ingest.py    exiftool → sidecar; optional .xmp keywords
  cluster.py   the rules; proposals with review status
  mover.py     apply / undo / rename / move-out; manifest.json; cluster listing
  main.py      FastAPI app, scheduler, routes
  templates/   dashboard, review, clusters, cluster, log, settings
tests/           pytest suites (see Development & tests); tests/synth.py builds the synthetic library
Dockerfile       stages: base -> test (ruff, pytest, Playwright) -> runtime (default)
```

## Development & tests

Everything runs in Docker; nothing but Docker is needed on the host or the CI runner.

```bash
docker compose -f docker-compose.test.yml run --rm tests                 # ruff + unit + web + browser tests
docker compose -f docker-compose.test.yml run --rm tests pytest -m e2e -v
```

| Suite | Where | What |
|---|---|---|
| unit | `tests/test_{geo,config,ingest,events,cluster,mover,video,thumbs,manual,sidecars}.py` | rules, coercion, records, moving/copying, videos, thumbnails, manual clusters |
| web e2e | `tests/test_app.py` | every page and form action through FastAPI's TestClient |
| browser e2e | `tests/e2e/test_browser.py` | real uvicorn + headless Chromium (Playwright): run, rename, toggle, approve, name, move out, undo, settings |
| smoke | `python -m tests.smoke` | one synthetic end-to-end run without pytest |

Locally without Docker (needs exiftool and `playwright install chromium`):

```bash
pip install -r requirements-dev.txt
pytest                                    # -m "not e2e" to skip the browser tests
PHOTOSORT_DATA=./data uvicorn app.main:app --reload --port 8080
```

CI (`.github/workflows/ci.yml`) runs the test image on every push and PR, then builds
`photosort` (amd64+arm64) and publishes it to GHCR on `main` and on `v*` tags.

## Status / roadmap

- [x] multi-inbox, recursive, per-source subfolders
- [x] trips, day outs, home bursts, everyday tree
- [x] review UI, undo, corrections, Everyday page with manual clusters
- [x] decision model (Laya, System One API) tried on the home-burst question and dropped (DESIGN.md §10)
- [ ] Matrix notifier on top of the review queue ("3 reviews waiting" + link)
- [ ] optional CLIP pass for leftovers inside home bursts (Immich vectors)
