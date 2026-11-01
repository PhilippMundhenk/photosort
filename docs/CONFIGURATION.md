# Configuration

Everything is set on the **Settings** page and stored in `data/config.yaml`. Every setting has a
sentence or two next to it on the page; this is the overview. The page has three modes, which
only change what is shown (a hidden setting keeps its value):

* **basic**: what a first setup needs (dry-run, what moves by itself, folders, home);
* **advanced**: adds the thresholds of the rules, places, thumbnails;
* **expert**: everything, including the records and your own rules.

A setting that differs from its default says so under its control and can be put back; each
section's heading counts them.

## What moves, and when

| Setting | Key | Default | Mode |
|---|---|---|---|
| Dry-run: nothing is moved, copied or deleted | `dry_run` | on | basic |
| Move trips / day outs / occasions without asking | `auto_apply_trips`, `auto_apply_local`, `auto_apply_home` | off | basic |
| Move everyday photos without asking | `auto_apply_everyday` | off | basic |
| Days an everyday photo stays in the inbox | `everyday_keep_days` | 4 | basic |
| Copy instead of move (read-only inboxes) | `copy_instead_of_move` | off | advanced |
| Minutes between scans | `scan_interval_min` | 10 | advanced |
| Warn about slow moves (inbox and sorted folder on different mounts) | `warn_cross_mount` | on | advanced |
| Files read at once on a scan (exiftool batches in parallel) | `scan_workers` | 3 | expert |
| Write keywords to `.xmp` files | `write_xmp_sidecar` | on | expert |

## Folders

| Setting | Key | Default | Mode |
|---|---|---|---|
| Inbox folder (one subfolder per device) | `inbox_root` | `/photos/inbox` | basic |
| Sorted folder | `root` | `/photos/sorted` | basic |
| Where everyday photos go (`leave`: stay in the inbox) | `everyday_layout` | `YYYY/MM` | basic |
| Inputs, listed by hand (`name=/path` per line; empty: the subfolders) | `inboxes` | empty | advanced |
| How each device behaves (see below) | `inbox_settings` | empty | advanced |
| A subfolder per device inside a cluster | `subfolder_by_source` | off | advanced |
| Folder for occasions without a name | `unnamed_dir` | `_unnamed` | expert |
| Subfolder for doubtful photos | `review_dir` | `_review` | expert |
| File types | `photo_extensions` | jpg, jpeg, heic, heif, png, dng, cr2, cr3, nef, arw, orf, rw2, mp4, mov | expert |

## Home

| Setting | Key | Default | Mode |
|---|---|---|---|
| Time zone at home (photo times without offset, video UTC times) | `timezone` | `TZ`, else UTC | basic |
| Where home is (*Detect* finds it from the photos and offers further places) | `home_lat`, `home_lon` | detected | basic |
| How far "at home" reaches | `home_radius_km` | 0.5 km | advanced |
| How far "near home" reaches (beyond: away) | `local_radius_km` | 20 km | advanced |
| Further homes (`Name = lat, lon, radius km` per line) | `homes` | empty | advanced |

Each home has its own usual day for occasions; with more than one home, an occasion's name
says which one (`2026-06-30 (25 Fotos) @ Granny`).

## Place names, trips and occasions

| Setting | Key | Default | Mode |
|---|---|---|---|
| Your own places (`Name = lat, lon, radius km`; grown by *remember this place*) | `named_places` | empty | advanced |
| Smallest town that gives a name (smaller: the region) | `min_city_population` | 1000 | expert |
| Hours that make a trip (shorter: a day out) | `trip_min_hours` | 20 | advanced |
| Photos with a position a trip needs | `trip_min_photos` | 3 | advanced |
| Photos a day out near home needs | `dayout_min_photos` | 8 | advanced |
| Interpolate positions of photos without GPS within (0: off) | `gps_interpolation_hours` | 6 h | advanced |
| Hours without photos that end an outing near home | `local_gap_hours` | 12 | expert |
| Days without photos that may split a trip (if the two parts are far apart) | `trip_gap_days` | 4 | expert |
| Distance that makes two areas | `trip_split_distance_km` | 300 km | expert |
| Places in a folder name (more: countries, then "Multiple") | `max_places_in_name` | 4 | expert |
| Name trips within one month by the month (`2026-08` instead of `2026-08-08..29`) | `name_multiday_by_month` | on | expert |
| Hours that keep photos in one burst | `burst_gap_hours` | 3 | advanced |
| Photos a burst at home needs | `burst_min_photos` | 12 | advanced |
| How unusual a burst must be (times your usual day) | `burst_baseline_factor` | 4 | expert |

## Thumbnails and records

| Setting | Key | Default | Mode |
|---|---|---|---|
| Where the NAS keeps its thumbnails | `thumb_pattern` | `@eaDir/{name}/SYNOPHOTO_THUMB_M.jpg` | advanced |
| Make thumbnails when the NAS has none | `generate_thumbnails` | on | advanced |
| Where records are kept (`central` in `data/index/`, or `beside` each photo) | `sidecar_mode` | central | expert |
| Name of a record file | `sidecar_name` | `{name}.photosort.json` | expert |
| After a photo is sorted: drop its record, or keep it | `sidecar_cleanup` | `after_move` | expert |

