# Design notes

Written from the design conversation that produced this project (September 2026). The aim
is that someone (including future-you) can see *why* the pipeline looks the way it does and
which alternatives were rejected.

## 1. Purpose and scope

Goal: automatically sort photos from several devices into a folder tree by trip / day out /
occasion, with names like `2026-10-03..24 Lisbon, Sevilla`, and get hands-on experience with
"System One" decision models (TypeSafe's Jev and its open reproductions Kev, Laya, SemIf) on a
task where they add real value rather than as a toy.

Non-goals:

- No database-driven photo manager (Immich, Paperless-style). The folder tree *is* the
  product; other tools must keep working on it.
- No image content analysis on the server. The target machine is a CPU-only ThinkPad T430
  that already OOM-kills a 7B LLM; it must never decode an image.
- No cloud or API models. Everything local.

## 2. Why a decision model at all — and why so little of it

*(Superseded: the model was dropped after the experiment in section 10. The reasoning below
is kept because it explains the shape of the rules.)*

Jev-style models take a short text *state* and typed questions (`Choice`, `Noul` = yes/no,
`Score`) and return calibrated probabilities in ~100 ms, without generating text. They are
small (Laya ≈ 1 GB, sub-second on CPU) and cheap to call thousands of times. They are weak on
long, noisy input with many fuzzy categories — which is exactly what the *first* idea
(sorting OCR'd scans into a large folder tree) would have been. So the photo task was chosen
instead: short states, few options, thousands of calls, harmless failures, free labels.

During design the model's share kept shrinking, deliberately. Every time a rule could
express what we wanted, the rule won: rules are debuggable, need no calibration and run in
microseconds. The model is reserved for genuine judgment calls the rules cannot see. Today
that is one question — *is this dense burst of photos at home an occasion or just a busy
day?* — plus room for two more (photo-less gaps, fringe photos) if the rules prove
insufficient. This is intentional: a first project where the model is load-bearing would be
a worse project.

## 3. The rules, and the decisions behind them

### Zones instead of "holiday detection"

Three zones by distance from a configured home point: **home** (< 0.5 km), **local**
(< 20 km), **away**. The home point can be set from the data ("Detect home from photos" in
Settings: the ~100 m cell with photos on the most distinct days), which the first real run
showed to be necessary: a home point 2.6 km off turned every day into a day out and no trip
ever ended. Earlier ideas — a static "holiday or not" filter, DBSCAN over
time+space, a window around user-given dates — were dropped because trips vary from one day
to four weeks and the user does not want to type exact dates. Distance from home is the one
feature that is always available and always meaningful.

### Excursion = maximal run of photos not at home, ended by any `home` photo (revised September 2026)

After the first run on real photos the distance-based split (day out = local burst, trip =
away run) produced a 13-photo "trip" to a university 25 km away and would have merged a
one-day outing 30 km away with a two-week holiday. The user's observation: what makes a trip
is *staying away overnight without photos at home in between*, not the distance. So both
kinds now come from one rule: a run of photos outside the home radius, ended by any home
photo, is a **trip** when it spans at least `trip_min_hours` (20 h, i.e. an overnight stay)
and a **day out** otherwise. Day outs near home need `dayout_min_photos` (8) so the school run
and the supermarket stay everyday; day outs mostly in the away zone, like trips, need only
`trip_min_photos` (3): three photos 30 km from home are an outing, three photos in the next
town are noise. The local/away
zone remains as a label and for confidences only. The earlier notes below describe the run
mechanics, which are unchanged.

### One timeline per device (September 2026)

With two phones in the inbox the single merged timeline broke in both directions: a photo
taken at home with one phone ended the other phone's trip (the trip fell apart into day-long
fragments, none long enough to be a trip), and two phones 10 000 km apart at the same time
produced one "trip" with both places in its name, because the gap-and-distance split only
looks at consecutive photos and the gaps between interleaved photos are minutes. Excursions
are therefore found per device (`source`, the inbox folder), and runs of different devices
become one excursion only when they overlap in time and some of their photos are within a
day and `trip_split_distance_km` of each other: the family trip with two phones stays one
folder, the partner at home stays out of it. The same rule applies to GPS-less photos: they
borrow a position from the nearest located photo *of the same device*; only a device that
never records GPS at all borrows from the others. Home bursts still look at the merged
timeline, since a birthday photographed with two phones is one occasion.

### Trip runs: mechanics

- Returning home ends a trip *no matter how short the stay*. This was an explicit
  requirement and removes all place-based adjacency logic.
- Lisbon → Seville → Lisbon is one trip, cross-country, flat (no nested "leg" folders —
  nesting was proposed and rejected).
- Local / no-GPS photos inside a run ride along with lower confidence (transit, an airport
  photo). They are not needed to define the trip; they must not break it either.
- A photo-less gap splits a run only if it is long (`trip_gap_days`) **and** the two sides are
  far apart. "Same area → same trip" was the user's rule for gaps; a model question was
  considered and dropped. Near home (local zone) the assumption is reversed: you sleep at
  home, so a gap longer than `local_gap_hours` (12 h) ends the outing even when no photo was
  taken at home in between (added after two evenings two days apart, and a day out plus a
  video the next morning, were merged into one "trip").
- Photos without GPS inherit the nearest GPS'd photo in time (within 48 h). Also the user's
  rule; simpler than asking the model.
- A run whose last photo is recent is marked *ongoing* (the home photo may not have synced
  yet) and is never applied automatically.

### Naming

`<span> <places>`. The span is the month (`2026-08`) when a multi-day cluster stays within one
month (user request, September 2026: the day range added nothing once the folder is inside a
year tree), the day for a single day, and a full range only across months. Places in order of
first appearance, deduplicated, at most four; then
countries; then "Multiple". Places mentioned by fewer than 3 % of a run's photos (and fewer
than two) are dropped, so a motorway stop does not name the trip. The place comes from:

