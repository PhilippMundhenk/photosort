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
(< 20 km), **away**. Earlier ideas — a static "holiday or not" filter, DBSCAN over
time+space, a window around user-given dates — were dropped because trips vary from one day
to four weeks and the user does not want to type exact dates. Distance from home is the one
feature that is always available and always meaningful.

### Trip = maximal run of `away` photos, ended by any `home` photo

- Returning home ends a trip *no matter how short the stay*. This was an explicit
  requirement and removes all place-based adjacency logic.
- Lisbon → Seville → Lisbon is one trip, cross-country, flat (no nested "leg" folders —
  nesting was proposed and rejected).
- Local / no-GPS photos inside a run ride along with lower confidence (transit, an airport
  photo). They are not needed to define the trip; they must not break it either.
- A photo-less gap splits a run only if it is long (`trip_gap_days`) **and** the two sides are
  far apart. "Same area → same trip" was the user's rule for gaps; a model question was
  considered and dropped.
- Photos without GPS inherit the nearest GPS'd photo in time (within 48 h). Also the user's
  rule; simpler than asking the model.
- A run whose last photo is recent is marked *ongoing* (the home photo may not have synced
  yet) and is never applied automatically.

### Naming

`<span> <places>`: cities in order of first appearance, deduplicated, at most four; then
countries; then "Multiple". The city name comes from the offline geocoder; villages below a
population threshold are replaced by their region so names stay recognisable (`Lisbon`
instead of `Alfama`). The user did not care whether it says "Lisbon, Seville" or
"Portugal, Spain", so the shorter list wins.

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

## 4. State: filesystem, not a database

Explicit requirement: databases get lost and break. So:

- **Sidecar per photo** (`<file>.photosort.json`): timestamp, GPS, place, zone, cluster,
  decision. Regenerable — delete it and the next scan rebuilds it. Keywords are optionally
  mirrored into a standard `.xmp` sidecar for digiKam/Lightroom. Writing into the originals
  was rejected (no benefit, risky for RAW).
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

## 7. Hardware constraints and how they shaped the code

- Never open an image: EXIF via `exiftool` in batches, thumbnails from the NAS's `@eaDir`.
- Offline reverse geocoding with a pure-Python package (no numpy/scipy build on the T430).
- The decision model runs as its own resident container (~1 GB); the app itself is ~60 MB.
  Ollama and Kev should not be loaded at the same time on this machine.
- No JS build, no CDN: server-rendered Jinja templates and plain forms.
- Moves are `rename()` on the same mount; inbox and target must share it.

## 8. Feedback loop and calibration

Every model decision is logged with confidence and id. Rejecting a proposal, moving a photo
out, or renaming a burst appends a *correction* that references the decision id. The Log page
buckets decisions by confidence and shows accuracy per bucket — for a calibrated model the two
match. This is the actual learning objective of the project: does "0.8" mean 0.8 on my data?

## 9. Open points

- ~~Kev/Laya wire formats differ between reproductions~~ Resolved (September 2026): the open
  reproductions converged on TypeSafe's `/v1/systemone` contract (`questions` keyed by id with
  `type/instructions/criteria`, answers with `choice/probabilities/confidence`). `HttpBackend`
  speaks it; Laya (laya-server) is the bundled default because it is the only one that fits the
  T430 (421M encoder, CPU, ~2 GB). Kev/SemIf need a Qwen-class model on a GPU.
- Laya's English checkpoint has a 512-token context; the burst summary is ~60 tokens, so the
  instructions and option descriptions can grow but must stay short.
- Home radius vs. transit: a photo at the local station on the way out ends a trip before it
  starts. Tune the radius, or (later) let the model judge home photos with < 6 h on either
  side.
- Devices without GPS whose neighbours are all from another leg will land in the wrong leg;
  corrections catch it.
- Time zones (revised September 2026 when videos were added): the config has a home zone
  (`timezone`, seeded from `TZ`). Photo times with `OffsetTimeOriginal` are exact; naive EXIF
  times are read as wall time in the home zone. Video times are exact when the file has
  iPhone's `Keys:CreationDate` (with offset); otherwise QuickTime `CreateDate` is UTC by spec
  and is converted to the home zone. Remaining error: a naive photo taken abroad is off by the
  difference between the trip's zone and home (0-2 h in Europe), which cannot misorder it by
  more than that. Some Android builds write local time into `CreateDate` against the spec;
  those videos are then off by the UTC offset. No fix without per-device rules.
- Devices: `Make`/`Model` from EXIF (photos, iPhone videos) or the Android QuickTime keys;
  a file without any device metadata (typical Android video) takes the inbox name and, when
  counting devices for the home-burst question, merges with the inbox's single named device.
