# Design notes

Why photosort works the way it does: the rules, the state it keeps, how it stays fast on a small
box, and the alternatives that were considered and not taken. How to use it is in
[USAGE.md](USAGE.md), every setting in [CONFIGURATION.md](CONFIGURATION.md).

## 1. Purpose and scope

Goal: sort the photos and videos of several devices into a folder tree of trips, day outs and
occasions, with names like `2026-06 Lisbon, Sevilla`, without anyone typing dates, and move
nothing a person has not seen.

Non-goals:

- No database-driven photo manager (Immich, Paperless-style). The folder tree *is* the
  product; other tools keep working on it.
- No image content analysis on the server. The target machine is a CPU-only small box; the only
  images it decodes are thumbnails, bounded and cached (section 7).
- No cloud services and no API models. Everything runs offline.

## 2. Rules, not a decision model

Every decision is a rule over metadata: time, position, device. Rules are debuggable, need no
calibration, run in microseconds and can be explained in the review page ("why"). A decision
model was considered for the one question metadata cannot answer, and is not part of the
pipeline (section 10). The review queue is where that question is answered: by the user.

## 3. The rules, and the decisions behind them

### Zones

Three zones by distance from the nearest home: **home** (< `home_radius_km`, 0.5 km), **local**
(< `local_radius_km`, 20 km), **away**. Distance from home is the one feature that is always
available and always meaningful; a "holiday or not" filter, DBSCAN over time and space, or a
window around dates the user types were rejected because trips vary from one day to weeks and
nobody wants to type dates.

The home point can come from the data: *Detect home* (Settings) takes the ~100 m cell with photos
on the most distinct days. A home point a few kilometres off turns every day into a day out, so
the detected point is offered rather than a guessed one. Detect also offers the next places with
photos on many days (at least 5, 1 km apart) as further homes.

### Excursions: a run of photos away from home, ended by any home photo

What makes a trip is staying away overnight without photos at home in between, not the
distance. So trips and day outs come from one rule: a run of photos outside the home radius,
ended by any home photo, is a **trip** when it spans at least `trip_min_hours` (20 h, an
overnight stay) and a **day out** otherwise. Day outs near home need `dayout_min_photos` (8),
so the school run and the supermarket stay everyday; day outs mostly in the away zone need only
`trip_min_photos` (3): three photos 30 km from home are an outing, three photos in the next
town are noise. The local/away zone is a label and feeds the confidences.

Mechanics of a run:

- Returning home ends a run *no matter how short the stay*. There is no place-based adjacency
  logic.
- Lisbon → Seville → Lisbon is one trip, flat: no nested "leg" folders.
- Local and GPS-less photos inside a run ride along with lower confidence (transit, an airport
  photo). They do not define the trip and do not break it.
- A photo-less gap splits a run only if it is long (`trip_gap_days`, 4) **and** the two sides
  are far apart (`trip_split_distance_km`, 300 km): same area, same trip. Near home the
  assumption is reversed: you sleep at home, so in the local zone a gap longer than
  `local_gap_hours` (12 h) ends the outing even without a home photo, and two evenings out on
  different days stay two day outs.
- A run whose last photo is recent is *ongoing* (the home photo may not have synced yet) and is
  never moved automatically.

### One timeline per device

Excursions are found per device (`source`, the inbox folder). A single merged timeline breaks in
both directions: a photo at home from one phone would end the other phone's trip, and two phones
far apart at the same time would form one trip with both places in its name, because the gaps
between interleaved photos are minutes. Runs of different devices become one excursion only
when they overlap in time and some of their photos are within a day and
`trip_split_distance_km` of each other: the family trip with two phones is one folder, the
partner at home stays out of it. Home bursts look at the merged timeline, since a birthday
photographed with two phones is one occasion.

### Positions for photos without GPS

A GPS-less photo borrows a position from the nearest located photo *of the same device*; a
device with no located photo of its own within `neighbour_gps_hours` (48 h) borrows from the
others. That covers the camera without GPS and the camera that geotags only now and then.
Between two located photos at most `gps_interpolation_hours` apart (6 h) the position is
interpolated by time along the great circle, so on a day of travel the camera's shots spread
along the phone's track instead of piling up at its stops. Across a longer gap (a night, a
flight) the nearest photo is the better guess. Interpolated positions are marked
`interpolated:<before>..<after>` and count as borrowed (confidence 0.8).