1. the user's **named places** (`named_places`: name, centre, radius), because nothing
   offline knows that a spot is "Black Forest", "the Alps" or "the allotment garden". The list is
   grown from the UI: tick "remember this place" when renaming a cluster and the cluster's
   photos define the circle (mean position, radius to the farthest photo).
2. else the offline geocoder's town when its population is at least `min_city_population`
   (1000 since September 2026; 20000 produced state names for most of Germany), else the
   region.

### Why "trip" and "day out" are separate rules although the output is the same

Asked in September 2026: a trip folder and a day-out folder look alike (`root/<span> <place>`),
so is the distinction worth anything? In the *output* it is not, and the code treats both the
same there (same target folder, same manifest, same review UI). It is kept because the two
zones need opposite evidence rules, and no single threshold set serves both:

- **Trip = run logic, sparse-tolerant.** Any run of `away` photos between two `home` photos,
  at least 3 photos, no density requirement; photo-less days do not end it. One beach photo a
  day is still the trip. Applied within 20 km of home this would turn the school run, the
  supermarket and the grandparents in the next town into "trips" whenever three photos fall
  between two home photos, and merge errands on consecutive days.
- **Day out = burst logic, density-based.** At least `burst_min_photos` (12) with no gap over
  `burst_gap_hours` (3). This filters everyday local noise, but would destroy trips: a
  two-week trip with one photo a day never forms a burst.
- Distance from home is therefore a proxy for *how much everyday background noise the zone
  has*: far away almost everything is signal, nearby only a dense burst is.

Other things that hang off the distinction: per-kind auto-apply trust (trips first), the
`ongoing` status for trips (the home photo may not have synced yet), local/no-GPS photos
riding along inside an away run as transit, and naming (date range + up to four places vs.
one date + the most common place).

Possible simplification, not done: one "excursion" rule using the run algorithm for both
zones with zone-dependent thresholds (away: 3 photos, split on a 4-day/300 km gap; local: 12
photos, split on a 3 h gap). `kind` would survive as metadata for trust and calibration. It
would change one behaviour: today a local burst is decided by *majority* zone, so a burst that
starts with a few home photos and continues at the lake is one day out including the home
photos; under the run rule the home photos would end it. Probably an improvement, but a
behaviour change that needs its own test before switching.

