# Using photosort

How sorting works, what each page is for, and what happens to your files.

## How sorting works

1. **Scan** (every 10 minutes, or *Run now* in the header). Every photo and video in every input
   folder is read once with `exiftool`: time, position, device. Its place is looked up offline
   (towns of 1000 people and more, or your own named places), and its *zone* relative to home:
   **home** (closer than 0.5 km), **local** (closer than 20 km), **away** (further).
   - A photo without GPS takes the position of the photos with GPS around it in time: from the
     same device first, then from any device, so a camera's shots join the phone's trip.
     Between two located photos a few hours apart, the position is interpolated by time.
   - Videos (iPhone and Android MP4/MOV) get their GPS from the QuickTime metadata and their
     time converted to the home time zone; abroad, they take the UTC offset of the photos
     around them.
2. **Cluster.** The rules turn the records into proposals:
   - **Trip**: a run of photos away from home that spans at least 20 hours. Any photo at home
     ends it. Lisbon → Seville → Lisbon is one trip, named `2026-06 Lisbon, Sevilla`.
   - **Day out**: such a run that is shorter, `2026-06-27 Starnberg`. Near home it needs at
     least 8 photos, so the school run stays everyday.
   - **Occasion at home**: a burst of photos at home well above your usual day (12 photos and
     four times your median day, by default). Nothing judges whether it *was* an occasion:
     metadata cannot tell a birthday from twenty shots of the same thing. It waits in
     `_unnamed/` until you name it (`2026-06-30 Birthday`) or reject it.
   - **Everyday**: everything else. It stays in the inbox and is shown on the *Everyday* page.
3. **Review** in the web page: you approve, rename, reject. Approved clusters move once dry-run
   is off.

Names: places most photographed first (at most four, then countries, then "Multiple"). A place
only a couple of photos mention (a motorway stop) and a short stop at either end of a trip (the
airport on the way out) are left out. Your own place names ("Black Forest") beat the map: tick
*remember this place* when you rename a cluster. Several devices: each is followed on its own
timeline, so a partner's photos at home never cut your trip short, and two phones on two
continents are two trips.

## The pages

| Page | What it is for |
|---|---|
| **Dashboard** | What waits for you, recent events, the inputs that were found. |
| **Review** | Every proposal with its photos. Rename it (saved as you type), click photos to leave them out, approve or reject. *why* shows the rule that made it, its conditions against the run's numbers, and the longest pause inside it. Bursts waiting for a name are listed below. |
| **Everyday** | Every photo in no cluster, by month and day. Tick photos to add them to a proposal or to build a cluster by hand (kept across runs, marked "by hand"). *Move … everyday photos* files those older than 4 days into `YYYY/MM/`. |
| **Clusters** | The clusters already moved. On a cluster's page: rename it, move photos to another cluster or back to the inbox, undo the whole cluster. |
| **History** | Every action that moved files, one line per file; revert an action, or put one file back where it was. The list of originals copy mode left behind. |
| **Log** | Every event: runs, reviews, settings changes, failures. |
| **Settings** | Three modes: *basic* for a first setup, *advanced* adds the thresholds, *expert* everything, including your own rules. See [CONFIGURATION.md](CONFIGURATION.md). |

A move, an undo, a rename or a put back runs in the background; the page shows its progress
and stays usable. Every page keeps what you are doing when a scan finishes and offers to
reload instead.

## Dry-run, and what moves

**Dry-run is on until you switch it off, and it means exactly that: nothing is moved, copied or
deleted.** Approving only marks a proposal. Switching dry-run off (*Settings*, with a
confirmation) moves everything you approved.

- An approval covers the photos you saw. If photos join an approved cluster before it moves (a
  camera imported after the phones), it comes back to *Review* with "N new photos since you
  approved it".
- A trip still under way (no photo at home yet, and the last photo less than 4 days old) is
  *ongoing* and waits.
- Moved clusters land in `sorted/<name>/`, with one subfolder per device if you like. Each gets
  a `manifest.json` (where every file came from), which is what undo uses.
- Only clusters moved *without asking* (the auto-move switches) park photos with a doubtful
  position in `<cluster>/_review/`; approving by hand settles them.

## Starting over

Everything in review is temporary until it moves. *Settings → Start over* drops every proposal
that has not been moved, together with what you decided about it (approvals, rejections, names,
left-out photos, clusters built by hand), reads every photo in the inbox again and clusters
with the settings as they are now. Moved clusters, the history and the settings stay. Use it
after changing a threshold, a home or a device setting, or after adding a device whose photos
belong to clusters you are still reviewing.

## Copy mode and read-only inboxes

*Copy instead of move* (*Settings*, advanced) copies into the sorted tree and leaves the
originals in the inbox; they are not proposed again. This is also the way to work with an inbox
mounted read-only (a move there fails). *History → Originals left in the inbox* lists the
originals that were copied and are still there, as a plain list to delete them yourself later
or as a CSV with the copies. Copy mode needs the records kept centrally (the default).

## Your data

| File | What |
|---|---|
| `data/config.yaml` | the settings |
| `data/proposals.json` | the proposals and what you decided |
| `data/journal.jsonl` | every file operation (History) |
| `data/events.jsonl` | the event log (Log) |
| `data/index/` | one small record per photo in the inbox (rebuilt when lost) |
| `data/thumbs/` | generated thumbnails |
| `data/*.json` (others) | small caches: the usual day per home, home candidates, zoning |

Losing `data/` loses history and the proposals, never photos.