### The rules as data

The rules are a ruleset (`rules.py`) evaluated in order: excursion runs go through
`excursion_rules` (a trip if the run spans the trip minimum with enough located photos; a long
run without positions is everyday; a day out far away needs the trip minimum of photos, near
home the day-out minimum), the bursts of the rest through `burst_rules` (a home occasion above
the baseline multiple). The first rule whose conditions hold decides; `everyday` is a kind of
its own. A custom ruleset can say "a burst of 30 photos in the local zone on a weekend is a day
out" or "a trip needs two devices".

`defaults(cfg)` builds the default ruleset from the settings fields. A golden test runs a frozen
copy of the pre-engine clustering code (`tests/legacy_rules.py`) and the engine over every
synthetic library and many parameter sets and requires identical proposals, so the defaults stay
what the rules have always meant. Every number of the rules is a parameter, expert mode at
least: what "mostly" means (`mostly_share`), which places name a cluster, the confidence under
which a photo is uncertain, the clock-offset window, the home detection.

Conditions are named keys (`photos_min: 8`) rather than an expression syntax: they validate one
by one with messages a person can act on, and the why panel can put each into words. The
editor's **Preview** clusters the inbox with the rules in force and with the ones typed, from the
same records, and lists what would be new, gone or different; nothing is saved until Save.

### Several homes

Homes are a list in the ruleset. A photo inside any home is at home, ends excursions and is
everyday there; the zone beyond follows the distance to the nearest home, and the record keeps
that home's name. Each home has its own usual day: `data/baseline.json` keeps the photos per day
per home, and a home burst is measured against the home it was taken at (a dozen photos at the
grandparents', where you take one a visit, is an occasion; at home, where you take ten a day, it
is not). With more than one home an occasion's default name says where
(`2026-06-30 (25 Fotos) @ Granny`).

### Home occasions are bursts

Photos at home closer together than `burst_gap_hours` (3 h) form a burst. A burst is an occasion
when it has at least `burst_min_photos` (12) and exceeds the home's median photos per day by
`burst_baseline_factor` (4). Home bursts carry no location signal: a birthday and twenty shots of
something for sale look identical in metadata. So nothing judges whether a burst *was* an
occasion: it waits in `_unnamed/` and the user names or rejects it. Reading a calendar (CalDAV)
to name bursts was rejected: the pipeline stays observational and asks the human.

### Naming

`<span> <places>`. The span is the month (`2026-08`) when a multi-day cluster stays within one
month (the day range adds nothing inside a year tree), the day for a single day, and a full range
across months. Places most photographed first, deduplicated, at most four; then countries; then
"Multiple". A place mentioned by fewer than 3 % of a cluster's photos (and fewer than two) is
dropped, so a motorway stop does not name the trip, and so is a short stop at either end: a place
whose photos all come before (or after) every other place's, span less than 3 h, are at most 5
photos (or 5 %) and at most a third of what the rest has. A four-photo airport layover on the
way out does not name the trip. Every one of these numbers is a key of the rules' `naming` block.

The place comes from:

1. the user's **named places** (`named_places`: name, centre, radius), because nothing offline
   knows that a spot is "Black Forest" or "the allotment garden". Ticking "remember this place"
   when renaming a cluster adds one: the cluster's photos define the circle (mean position,
   radius to the farthest photo, within the `naming` limits).
2. else the offline geocoder's town when its population is at least `min_city_population`
   (1000; a much higher bar gives state names for most of a country), else the region.

City districts and airports would need place data of photosort's own (a GeoNames extract with the
admin hierarchy, an airport list); "remember this place" covers them by hand instead.

## 3b. Code map

One package, `app/`, no framework beyond FastAPI and Jinja:

- `config.py`: the dataclass behind `config.yaml`, loaded per request from a content-hashed cache,
  coerced to field types, discovered inputs, time zone, per-device options.
- `ingest.py`: exiftool in batches, one JSON record per photo (central index or beside the photo),
  timestamps and offsets, GPS sanity, the scan with its change tracking and re-zoning key.