### Day outs and home occasions = bursts

Photos closer together than `burst_gap_hours` form a burst; the majority zone decides the
kind. Local bursts are named from the geocoder. Home bursts have no location signal at all
— a birthday and the laundry look identical in metadata — so:

- the burst must exceed the user's baseline photos/day by a factor (occasions are dense);
- the decision model judges `occasion / busy_day` from the burst summary (count, duration,
  weekday, hours, number of devices, ratio to baseline);
- accepted bursts go to `_unnamed/` and the *user* names them later. Reading the calendar
  (CalDAV) to name them was proposed and rejected: keep the pipeline observational and ask
  the human.

Leftover non-occasion photos inside a burst are accepted as a small cost; an optional CLIP
pass (Immich already computes vectors) is the noted follow-up if it bothers.

## 3b. Code map

One package, `app/`, no framework beyond FastAPI and Jinja:

- `config.py`: the dataclass behind `config.yaml`, loaded per request from a content-hashed cache,
  coerced to field types, discovered inputs, time zone.
- `ingest.py`: exiftool in batches, one JSON record per photo (central index or beside the photo),
  timestamps and offsets, GPS sanity, the scan with its change tracking and re-zoning key.
- `cluster.py`: the record cache, per-device excursions and their merge, bursts, naming,
  proposals and their identity across runs, the proposals file and its lock.
- `mover.py`: every file operation (move, copy, undo, rename, move out, put back), the dry-run
  guard, manifests written incrementally, the cluster list and the cross-mount warning.
- `geo.py`: distance, zones, the offline geocoder with the covering-town rule, own places,
  home detection.
- `thumbs.py`: thumbnails and previews in a helper process, the cache and its failure markers,
  the paced prefetch.
- `events.py`: the append-only log and its tail reader.
- `journal.py`: the transaction journal, one line per file operation in batches (one per action),
  and revert of a batch or of one file (see section 4b).
- `main.py`: the pages and actions, the pipeline, the apply worker, the status endpoint.

## 4. State: filesystem, not a database

Explicit requirement: databases get lost and break. So:

- **One JSON record per photo**: timestamp, GPS, place, zone, cluster, decision.
  Regenerable — delete it and the next scan rebuilds it. Since September 2026 it lives by
  default in a central index (`data/index/<inbox>/<relative path>/<file>.photosort.json`)
  because the first real run doubled the file count in the photo folders; "beside the
  photo" remains an option (the record then follows the file wherever another tool moves
  it), the file name pattern is configurable, and once a photo has been sorted its record
  is dropped by default (the folder's `manifest.json` carries everything; copy-mode
  originals keep theirs, which marks them as done). Keywords are optionally mirrored into a
  standard `.xmp` sidecar for digiKam/Lightroom. Writing into the originals was rejected
  (no benefit, risky for RAW).
- **Manifest per cluster folder** (`manifest.json`): proposal, decision and confidence,
  source→target of every photo, later corrections. It is the undo log and the eval set.
- **The folder tree is the decision**: a photo in a trip folder is settled; `_unnamed/` holds
  bursts waiting for a label (each folder carries its date, `_unnamed/2026-06-30 (25 Fotos)/`,
  so several can wait at once). `_review/` inside a folder holds low-confidence photos of
  *auto-applied* clusters only: when a human approves a proposal in the UI, that *is* the
  review, and everything goes straight into the folder (decided September 2026, after the
  first version parked them in `_review/` in both cases).
- `data/` (config.yaml, proposals.json, events.jsonl) is the only non-photo state and only
  costs history if lost.

### Inputs are discovered

