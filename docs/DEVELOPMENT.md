# Development

Everything runs in Docker; nothing but Docker is needed on the host or the CI runner.

## Code layout

```
app/
  config.py      settings dataclass, YAML load/save, form coercion, per-device options
  rules.py       the clustering rules as data: defaults from the settings, your own YAML, the why panel
  geo.py         distances, zones, offline reverse geocoding, home detection, great-circle interpolation
  ingest.py      exiftool -> one record per photo; scan, re-zoning, change tracking
  cluster.py     the record cache, positions for photos without GPS, excursions, bursts, names, proposals
  mover.py       apply / undo / rename / move out / put back; manifest.json; the cluster list
  journal.py     one line per file operation, grouped by action; revert
  events.py      the event log (Log page)
  thumbs.py      thumbnails: the NAS's own, else generated in a helper process
  main.py        FastAPI app, scheduler, the worker for moves and folder jobs, pages and actions
  templates/     the pages; static/ui.js and style.css (conventions at its top)
tests/           pytest suites (below); tests/synth.py builds the synthetic library
tools/           screenshots.py: the documentation screenshots
Dockerfile       stages: base -> test (ruff, pytest, Playwright) -> runtime (default)
constraints.txt  the exact package versions the images install
```

The reasons behind the rules and the code: [DESIGN.md](DESIGN.md).

## Tests

```bash
docker compose -f docker-compose.test.yml build tests
docker compose -f docker-compose.test.yml run --rm tests                 # ruff + every test + coverage gate
docker compose -f docker-compose.test.yml run --rm tests pytest -q tests/test_cluster.py   # one file
docker compose -f docker-compose.test.yml run --rm tests pytest -m e2e -v                  # browser tests only
```

The default command fails below **99 % line and branch coverage** of `app/`.

| Suite | Where | What |
|---|---|---|
| unit | `tests/test_{geo,config,ingest,events,cluster,devices,mover,video,thumbs,manual,sidecars,perf_paths,edges}.py` | rules, several devices, coercion, records, moving and copying, videos, thumbnails (helper process), manual clusters, caches and hot paths, every error path |
| rules | `tests/test_rules.py` | the rule engine: a golden test against the frozen pre-engine rules (`tests/legacy_rules.py`) on every library and many parameter sets, custom rules, validation, the preview, multi-home zoning |
| why panel | `tests/test_why.py` | what a proposal stores about the rule that made it, every condition in plain words, the panel on the review and cluster pages |
| journal | `tests/test_journal.py` | every action is one batch of file operations; revert of a batch, of one file, of a revert; skipped files reported; dry-run refused; the History page, the originals left by copy mode |
| jobs | `tests/test_jobs.py` | undo, rename and put back queued for the worker, busy folders, dry-run switched on while they wait |
| web | `tests/test_app.py`, `tests/test_live_pages.py`, `tests/test_moving_matrix.py`, `tests/test_templates.py` | every page and form action through FastAPI's TestClient; a run finishing mid-review; every way a file can move against dry-run, live with switches off and each switch; every template branch |
| soak | `tests/test_soak.py` | thirty run-approve-undo cycles: no memory growth, nothing left in queues or threads |
| fuzz | `tests/test_fuzz.py` | a thousand random tag sets, timestamps, offsets, user texts and timelines through every parser and the clustering: never a crash |
| latency | `tests/test_perf_features.py` | every feature timed on a 20 500-photo library; its own CI step without the coverage tracer; `PERF_BASELINE=1` prints without failing |
| scale | `tests/test_perf_scale.py` | 20 000 records clustered, 5 000 loaded from disk, a 3 000-photo move with the pages timed while it runs |
| browser | `tests/e2e/test_browser.py`, `tests/e2e/test_smooth.py`, `tests/e2e/test_features.py`, `tests/e2e/test_scenarios.py`, `tests/e2e/test_restart.py` | real uvicorn and headless Chromium (Playwright): the review flow, no reload while the user works, every feature end to end, copy mode, two phones on two continents, a kill and restart in the middle of a 2 500-file move |
| browser memory | `tests/e2e/test_memory.py` | no growth of JS heap, DOM nodes or event listeners while a page polls or the same clicks repeat (measured after garbage collection) |
| script coverage | `tests/e2e/test_script_coverage.py` | one browser session runs every function of `ui.js` (99 % of its code or more) |
| docs | `tests/test_docs.py` | this table matches the files, every setting is documented and has a form control, links and images in the docs exist, the design document names every module |
| smoke | `python -m tests.smoke` | one synthetic end-to-end run without pytest |

Locally without Docker (needs exiftool, and `playwright install chromium` for the browser tests):

```bash
pip install -r requirements-dev.txt -c constraints.txt   # the exact versions CI tests
pytest                                                   # -m "not e2e" skips the browser tests
PHOTOSORT_DATA=./data uvicorn app.main:app --reload --port 8080
```

## CI and releases

`.github/workflows/ci.yml` runs on every push and pull request: the test image (ruff, every
test, the coverage gate), the smoke script, the latency harness, the compose files, then it
builds the runtime image, starts it and calls every page. On `main` and on `v*` tags it
publishes `ghcr.io/philippmundhenk/photosort` (amd64 and arm64).

Package versions are pinned in `constraints.txt`; Dependabot proposes one bump per pull request,
so a version that breaks something shows up on its own.

## Screenshots

The screenshots in the README and these docs come from the real UI on the synthetic library,
with generated pictures instead of the placeholder photos:

```bash
docker compose -f docker-compose.test.yml build tests
docker run --rm -v "$PWD/tools:/srv/tools:ro" -v "$PWD/docs/screenshots:/out" photosort-test:local \
  python -m tools.screenshots /out
```

Their places are generic (home in Munich, a day out to Starnberg): no real places in pictures.
