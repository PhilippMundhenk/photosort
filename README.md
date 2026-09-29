# photosort

[![CI](https://github.com/PhilippMundhenk/photosort/actions/workflows/ci.yml/badge.svg)](https://github.com/PhilippMundhenk/photosort/actions/workflows/ci.yml)

Sorts the photos and videos from your phones into a folder tree of trips, day outs and
occasions, by rules alone, fully offline, on weak hardware, with a web UI that reviews every move.

## What it does

```
inbox/phone-a/**.jpg ─┐                          sorted/2026-06 Lisbon, Sevilla/
inbox/phone-b/**.jpg ─┼─ scan ─ zone ─ cluster ─► sorted/2026-06-27 Ludwigsburg/
inbox/camera/**.dng  ─┘                          sorted/_unnamed/2026-06-30 (25 Fotos)/ ← name it
                                                 sorted/2026/06/                        ← the rest
```

## Key features

- **Rules, not a database.** Where a photo was taken relative to home and when decides
  everything; the folder tree is the result and other tools keep working on it.
- **Trips, day outs and occasions from plain observation.** A run of photos away from home is a
  trip if it spans a night and a day out otherwise; a dense burst at home is an occasion for you
  to name. No dates or hints to type in.
- **Review before anything moves.** Dry-run is the default and blocks every move, copy and
  delete; approvals wait until you switch it off. Approve, rename, exclude single photos, name
  bursts, undo whole folders; the Everyday page shows what was not clustered and lets you build
  clusters by hand.
- **Every move is on record and can be taken back.** A journal keeps one line per file operation,
  grouped by action; the History page reverts any action, or puts one file back where it was
  before a given action, as far as the files are still where that action left them.
- **Two phones, one library.** Each device is followed on its own timeline: the family trip
  with two phones is one folder, the partner's photos at home never cut it short, and two
  phones on different continents are two trips.
- **Videos included.** iPhone and Android MP4/MOV get correct local timestamps (UTC converted
  to your home zone), GPS from the QuickTime metadata and a thumbnail frame.
- **Readable names.** `2026-06 Lisbon, Sevilla`; your own place names ("Black Forest") beat
  the offline geocoder and are learned when you rename a cluster.
- **Thumbnails without a NAS.** Generated once and cached; RAW previews and video frames too.
- **Move or copy.** Moves are renames on the same share; copy mode leaves originals untouched.
- **One small container.** Runs on a CPU-only laptop; the whole test suite runs in Docker.

See [docs/DESIGN.md](docs/DESIGN.md) for the rules and why they were chosen.

## Deploy

```bash
git clone … photosort && cd photosort
cp .env.example .env          # set PUID/PGID (the NAS user), the inbox root and the target root
docker compose pull && docker compose up -d      # image from ghcr.io/philippmundhenk/photosort
# or build locally:  docker compose up -d --build
```

Open `http://<host>:8080`. A first run starts by itself; when the home location is not set
it is detected from the photos (the spot with photos on the most different days) and can be
corrected in **Settings**. Press **Run now** any time. The first scan runs `exiftool` over every photo once (a few minutes for
20k files); afterwards only new files are read.

- Photos are mounted, not copied. Moves are `rename()` on the same mount; keep inbox and
  target on the same share. **Copy instead of move** (Settings → Behaviour) copies into the
  sorted tree and leaves the originals in the inbox; their sidecar records `copied_to`, so they
  are not proposed again. Undo and "move out" then delete the copy and clear that mark.
- Config lives in `data/config.yaml`, the event log in `data/events.jsonl`, current proposals in
  `data/proposals.json`. Losing `data/` loses history and pending proposals, never photos.
- Behind Traefik/Authelia: uncomment the labels in `docker-compose.yml`. There is no
  built-in auth.
- Thumbnails come from the Synology `@eaDir` folders (pattern configurable); the T430 never
  decodes an image. Without thumbnails the UI shows a placeholder (small JPEGs are served
  directly).

### Input folders

Mount the folder that holds `inbox/` and `sorted/` once, at `/photos` (`PHOTOS_BASE` in `.env`):
a move is then a rename on the share. Two separate mounts, as earlier versions used, make every
move a copy through the container and back over the network (the pages warn when they see it).
The inbox root is `/photos/inbox`, the sorted root `/photos/sorted`; both are editable in
Settings for other layouts. Every subfolder of the inbox root is an
input named after the folder: `hans/` becomes `hans=/photos/inbox/hans`, discovered on every
scan, so a new device folder needs no configuration. Settings shows what was found; the
override box takes explicit `name=/path` lines when the layout is different. Each input is
scanned recursively. With **Per-source subfolders** on, a trip folder gets one subfolder per
input (`2026-10 Lisbon/hans/`, `2026-10 Lisbon/phone-b/`); the everyday tree does the same.

### Home occasions

A burst at home is proposed when it has at least `burst_min_photos` files and exceeds your
median photos/day by `burst_baseline_factor`. Nothing judges whether it *was* an occasion:
metadata cannot tell a birthday from twenty shots of the same thing, and neither could the
decision model that was tried for exactly this (DESIGN.md, section 10). You name it or reject it.

## How it works

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
     Lisbon → Seville → Lisbon is one trip. Name: `YYYY-MM Place, Place` when the trip stays
     within one month, `YYYY-MM-DD..MM-DD` otherwise (places in order of first appearance;
     countries if more than four; "Multiple" beyond that). Places
     are your own named places (Settings, or "remember this place" when renaming), else the
     town from the offline geocoder.
   - **Day out** = a run of not-at-home photos shorter than 20 hours: `YYYY-MM-DD Place`.
     (Trip and day out are the same rule; only the duration differs.)
   - **Occasion at home** = a burst at home well above your normal photos/day. Every such burst
     is proposed and goes to `_unnamed/` until you name it (`2026-06-30 Hannas Geburtstag`) or
     reject it (then it stays everyday).
   - Everything else is *everyday*: shown on the Everyday page; moved into `YYYY/MM/` by the
     button there once older than `everyday_keep_days` (4), or on every run with *Auto-move everyday*.
3. **Review** in the web UI: approve, reject, rename, toggle single photos, name unnamed
   bursts, undo whole clusters, move single photos back out. **Everyday** lists every photo
   that is in no cluster, by month and day; tick photos to add them to a pending proposal or
   to create a cluster by hand (kept across runs, marked "by hand"). Every action is logged and every
   folder gets a `manifest.json` (source paths, confidences, corrections). Approving in the
   UI settles every photo; only auto-applied clusters park their low-confidence photos in
   `<folder>/_review/` for a later look.

**Dry-run is on by default, and it means exactly that: nothing is moved, copied or deleted.**
Approving only marks proposals; they are moved once you switch dry-run off in Settings (which
asks for confirmation). Every function that touches a file checks the flag itself.

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
| unit | `tests/test_{geo,config,ingest,events,cluster,devices,mover,video,thumbs,manual,sidecars,perf_paths,edges}.py` | rules, two devices, coercion, records, moving/copying, videos, thumbnails (helper process), manual clusters, caches and hot paths, every error path |
| web e2e | `tests/test_app.py`, `tests/test_live_pages.py`, `tests/test_moving_matrix.py`, `tests/test_templates.py` | every page and form action through FastAPI's TestClient; batched clicks, a run finishing mid-review; every way a file can move against dry-run, live-with-switches-off and each switch |
| soak | `tests/test_soak.py` | thirty run-approve-undo cycles: no memory growth, nothing left in queues or threads |
| fuzz | `tests/test_fuzz.py` | a thousand random tag sets, timestamps, offsets, user texts and timelines through every parser and the clustering: never a crash, always well-formed output (it found five defects on its first run) |
| latency | `tests/test_perf_features.py` | every feature timed on a 20 500-photo library (pages, polls, clicks, approvals, moves of thousands of files, undo, settings); its own CI step without the coverage tracer; `PERF_BASELINE=1` prints without failing |
| scale | `tests/test_perf_scale.py` | 20 000 records clustered, 5 000 records loaded from disk, a 3 000-photo proposal through the web layer; latencies printed with `pytest -s`, generous bounds |
| browser e2e | `tests/e2e/test_browser.py`, `tests/e2e/test_smooth.py`, `tests/e2e/test_features.py`, `tests/e2e/test_scenarios.py`, `tests/e2e/test_restart.py` | real uvicorn + headless Chromium (Playwright): the review flow; no reload while the user works, hidden-tab polling, a 4000-photo library; every feature end to end (everyday selection and manual clusters, keyboard, viewer, remembered places, approve all, the everyday move button, settings dialogs, home detection, sidecar buttons, log filters, month navigation, dashboard, phone width); copy mode, two phones on different continents, wall-clock times in the home zone and an own place name from Settings through the whole pipeline; a kill and restart in the middle of a 2 500-file move |
| smoke | `python -m tests.smoke` | one synthetic end-to-end run without pytest |
| journal | `tests/test_journal.py` | every action is one batch of file operations; revert of a batch, of one file, of a revert; skipped files reported; dry-run refused; the History page |
| docs | `tests/test_docs.py` | the README's test table matches the files on disk, every configuration field has a form control, the design document names every module |
| coverage | the test image's default command | line and branch coverage of `app/` must stay at 99 % or above (it is 100 % as of 2026-09-29); `tests/e2e/test_script_coverage.py` measures how much of `ui.js` one browser session executes (99 % or above; every function runs); `tests/test_templates.py` renders every template branch; CI also starts the published runtime image and calls every page |

Locally without Docker (needs exiftool and `playwright install chromium`):

```bash
pip install -r requirements-dev.txt
pytest                                    # -m "not e2e" to skip the browser tests
PHOTOSORT_DATA=./data uvicorn app.main:app --reload --port 8080
```

CI (`.github/workflows/ci.yml`) runs the test image on every push and PR, then builds
`photosort` (amd64+arm64) and publishes it to GHCR on `main` and on `v*` tags.

## Status / roadmap

Done: inputs discovered per device folder; trips, day outs, home bursts, everyday tree; review
UI with undo, corrections, Everyday page and manual clusters; videos, thumbnails, named places;
dry-run that blocks every file operation; background moves with progress. A decision model
(Laya, System One API) was tried on the home-burst question and dropped (DESIGN.md §10).

Open work is tracked as [issues](https://github.com/PhilippMundhenk/photosort/issues), among
them a generic rule engine (#2), multi-home (#3), background undo (#4), naming of transit
stops and city districts (#5), a per-proposal "why" panel (#6), large-library performance (#7),
a Matrix notifier (#8), content-based signals for home bursts (#9) and multi-user support (#1).