Input folders were fixed in the config (`phone-a=/photos/inbox/phone-a`, `phone-b=...`) and
thus had to be edited for every device. Since September 2026 the container mounts one inbox
root and every direct subfolder is an input named after the folder, re-read on each scan; the
config only holds an optional override. The folder name is the device name that appears in
per-source subfolders and in the records (`source`), so naming the folders after their owners
("hans", "maria") gives readable trees.

### When the share drops out

An inbox folder that is not there at scan time (the network share unmounted, the NAS down) is
reported on the dashboard and its records leave the cache for that run: nothing is proposed
from photos nobody can reach, so an approve-all during the outage cannot mark a trip "applied"
without moving a file. Should such an approval happen anyway, a move that finds none of its
files raises instead of succeeding emptily; the proposal stays approved with the error and the
retry works once the share is back. When it returns, the records are still in the index, the
same proposals come back with the same ids, and nothing is re-indexed.

### 4b. The transaction journal (September 2026)

The user's ask after a month of use: "How about transactional? To make sure we can always trace,
revert to previous state." The per-folder manifests recorded what a cluster contained, but the
everyday move had no manifest and could not be undone, a folder rename left no trace, and there
was no single place to ask where a file is now. `data/journal.jsonl` is that place: every file
operation passes through two primitives in `mover` (`_transfer`, `_delete_with_sidecars`) and
each writes one line (op, src, dst); every action (apply, everyday move, undo, rename, move out,
put back, revert) opens a batch, so a batch is one user or worker action. The History page lists
the batches with a Revert button and traces a file by name, offering to put it back where it was
before any one action. Revert moves back what is still where the batch left it and reports the
rest; it never guesses, never overwrites (a clash gets the `_1` suffix like any move), never
deletes an original (a deleted copy cannot be restored and says so). A revert is a batch itself
and can be reverted. Manifests stay as the per-folder view and follow a revert (an emptied folder
stops being a cluster; a reverted undo gets its manifest back). Losing the journal costs history,
never photos. Nothing prunes it.

## 5. Triggers

Originally: a Matrix message like "2026-10 Uruguay" seeds the trip, the model grows the edges.
Replaced by pure observation: new photos in the inbox are enough, because "beyond 20 km" plus
"ended by a home photo" defines a trip without any hint. A hand-created empty folder as a
rename hint is still possible but no longer needed.

Polling every N minutes instead of inotify, because inotify is unreliable over SMB/CIFS.

## 6. Review UI instead of chat

Matrix was the first choice for approvals and naming, then dropped for v1: chat is finicky,
items get lost, there is no queue. A web service with a review queue, a log and a settings
page is the durable form; a Matrix notifier ("3 reviews waiting" + link) can sit on top later
with the queue staying authoritative.

Dry-run is the default. Auto-apply is per rule (trips first, bursts later) so trust can be
built incrementally.

Dry-run semantics were tightened on 2026-09-26 after an incident: the first version moved
files when the user approved a proposal *in* dry-run mode ("nothing moves until you approve"),
and an "approve all" in dry-run then moved 1060 files. The user's rule: dry-run means nothing
is moved, copied or deleted, whatever is approved. It is enforced in one place, the two
functions in `mover` through which every file operation passes (`_transfer`,
`_delete_with_sidecars`), plus `apply`/`apply_everyday`/`undo`/`rename` themselves; they raise
`DryRun`. Approvals are only recorded; switching dry-run off (with a confirmation) queues them.
An invariant test drives every UI action with dry-run on and asserts the inbox is untouched.

The page never reloads itself (September 2026). The first version reloaded every page when a
scan or a move finished, "for fresh counts"; with a scan every ten minutes on a large library
that meant losing ticked photos, a half-typed name and the open viewer several times an hour,
and each reload re-fetched hundreds of thumbnails. Now one poll per page updates the busy
banner and the move progress in place and, when a run finished or files were moved, offers a
reload in the banner; only the pages without user state (dashboard, log, clusters) refresh
on their own. Clicks on photos flip at once and travel batched, one request in flight per
proposal, because a page full of lazily loading thumbnails otherwise queues every click
behind the browser's six connections per host.