- `cluster.py`: the record cache, positions for photos without GPS, per-device excursions and
  their merge, bursts, naming, proposals and their identity across runs, the proposals file and
  its lock.
- `rules.py`: the clustering rules as data: homes, excursion cutting parameters, excursion rules,
  burst rules, naming, review and detection parameters; `defaults(cfg)` from the settings fields,
  `effective(cfg)` with the custom blocks from the config laid over it; validation, the YAML round
  trip, feature extraction, first-match evaluation and the words of the why panel.
- `mover.py`: every file operation (move, copy, undo, rename, move out, put back), the dry-run
  guard, manifests written incrementally, the cluster list and the cross-mount warning.
- `geo.py`: distance, zones, great-circle interpolation, the offline geocoder with the
  covering-town rule, own places, home detection.
- `thumbs.py`: thumbnails and previews in a helper process, the cache and its failure markers,
  the paced prefetch.
- `events.py`: the append-only log and its tail reader.
- `journal.py`: the transaction journal, one line per file operation in batches (one per action),
  and revert of a batch or of one file (section 4b).
- `main.py`: the pages and actions, the pipeline, the worker for moves and folder jobs, the
  status endpoint.

## 4. State: filesystem, not a database

Databases get lost and break; files in folders do not need a server. So:

- **One JSON record per photo**: timestamp, GPS, place, zone, cluster, decision. Regenerable:
  delete it and the next scan rebuilds it. By default it lives in a central index
  (`data/index/<inbox>/<relative path>/<file>.photosort.json`), so the photo folders do not
  double their file count; "beside the photo" is an option (the record then follows the file
  wherever another tool moves it), and the file name pattern is configurable. Once a photo is
  sorted its record is dropped by default (the folder's `manifest.json` carries everything);
  copy-mode originals keep theirs, which marks them as done.
- **Keywords** are optionally mirrored into a standard `.xmp` sidecar for digiKam and Lightroom.
  The sidecar holds the keywords only, not a copy of the photo's metadata: photo managers prefer
  the sidecar to the file, so copied dates (without their offset) or a video's UTC clock would
  override the right values. Writing into the originals is rejected (no benefit, risky for RAW).
- **Manifest per cluster folder** (`manifest.json`): proposal, decision and confidence,
  source→target of every photo, later corrections. It is what undo uses.
- **The folder tree is the decision**: a photo in a trip folder is settled; `_unnamed/` holds
  bursts waiting for a label (each folder carries its date, `_unnamed/2026-06-30 (25 Fotos)/`, so
  several can wait at once). `_review/` inside a folder holds low-confidence photos of clusters
  moved *without asking* only: when a human approves a proposal, that is the review, and
  everything goes straight into the folder.
- `data/` (config.yaml, proposals.json, journal.jsonl, events.jsonl, small caches) is the only
  other state; losing it costs history and open proposals, never photos.

### Inputs are discovered

The container mounts one inbox root, and every direct subfolder is an input named after the
folder, re-read on each scan; the config only holds an optional list of inputs that overrides
this. The folder name is the device name that appears in per-source subfolders and in the
records (`source`), so naming folders after their owners ("anna", "ben") gives readable trees,
and per-device settings go by that name.

### When the share drops out

An inbox folder that is not there at scan time (the share unmounted, the NAS down) is reported on
the dashboard and its records leave the cache for that run: nothing is proposed from photos
nobody can reach, so an approve-all during the outage cannot mark a trip "applied" without moving
a file. A move that finds none of its files raises instead of succeeding emptily; the proposal
stays approved with the error, and the retry works once the share is back. When it returns, the
records are still in the index, the same proposals come back with the same ids, and nothing is
re-indexed.

### 4b. The transaction journal

`data/journal.jsonl` answers "where is this file now, and how do I get back?". Every file
operation passes through two primitives in `mover` (`_transfer`, `_delete_with_sidecars`), and
each writes one line (op, src, dst); every action (apply, everyday move, undo, rename, move out,
put back, revert) opens a batch, so a batch is one user or worker action. The History page lists
the batches with a Revert button and traces a file by name, offering to put it back where it was
before any one action. Revert moves back what is still where the batch left it and reports the
rest; it never guesses, never overwrites (a clash gets the `_1` suffix like any move), and never
deletes an original (a deleted copy cannot be restored and says so). A revert is a batch itself
and can be reverted. Manifests stay as the per-folder view and follow a revert (an emptied folder
stops being a cluster; a reverted undo gets its manifest back). Losing the journal costs history,
never photos. Nothing prunes it.

## 5. Triggers

Pure observation: new photos in the inbox are enough, because "outside the home radius" plus
"ended by a home photo" defines a trip without any hint. Seeding trips from a chat message or a
hand-made folder is not needed.

Scans poll every `scan_interval_min` minutes instead of using inotify, which is unreliable over
SMB/CIFS.

## 6. Review UI

A web service with a review queue, a log and a settings page. Chat (Matrix) is not used for
approvals or notifications: items get lost in a chat, it has no queue, and the review queue is the
one place where proposals are decided.

### Dry-run is absolute

Dry-run is the default and means nothing is moved, copied or deleted, whatever is approved.
It is enforced in one place, the two functions in `mover` through which every file operation
passes (`_transfer`, `_delete_with_sidecars`), plus `apply`/`apply_everyday`/`undo`/`rename`
themselves; they raise `DryRun`. Approvals are only recorded; switching dry-run off (with a
confirmation) queues them. An invariant test drives every UI action with dry-run on and asserts
the inbox is untouched. Auto-apply is per kind (trips first, bursts later), so trust can be built
step by step.

### The page never reloads itself

With a scan every few minutes, a reload would lose ticked photos, a half-typed name and the open
viewer, and re-fetch hundreds of thumbnails. One poll per page updates the busy banner and the
move progress in place and, when a run finished or files were moved, offers a reload in the
banner; only pages without user state (dashboard, log, clusters) refresh on their own. Clicks on
photos flip at once and travel batched, one request in flight per proposal, because a page full
of lazily loading thumbnails would otherwise queue every click behind the browser's six
connections per host.

### Settings

Three modes: basic shows what a first setup needs (dry-run, what moves by itself, folders, home),
advanced adds the clustering thresholds, expert everything else (folder details, records, rarely
touched limits, custom rules). A hidden setting is still in the form, so a save in basic mode
keeps every value; the mode is remembered in the config. The page is one column: a section per
topic, a row per setting with a plain-language description on the left (what it does, when to
change it, an example) and the control on the right. A setting that differs from its default says
so under its control ("default: 4 · use default"), and a section's heading counts them, hidden
rows of other modes included, so a value tuned in expert mode stays visible in basic mode.
Putting a default back only changes the field; Save stores it.

### One look for the same thing

Every photo grid (Review, Everyday, a cluster's page) is written by one function
(`main._figure`; the pages only set what a click does and what the caption says), every
destructive action (undo, revert, removing records, Start over) is a red button, confirmations
come only where files move or records go (not in dry-run), and the conventions are written at
the top of `style.css`.

### Moving photos between clusters

Everyday and a cluster's page share one selection bar and one target list: open proposals,
clusters already moved (the worker moves the photos into their folder, the manifest grows), a new
cluster, or, on a cluster page, back to the inbox. Moving out of a cluster is a correction (the
old manifest lists it, the old proposal excludes it), so undo of either cluster stays exact; the
moves out of one action are one journal batch, the move into the target is the worker's.

### Long actions run in the worker

Moves, undo, rename and put back run in a background worker, so the browser and a reverse proxy
never wait for a share. The queue holds job keys: a proposal id, the everyday job, or a folder
job (`undo:<folder>`, `rename:<folder>`, `put_back:<folder>:<photo>`) whose type and arguments
wait in `_jobs`. The worker runs one job at a time under `_lock`, so a job never moves files
while a scan runs; each job is one journal batch, and dry-run is checked when the request comes
(refused, nothing queued) and again when the job starts (switched on meanwhile: the job fails
with "dry-run is on", nothing moves). While a job waits or runs, every other action on its folder
is refused with "folder busy" instead of racing it. A rename stays inside the request when it is
the directory rename plus a few records in the central index; with records beside the photos or
more than 300 photos it is queued, and the request answers with the new folder's page, which
shows the progress until the folder is there. A failure stays on the page for fifteen minutes and
in the log.

### Proposals keep their identity

A proposal's id is a hash of its first and last photo. A new proposal that contains at least half
of the photos of an old live proposal takes over its id (the larger half keeps it when a late
home photo splits one in two), so a photo that syncs late does not lose the review state (toggled
photos, an edited name, an approval).

An approval covers the photos you saw. A proposal approved by hand but not moved yet (dry-run, a
move that failed on a read-only inbox) that gains photos on a later run (typically a camera
imported after the phones) goes back to the review queue with a note of how many joined
(`new_photos`) instead of moving them unseen. One approved by an auto-apply rule just takes them.
Once moved, a cluster is final: photos that arrive later are proposed on their own or stay
everyday, and adding them is up to you.

### Everything in review is temporary

A scheduled run keeps review state, so a typed name or an approval survives the next scan.
**Start over** (Settings) throws all of it away: every proposal that has not been moved is
dropped, every inbox photo is read again with exiftool (`scan(force=True)`) and the run clusters
with the settings as saved. It waits for a run or a move in progress rather than being skipped.
Moved clusters, the journal and the settings stay; the copy-mode mark (`copied_to`) survives the
re-read, so originals already sorted in copy mode are not proposed again.

## 7. Hardware constraints and how they shaped the code

- EXIF via `exiftool` in batches of 200, `scan_workers` (3) of them at once: over a share the
  reading waits mostly for the share, so parallel batches cut a first scan to about a third.
- Thumbnails come from the NAS's `@eaDir` when present; otherwise they are generated once into
  `data/thumbs`: Pillow in JPEG draft mode (decodes at 1/8 scale, ~30 ms per 12 MP photo), the
  embedded preview for RAW via exiftool, one frame via ffmpeg for videos. A background thread
  pre-generates after each scan, pausing whenever a page is loading thumbnails. Decoding runs in
  a helper process with a memory cap, so a broken or huge file costs one thumbnail instead of
  the container; a file that gave none is remembered and not decoded again. This is the one
  place the app decodes images; it is bounded, cached and can be switched off.
- The status poll and the pages stay cheap on a library of tens of thousands of files: the
  proposals file is parsed only when it changed, the sorted tree is walked for the cluster list
  at most every few minutes, records are re-zoned only when homes, radii or named places change,
  and the neighbour-GPS fill runs in one thread at a time.
- Offline reverse geocoding with a pure-Python package (no numpy/scipy build on a small box).
- One small container; no second service.
- No JS build, no CDN: server-rendered Jinja templates, plain forms and one `ui.js`.
- Moves are `rename()` on the same mount; inbox and target must share it. That means one bind
  mount for both (`PHOTOS_BASE:/photos`): two bind mounts of the same share are two mount points
  inside the container, `rename()` fails with EXDEV, and `shutil.move` copies every file through
  the container and back over the network. The pages warn when an inbox and the root are on
  different devices. Across devices a move copies in chunks and shows how far the current file
  is. On a read-only inbox such a copy-move can leave the original behind while the share reports
  success, so a transfer verifies the source is gone and removes the copy otherwise.
- Keywords for a whole cluster are written by one exiftool process (`-execute`), not one per
  file. The folder's manifest is written every 25 files while a cluster moves and extended, never
  replaced, on a later apply into the same folder: a kill mid-move (power, a container restart)
  loses at most one batch of entries, and the continuation after the restart records the files it
  finds in the folder without an entry, so undo always covers everything (a browser test kills
  the server in the middle of a 2 500-file move and restarts it).

### A page view must not cost what the library costs

A small box is five to ten times slower than a developer machine, so the pages avoid work per
view that grows with the library: readers share the cached records (only code that edits gets
copies), the everyday list is kept until a record or the proposals file changes, figures are
written out by a plain function rather than the template engine, the inbox folders and the
resolved roots are remembered for ten seconds (a scan always looks afresh), and each page has a
budget of inline photos (Review 400, the other open cards load their first photos when they
scroll near; Everyday 300, the later days of a long month load as they scroll near).

The browser keeps little of what it has seen: thumbnails are made at twice their display size
(240x180), every one declares its size and decodes off the main thread, and figures off screen
are neither laid out nor painted (`content-visibility: auto`). A browser test holds pages to
that: after garbage collection, a dozen status polls or twenty rounds of the same clicks add no
nodes, listeners or heap. The harness bounds for Review and Everyday are 150 ms.

## 8. Testing policy

Everything runs in Docker (`docker compose -f docker-compose.test.yml run --rm tests`), nothing on
the host. The test image's default command lints, runs the whole suite and fails below 99 % line
and branch coverage of `app/`; dead code is removed rather than covered. One browser session
executes `ui.js` under Chromium's coverage profiler (every function, 99 %+ of the code;
page-initiated navigations are answered with 204 so the document survives for the snapshot),
every template branch is rendered and asserted, every way a file can move is checked against
dry-run, live with switches off and each switch, scale tests time 20 000 synthetic records and a
3 000-photo proposal, and CI starts the published runtime image and calls every page. Randomized
inputs (a thousand tag sets, timestamps, offsets, user texts and timelines) go through every
parser and the clustering. The rule when a bug is fixed: the test that would have caught it lands
in the same change. The suites are listed in [DEVELOPMENT.md](DEVELOPMENT.md#tests).

## 9. Open points and time zones

- Home radius vs. transit: a photo in town on the way out is "local", so it starts the run early
  (the run then carries a few in-town photos); a photo *inside* the home radius ends it. A larger
  home radius for a small town is the tuning knob.
- A device without GPS whose neighbours are all from another leg of a trip lands in the wrong
  leg; moving photos between clusters fixes it.
- Time zones: the config has a home zone (`timezone`, seeded from `TZ`). Photo times are exact
  when the file has `OffsetTimeOriginal` or a GPS clock (`GPSDateTime`, UTC; the offset is the
  difference to the wall time, rounded to 15 min); only a naive photo without either is read as
  home-zone time. Video times are exact with iPhone's `Keys:CreationDate`; otherwise QuickTime
  `CreateDate` is UTC by spec, converted to the home zone at scan time and re-expressed at cluster
  time in the offset of the nearest photo (within `offset_borrow_hours`, 48 h) whose offset is
  known, so a clip abroad shows 10:20 next to the 10:11 photo, not 04:20. Records carry
  `ts_source` (exif, exif+offset, gps, utc-home, video-local, name, mtime). A phone without a fix
  writes GPS 0/0 into videos; that is read as no position, not as the Gulf of Guinea.
- Devices that differ get per-inbox settings (Settings → "How each device behaves",
  `inbox_settings`): `video_clock=local` for Android builds that write local time into
  `CreateDate` against the spec; `borrow_offset=yes` for a camera whose photos carry no offset
  and is set to local time abroad: its wall time is re-read at cluster time in the offset of the
  nearest photo of any device, the reverse of the videos (wall time kept, instant moved);
  `gps_hours` for the window in which a photo without a position borrows one; `home_radius_km`
  for a device with poor GPS. Burst thresholds stay global: a burst often mixes devices, so a
  per-device minimum has no clear meaning.
- Devices are identified by `Make`/`Model` from EXIF (photos, iPhone videos) or the Android
  QuickTime keys; a file without device metadata (typical Android video) takes the inbox name
  and, when counting devices, merges with the inbox's single named device.

## 10. Why there is no decision model

A small text decision model (Laya, which takes a short text *state* and typed questions and
returns calibrated probabilities in milliseconds) was evaluated for the one question the rules
leave open: is this dense burst of photos at home an occasion or just a busy day? The metadata
of a burst (count, duration, weekday, hours, number of devices, ratio to the usual day) carries
no feature that separates a birthday from twenty shots of the same thing, and the model had
nothing to read: its answers did not separate the two. So the pipeline has no model:

- It is fully deterministic and needs no second container; a run costs no model calls. The rules
  in section 3 are the whole logic.
- The review queue is the judge of home occasions. Its cost is proposals to reject;
  `burst_min_photos` and `burst_baseline_factor` set that trade-off.
- What could make a model useful: image content, which this hardware does not decode. Content
  analysis is not planned; a burst that is not an occasion is rejected, or named like any other
  (a sale, a project) when it deserves a folder.
