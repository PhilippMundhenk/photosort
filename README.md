# photosort

[![CI](https://github.com/PhilippMundhenk/photosort/actions/workflows/ci.yml/badge.svg)](https://github.com/PhilippMundhenk/photosort/actions/workflows/ci.yml)

Sorts incoming photos from phones and cameras into a folder tree by **trip**, **day out** and
**occasion at home**, fully locally, on weak hardware (built for a CPU-only ThinkPad T430).
The filesystem is the only state; a "System One" decision model (the bundled open-weights
[Laya](https://huggingface.co/convaiinnovations/laya), or any server speaking TypeSafe's Jev
API such as [Kev](https://github.com/arjun988/Kev)) is used only for the judgment calls rules
cannot make. A web UI reviews every move.

See [docs/DESIGN.md](docs/DESIGN.md) for scope, the rules and why they were chosen.

## What it does

```
inbox/phone-a/**.jpg ─┐                              sorted/2026-06-04..24 Lisbon, Sevilla/
inbox/phone-b/**.jpg ─┼─ scan ─ zone ─ cluster ─►    sorted/2026-06-27 Ludwigsburg/
inbox/camera/**.dng  ─┘                              sorted/_unnamed/2026-06-30 (25 Fotos)/   ← you name it
                                                     sorted/2026/06/                          ← everything else
```

1. **Scan** (every N minutes): every photo in every input folder (recursive) gets a JSON
   sidecar `<file>.photosort.json` with timestamp, GPS, offline reverse-geocoded place and its
   *zone* relative to your home: `home` (< 0.5 km), `local` (< 20 km), `away` (beyond).
   Photos without GPS take the position of the nearest photo in time that has one.
2. **Cluster** (deterministic):
   - **Trip** = a run of `away` photos. Any `home` photo ends it, no matter how short the stay.
     Local/no-GPS photos inside the run ride along. Lisbon → Seville → Lisbon is one trip.
     Name: `YYYY-MM-DD..DD City, City` (cities in order of first appearance; countries if more
     than four; "Multiple" beyond that).
   - **Day out** = a burst of `local` photos: `YYYY-MM-DD Place`.
   - **Occasion at home** = a burst at home well above your normal photos/day. Here the
     decision model is asked `occasion / busy day`. Occasions go to `_unnamed/` until you name
     them (`2026-06-30 Hannas Geburtstag`); busy days stay everyday.
   - Everything else → `YYYY/MM/`.
3. **Review** in the web UI: approve, reject, rename, toggle single photos, name unnamed
   bursts, undo whole clusters, move single photos back out. Every action is logged and every
   folder gets a `manifest.json` (source paths, confidences, corrections). Approving in the
   UI settles every photo; only auto-applied clusters park their low-confidence photos in
   `<folder>/_review/` for a later look.

**Dry-run is on by default.** Nothing moves until you approve it or switch a rule to auto-apply.

## Deploy

```bash
git clone … photosort && cd photosort
cp .env.example .env          # set PUID/PGID (the NAS user) and the three mount paths
docker compose pull && docker compose up -d      # images from ghcr.io/philippmundhenk/{photosort,photosort-laya}
# or build locally:  docker compose up -d --build
```

The first Laya start downloads 846 MB of weights into `./kev-models`; the dashboard shows the
decision backend as *down* until the model is loaded.

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

### Decision model

`docker-compose.yml` ships **Laya** (Convai Innovations, Apache-2.0, 421M parameters,
non-autoregressive, ~200-500 ms per question on a laptop CPU, ~2.2 GB RAM) behind
[laya-server](https://github.com/nvkudva/laya-server), which implements TypeSafe's System One
API. `docker/laya/Dockerfile` pins the server commit; the checkpoint is cached in
`./kev-models`. `http://localhost:8000/demo` is the server's own playground.

The app speaks the shared wire format, so any other System One server works unchanged:

```
POST <kev_url>/v1/systemone
{"state": "photos: 25\nduration_h: 3.6\n...", "model": "laya",
 "questions": {"q": {"type": "choice",
                     "instructions": "Is this a special occasion or an ordinary day ...?",
                     "criteria": {"occasion": "birthday, party, visitors ...",
                                  "busy_day": "documenting things, kids playing ..."}}}}
-> {"answers": {"q": {"choice": "occasion",
                      "probabilities": {"occasion": 0.83, "busy_day": 0.17}, "confidence": 0.71}}}
```

- `kev_url` (Settings, seeded from `PHOTOSORT_KEV_URL`) is the server's base URL. Empty
  → rule-based fallback (home bursts flagged by size and device count), also used whenever
  the server errors; such decisions are logged with `fallback_from: kev`.
- `kev_model` sets the request's `model` field (`laya`, `laya-multilingual`, `kev-latest`, ...);
  empty uses the server default.
- Alternatives: [Kev](https://github.com/arjun988/Kev) (`npm i -g @kev-ai/server`, needs
  Ollama + a Qwen model, too heavy for the T430), [jaredpalmer/kev](https://github.com/jaredpalmer/kev)
  (GPU/Apple Silicon), [SemIf](https://github.com/TheoLeeCJ/SemIf-OpenJev) (GPU), or Jev
  itself at `https://api.typesafe.ai` (cloud, against the project's local-only goal).
  Curated lists: [awesome-decision-models](https://github.com/sfmqrb/awesome-decision-models).
- The question, its instructions and the option descriptions live in `app/cluster.py`
  (`HOME_BURST_*`); the transport in `app/kev.py`. `python -m tests.test_kev` checks the
  adapter against a fake server.

Every model decision is logged with its confidence. **Log → Calibration** compares confidence
buckets with your later corrections (rejects, renames of the answer, photos moved out) — a
calibrated model shows accuracy ≈ confidence per row.

## Layout

```
app/
  config.py    dataclass + YAML load/save + form coercion
  geo.py       haversine, zones, offline reverse geocoding (reverse-geocode)
  ingest.py    exiftool → sidecar; optional .xmp keywords
  cluster.py   the rules; proposals with review status
  kev.py       decision backends (rule fallback, System One HTTP) and the logging facade
  mover.py     apply / undo / rename / move-out; manifest.json; cluster listing
  main.py      FastAPI app, scheduler, routes
  templates/   dashboard, review, clusters, cluster, log, settings
tests/           pytest suites (see Development & tests); tests/synth.py builds the synthetic library
docker/laya/     Dockerfile for the Laya decision-model container
Dockerfile       stages: base -> test (ruff, pytest, Playwright) -> runtime (default)
```

## Development & tests

Everything runs in Docker; nothing but Docker is needed on the host or the CI runner.

```bash
docker compose -f docker-compose.test.yml run --rm tests                 # ruff + unit + web + browser tests
docker compose -f docker-compose.test.yml run --rm tests pytest -m e2e -v
docker compose -f docker-compose.test.yml --profile integration run --rm laya-tests   # against the real model
```

| Suite | Where | What |
|---|---|---|
| unit | `tests/test_{geo,config,ingest,events,cluster,mover,kev}.py` | rules, coercion, sidecars, moving/copying, the System One adapter against a fake server |
| web e2e | `tests/test_app.py` | every page and form action through FastAPI's TestClient |
| browser e2e | `tests/e2e/test_browser.py` | real uvicorn + headless Chromium (Playwright): run, rename, toggle, approve, name, move out, undo, settings |
| integration | `tests/test_laya_integration.py` | the adapter against a live Laya container (`-m integration`) |
| smoke | `python -m tests.smoke` | one synthetic end-to-end run without pytest |

Locally without Docker (needs exiftool and `playwright install chromium`):

```bash
pip install -r requirements-dev.txt
pytest                                    # -m "not e2e" to skip the browser tests
PHOTOSORT_DATA=./data uvicorn app.main:app --reload --port 8080
```

CI (`.github/workflows/ci.yml`) runs the test image on every push and PR, then builds
`photosort` (amd64+arm64) and `photosort-laya` (amd64) and publishes them to GHCR on `main`
and on `v*` tags. The Laya integration job runs on tags or by hand (workflow_dispatch).

## Status / roadmap

- [x] multi-inbox, recursive, per-source subfolders
- [x] trips, day outs, home bursts, everyday tree
- [x] review UI, undo, corrections, calibration view
- [x] System One wire format (`/v1/systemone`) verified against a real server (Laya via laya-server)
- [ ] calibration data: enough reviewed home bursts to compare Laya's confidence with corrections
- [ ] Matrix notifier on top of the review queue ("3 reviews waiting" + link)
- [ ] optional CLIP pass for leftovers inside home bursts (Immich vectors)