The page's mode itself is `settings_mode`.

## Devices that differ

*How each device behaves* (`inbox_settings`) takes one line per input, by its folder name:

```
camera: borrow_offset=yes, gps_hours=12
oldphone: video_clock=local
actioncam: home_radius_km=1
```

| Option | What it does |
|---|---|
| `borrow_offset=yes` | Photos of this device carry no time zone (a camera set to local time abroad): they take the offset of the photos around them that have one. |
| `video_clock=local` | Its videos store local time where the standard says UTC (some Android builds). Read when a file is indexed: use *Start over* after changing it. |
| `gps_hours=12` | How many hours away a located photo may be for one of this device without GPS to borrow its position (default 48). |
| `home_radius_km=1` | How far "at home" reaches for this device, for one with poor GPS. |

## Your own rules

In expert mode, *Your own rules* (`rules`) takes YAML. The settings above feed a default ruleset; a
block you write replaces that block of the defaults, and in a parameter block a key you leave
out keeps its default. *Preview: what would change?* clusters the inbox with your rules and
lists which proposals would be new, gone or different, before you save. Rules that do not
validate are not saved, and the message says why.

The default ruleset, as the settings above produce it:

```yaml
excursions:                 # how runs of photos away from home are cut
  split_gap_days: 4.0
  split_distance_km: 300.0
  local_gap_hours: 12.0
  merge_devices: true       # runs of two devices in the same area at the same time are one
  same_area_hours: 24.0
  neighbour_gps_hours: 48.0
  interpolate_gps_hours: 6.0
  offset_borrow_hours: 48.0 # videos and cameras take the UTC offset of photos this close
excursion_rules:            # the first rule whose conditions hold decides
- name: trip
  kind: trip
  when: {span_hours_min: 20.0, located_min: 3}
  name_template: '{span} {places}'
  ongoing_days: 4.0
- name: long run without positions
  kind: everyday
  when: {span_hours_min: 20.0}
- name: day out far away
  kind: local
  when: {mostly_zone: away, photos_min: 3}
  name_template: '{span} {place}'
  ongoing_days: 1.0
- name: far away with too few photos
  kind: everyday
  when: {mostly_zone: away}
- name: day out near home
  kind: local
  when: {photos_min: 8}
  name_template: '{span} {place}'
  ongoing_days: 1.0
bursts:
  gap_hours: 3.0
burst_rules:
- name: home occasion
  kind: home
  when: {zone: home, photos_min: 12, baseline_factor: 4.0}
  name_template: '{span} ({media})'
naming:                     # which places name a cluster
  place_min_share: 0.03
  place_min_photos: 2
  stop_hours: 3.0           # a stop at either end of a trip: shorter than this,
  stop_photos: 5            # at most this many photos (or stop_share of them),
  stop_share: 0.05
  stop_ratio: 3.0           # and this many times fewer than the other places
  remember_min_km: 0.5      # "remember this place": the circle is at least this big
  remember_max_km: 60.0     # and at most this big
review:
  uncertain_below: 0.7      # a photo with a borrowed or missing position counts as doubtful below this
detection:                  # "Detect home" in Settings
  home_candidates: 4        # how many places it finds (the first is home, the others are offered)
  home_min_km: 1.0          # how far apart they must be
  home_min_days: 5          # on how many different days a place needs photos
```

Also possible: `homes` (a list of `{name, lat, lon, radius_km}`).

**Kinds**: `trip`, `local` (a day out), `home` (an occasion, burst rules only), `everyday`.

**Conditions** (all must hold): `span_hours_min`, `span_hours_max`, `photos_min`, `photos_max`,
`located_min`, `mostly_zone` (more than half of the files in `home`/`local`/`away`; `mostly_share`
changes "half"), `zone` (the majority zone), `baseline_factor` (files at least your usual day
times this), `devices_min`, `videos_min`, `weekday` (a list of names), `start_hour_min`,
`end_hour_max`, `max_km_min` / `max_km_max` (the farthest photo's distance from home),
`inbox` (a list of input names the run has files from).

**Name templates**: `{span}` (`2026-08` or `2026-06-27`), `{places}`, `{place}` (the most
photographed place), `{media}` (`12 Fotos, 2 Videos`), `{n}`, `{home}`.

A rule list you write replaces the whole list of that block: to add a rule, copy the default
list above and insert yours where it should be checked. Example: an occasion at home only on a
weekend, named a party.

```yaml
burst_rules:
- name: party
  kind: home
  when: {zone: home, photos_min: 12, baseline_factor: 4, weekday: [Saturday, Sunday]}
  name_template: '{span} Party ({media})'
```

## Environment

| Variable | What |
|---|---|
| `TZ` | the home time zone, until one is saved in Settings |
| `PHOTOSORT_DATA` | where `data/` is inside the container (default `/data`) |
| `PHOTOSORT_REVIEW_INLINE`, `PHOTOSORT_EVERYDAY_INLINE` | photos rendered with the Review (400) and Everyday (300) pages; the rest load as you scroll |