A proposal keeps its identity across runs. Its id is a hash of first and last photo, so a
photo that synced late and landed at either end gave the same excursion a new id and the
review state (toggled photos, an edited name, an approval) was lost on the next run. A new
proposal that contains at least half of the photos of an old live proposal now takes over
its id; the larger half keeps it when a late home photo splits an old one in two.

## 7. Hardware constraints and how they shaped the code

- EXIF via `exiftool` in batches. Thumbnails come from the NAS's `@eaDir` when present;
  otherwise (revised September 2026, after the first real run showed mostly placeholders)
  they are generated once into `data/thumbs`: Pillow in JPEG draft mode (decodes at 1/8
  scale, ~30 ms per 12 MP photo), the embedded preview for RAW via exiftool, one frame via
  ffmpeg for videos. A background thread pre-generates after each scan, pausing whenever a
  page is loading thumbnails. Decoding runs in a helper process (September 2026: with a
  512 MB memory limit the container was killed and restarted a few seconds after every run,
  without a traceback, as soon as the prefetch reached a handful of broken and very large
  files; the kill hit uvicorn itself). A helper that dies or exceeds its memory cap costs one
  thumbnail; a file that gave none is remembered and not decoded again on every run. This is
  the one place the app decodes images; it is bounded and cached, and can be switched off.
- The status poll and the pages must stay cheap on a library of tens of thousands of files:
  the proposals file is parsed only when it changed, the sorted tree is walked for the cluster
  list at most every few minutes, records are re-zoned only when home, radii or named places
  changed (not on every run), and the neighbour-GPS fill runs in one thread at a time.
- Offline reverse geocoding with a pure-Python package (no numpy/scipy build on the T430).
- The app is one ~60 MB container; the decision-model container that was planned next to it
  (~1 GB resident) is gone with the model (section 10).
- No JS build, no CDN: server-rendered Jinja templates and plain forms.
- Moves are `rename()` on the same mount; inbox and target must share it. That means one
  bind mount for both (`PHOTOS_BASE:/photos`): two bind mounts of the same share are two mount
  points inside the container, `rename()` fails with EXDEV, and `shutil.move` copies every
  file through the container and back over the network (87 files took minutes, September
  2026). The pages warn when an inbox and the root are on different devices. On a read-only
  inbox such a copy-move can leave the original behind while the share reports success, so a
  transfer verifies the source is gone and removes the copy otherwise. Keywords for a whole
  cluster are written by one exiftool process (`-execute`), not one per file. The folder's
  manifest is written every 25 files while a cluster moves and extended, never replaced, on a
  later apply into the same folder: a kill mid-move (power, a container restart) loses at most
  one batch of entries, and the continuation after the restart records the files it finds in
  the folder without an entry, so undo always covers everything (a browser test kills the server
  in the middle of a 2 500-file move and restarts it).

## 8. Feedback loop and calibration (historical, see section 10)

Every model decision is logged with confidence and id. Rejecting a proposal, moving a photo
out, or renaming a burst appends a *correction* that references the decision id. The Log page
buckets decisions by confidence and shows accuracy per bucket — for a calibrated model the two
match. This is the actual learning objective of the project: does "0.8" mean 0.8 on my data?

## 8b. Testing policy (September 2026)

Everything runs in Docker (`docker compose -f docker-compose.test.yml run --rm tests`), nothing
on the host. The test image's default command lints, runs the whole suite and fails below 99 %
line and branch coverage of `app/`; it stands at 100 %, with dead code removed rather than
covered. Beyond the Python: one browser session executes `ui.js` under Chromium's coverage
profiler (every function, 99 %+ of the code; page-initiated navigations are answered with 204
so the document survives for the snapshot), every template branch is rendered and asserted,
every way a file can move is checked against dry-run, live-with-switches-off and each switch,
scale tests print latencies for 20 000 records and a 3 000-photo proposal, and CI starts the
published runtime image and calls every page. Randomized inputs (a thousand tag sets, timestamps, offsets,
user texts and timelines) go through every parser and the clustering; their first run found an
out-of-range coordinate crashing the geocoder, an EXIF offset beyond +14:00 crashing the time
parser, control characters surviving in folder names and a split run that could start with, or
consist only of, GPS-less photos. The rule when a bug is fixed: the test that
would have caught it lands in the same commit.

## 9. Open points

- Home radius vs. transit: a photo in town on the way out is "local", so it starts the run
  early (the run then carries a few in-town photos); a photo *inside* the home radius ends
  it. A larger home radius for a small town is the tuning knob.
- Devices without GPS whose neighbours are all from another leg will land in the wrong leg;
  corrections catch it.
- Time zones (revised September 2026 when videos were added, again after the first trip to
  Asia): the config has a home zone (`timezone`, seeded from `TZ`). Photo times are exact when
  the file has `OffsetTimeOriginal` (newer phones) or a GPS clock (`GPSDateTime`, UTC; the
  offset is the difference to the wall time, rounded to 15 min); only a naive photo without
  either is read as home-zone time. Video times are exact with iPhone's `Keys:CreationDate`;
  otherwise QuickTime `CreateDate` is UTC by spec, converted to the home zone at scan time and
  re-expressed at cluster time in the offset of the nearest photo (within 48 h) whose offset is
  known, so a clip in Shanghai shows 13:20 next to the 13:11 photo, not 07:20. Records carry
  `ts_source` (exif, exif+offset, gps, utc-home, name, mtime). A phone without a fix writes
  GPS 0/0 into videos; that is read as no position, not as the Gulf of Guinea. Some Android
  builds write local time into `CreateDate` against the spec; no fix without per-device rules.
- Devices: `Make`/`Model` from EXIF (photos, iPhone videos) or the Android QuickTime keys;
  a file without any device metadata (typical Android video) takes the inbox name and, when
  counting devices for the home-burst note, merges with the inbox's single named device.

## 10. The decision model, tried and dropped (September 2026)

Laya (Convai Innovations' open-weights System One model, served behind TypeSafe's
`/v1/systemone` API) was wired in as planned and asked the one question the rules cannot
answer: is this dense burst of photos at home an occasion or just a busy day? On the first
real library it answered `busy_day` for every burst, with confidence between 0.06 and 0.11.
Rephrasing the state as prose and adding an obvious party did not help:

| burst described as                        | p(occasion) | confidence |
|-------------------------------------------|-------------|------------|
| birthday, numbers only (as the app sent)  | 0.34        | 0.08       |
| birthday, prose                           | 0.29        | 0.13       |
| chores, numbers only                      | 0.30        | 0.11       |
| chores, prose                             | 0.33        | 0.09       |
| a 60-photo, five-phone party, prose       | 0.26        | 0.17       |

The party scores below the chores; the signal is absent, and the low confidence says so. A
text-classification model trained on tickets, routing and moderation has no way to read a
handful of counts and hours, and the metadata itself carries no feature that separates a
birthday from twenty shots of the same thing. The rule-based fallback gave the same verdicts.

**Decision (2026-09-26, user):** drop the model from the pipeline. Every burst above the size
gate is proposed and the user names or rejects it, which is what the review queue is for. The
adapter (`app/kev.py`), the Laya container, the calibration view, the model settings and the
model tests were removed rather than left as dead weight; they are in the git history (last
commit with them: 1a02926) should a better question turn up.

Consequences:

- The pipeline is fully deterministic and needs no second container; a run costs no model
  calls. The rules in section 3 are the whole logic.
- The review queue is the only judge of home occasions. Its cost is more proposals to reject
  (on the first real library: four small bursts of 13-18 photos next to one real 57-photo
  event). `burst_min_photos` and `burst_baseline_factor` set that trade-off.
- Section 2's argument for a *small* model share stands; its conclusion that one question
  would be load-bearing did not survive contact with data. What would make a model useful
  here: image content (rejected for this hardware), or a few hundred reviewed bursts to
  fine-tune on, which the review queue now collects as manifests.
