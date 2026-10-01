"""photosort web service: scheduler + review UI. Run: uvicorn app.main:app"""
from __future__ import annotations

import logging
import mimetypes
import os
import queue
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from markupsafe import escape as esc

from . import cluster, config, events, geo, ingest, journal, mover, rules, thumbs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("photosort")

BASE = Path(__file__).parent
app = FastAPI(title="photosort")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
tpl = Jinja2Templates(directory=BASE / "templates")


def _static_version() -> str:
    """Changes whenever a static file changes: templates append it as ?v=... so browsers never
    keep an old ui.js after a deploy (they did, and the page then silently lost features)."""
    import hashlib
    h = hashlib.sha1()
    for f in sorted((BASE / "static").glob("*")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:10]


STATIC_VERSION = _static_version()

_lock = threading.Lock()
# runs: counts finished pipeline runs; a page compares it with what it saw when it loaded and
# offers a reload instead of reloading by itself (which threw away what the user was doing)
_state = {"last_run": None, "last_stats": {}, "running": False, "error": None, "progress": None, "runs": 0}

# Approving a proposal only marks it "approved"; this worker moves the files (hundreds of them
# over a share take a while) so the request returns at once. Progress is shown on the review page.
_apply_queue: queue.Queue = queue.Queue()
_applying: dict[str, dict] = {}
_props_lock = cluster.proposals_lock          # every read-modify-write of proposals.json, pages and run alike


EVERYDAY_JOB = "everyday"


def _progress(key: str):
    def cb(done: int, total: int, current: dict | None = None) -> None:
        _applying[key].update(done=done, total=total, current=current)
    return cb


def _apply_everyday_job() -> None:
    with _lock:
        cfg = config.load()
        if cfg.dry_run:
            return
        _applying[EVERYDAY_JOB] = {"done": 0, "total": 0, "name": "everyday photos", "current": None}
        try:
            n = mover.apply_everyday(cfg, min_age_days=cfg.everyday_keep_days, progress=_progress(EVERYDAY_JOB))
            events.log("review", action="move_everyday", n=n)
        except mover.DryRun:
            return
        except Exception as e:  # noqa: BLE001
            log.exception("moving everyday photos failed")
            events.log("review", action="move_everyday_failed", error=f"{type(e).__name__}: {e}")
        finally:
            _applying.pop(EVERYDAY_JOB, None)


def _apply_one(pid: str) -> None:
    if pid == EVERYDAY_JOB:
        _apply_everyday_job()
        return
    with _lock:                                   # never while a scan rewrites the proposals
        cfg = config.load()
        if cfg.dry_run:                           # approved proposals wait; Settings queues them again
            return
        pr = cluster.load_proposals().get(pid)
        if not pr or pr["status"] != "approved":
            return
        _applying[pid] = {"done": 0, "total": pr["n"], "name": pr["name"], "current": None}
        update: dict = {}
        try:
            # a human looked at it: nothing goes to _review; auto-applied by a run (pr["auto"]): uncertain
            # photos are parked in _review
            mover.apply(cfg, pr, reviewed=not pr.get("auto"), progress=_progress(pid))
            update = {"status": "applied", "error": None}
        except mover.DryRun:
            return                                        # switched on meanwhile: stays approved, untouched
        except Exception as e:  # noqa: BLE001
            log.exception("apply failed for %s", pid)
            update = {"error": f"{type(e).__name__}: {e}"}
            events.log("review", proposal=pid, action="apply_failed", name=pr["name"], error=update["error"])
        finally:
            _applying.pop(pid, None)
        with _props_lock:
            props = cluster.load_proposals()          # re-read: the UI may have edited others meanwhile
            if pid in props:
                props[pid].update(update)
                cluster.save_proposals(props)


def _apply_worker() -> None:
    while True:
        pid = _apply_queue.get()
        try:
            _apply_one(pid)
        except Exception:  # noqa: BLE001
            log.exception("apply worker")
        finally:
            _apply_queue.task_done()


def queue_apply(pid: str) -> None:
    _apply_queue.put(pid)


def queue_approved() -> int:
    """Queue every approved-but-not-applied proposal (after dry-run was switched off, at startup)."""
    n = 0
    for pid, pr in cluster.load_proposals().items():
        if pr["status"] == "approved":
            queue_apply(pid)
            n += 1
    return n


def wait_for_apply(timeout: float = 120) -> None:
    """Block until every queued apply is done (tests)."""
    deadline = threading.Event()
    threading.Thread(target=lambda: (_apply_queue.join(), deadline.set()), daemon=True).start()
    deadline.wait(timeout)


# --- pipeline -------------------------------------------------------------------

def run_pipeline(trigger: str = "schedule") -> dict:
    if not _lock.acquire(blocking=False):
        return {"skipped": "already running"}
    _state["running"], _state["error"] = True, None
    _state["progress"] = {"phase": "scanning", "done": 0, "total": 0}

    def scanned(done: int, total: int) -> None:
        _state["progress"] = {"phase": "scanning", "done": done, "total": total}
    cfg, queued = None, []
    try:
        cfg = config.load()
        cluster.busy = True                       # page requests serve a slightly stale cache meanwhile
        s1 = ingest.scan(cfg, progress=scanned)
        if not cfg.home_lat and not cfg.home_lon:  # home unset: nothing could be clustered; detect it now
            found = geo.detect_home(cluster.load_records(cfg)[0])
            if found:
                cfg.home_lat, cfg.home_lon = found["lat"], found["lon"]
                config.save(cfg)
                events.log("settings", changed=["home_lat", "home_lon"], detected=found, by="pipeline")
                log.info("home detected at %s, %s (%d photos on %d days); re-zoning", found["lat"], found["lon"],
                         found["photos"], found["days"])
                _state["progress"] = {"phase": "scanning", "done": 0, "total": 0}
                ingest.scan(cfg, progress=scanned)  # re-zones the records; no exiftool involved
                s1["home_detected"] = found
        cluster.busy = False
        _state["progress"] = {"phase": "clustering", "done": 0, "total": 0}
        s2 = cluster.run(cfg)
        applied, queued = 0, []
        if not cfg.dry_run:
            # auto-apply: the run approves what the settings allow and the apply worker moves it
            # (with progress on the review page), exactly like a proposal approved by hand,
            # except that its uncertain photos go to _review (pr["auto"])
            auto = {"trip": cfg.auto_apply_trips, "local": cfg.auto_apply_local, "home": cfg.auto_apply_home}
            with _props_lock:
                props = cluster.load_proposals()
                for pid, pr in props.items():
                    if pr["status"] == "pending" and auto.get(pr["kind"]):
                        pr["status"], pr["auto"], pr["error"] = "approved", True, None
                        events.log("review", proposal=pid, action="approve", name=pr["name"], by="auto")
                        queued.append(pid)
                if queued:
                    cluster.save_proposals(props)
                waiting = list(_apply_queue.queue)                # approved earlier but never moved (a restart
                queued += [pid for pid, pr in props.items()       # mid-way, an edit of the file): pick them up;
                           if pr["status"] == "approved" and pid not in queued and pid not in waiting
                           and pid not in _applying and not pr.get("error")]   # a failed one waits for "retry"
            if cfg.auto_apply_everyday:
                applied += mover.apply_everyday(cfg, min_age_days=cfg.everyday_keep_days)
                mover.invalidate_clusters()
        stats = {"ingest": s1, "cluster": s2, "applied": applied, "queued": len(queued), "trigger": trigger}
        _state["last_run"], _state["last_stats"] = datetime.now(timezone.utc).isoformat(timespec="seconds"), stats
        events.log("run", **{k: v for k, v in stats.items() if k != "ingest"}, new_photos=s1.get("new"))
        return stats
    except Exception as e:  # noqa: BLE001
        log.exception("pipeline failed")
        _state["error"] = str(e)
        return {"error": str(e)}
    finally:
        cluster.busy = False
        _state["running"], _state["progress"] = False, None
        _state["runs"] += 1
        _lock.release()
        for pid in queued:
            queue_apply(pid)
        if cfg is not None:
            try:                                          # missing thumbnails: found and made in the background
                thumbs.prefetch(cfg, [p for _, folder in config.inbox_dirs(cfg) if folder.exists()
                                      for p in ingest.list_photos(cfg, folder)])
            except Exception:  # noqa: BLE001
                log.exception("thumbnail prefetch")


scheduler = BackgroundScheduler()


@app.on_event("startup")
async def _start():
    import anyio
    # every request handler runs in this pool; a page full of thumbnails must not use up the
    # threads the status poll and the clicks need (default: 40)
    anyio.to_thread.current_default_thread_limiter().total_tokens = 96
    cfg = config.load()
    if not config.CONFIG_PATH.exists():
        config.save(cfg)
    scheduler.add_job(run_pipeline, "interval", minutes=max(1, cfg.scan_interval_min), id="scan",
                      replace_existing=True, next_run_time=datetime.now() + timedelta(seconds=15))
    scheduler.start()                                          # first run shortly after start, not one interval later
    if not any(t.name == "apply" for t in threading.enumerate()):
        threading.Thread(target=_apply_worker, name="apply", daemon=True).start()
    if not cfg.dry_run:
        queue_approved()                                       # approved before a restart: finish them
    threading.Thread(target=_warm_cache, name="warm", daemon=True).start()


def _warm_cache() -> None:
    """Read the records once at startup so the first page does not pay for it (a thousand small
    files through a bind mount take seconds)."""
    try:
        cluster.records_filled(config.load())
    except Exception:  # noqa: BLE001
        log.exception("cache warm-up failed")


@app.on_event("shutdown")
def _stop():
    scheduler.shutdown(wait=False)
    thumbs.shutdown()


@app.exception_handler(mover.DryRun)
async def _dry_run_refused(request: Request, exc: mover.DryRun):
    """Any request that would move a file while dry-run is on ends here, never in a move."""
    return HTMLResponse(f"<h1>Dry-run is on</h1><p>{exc}</p><p><a href='/review'>Review</a> · "
                        f"<a href='/settings'>Settings</a></p>", status_code=409)


# --- helpers --------------------------------------------------------------------

def render(request: Request, name: str, **ctx):
    cfg = config.load()
    ctx.update(request=request, cfg=cfg, state=_state, page=name.split(".")[0], v=STATIC_VERSION,
               mount_note=mover.cross_mount_note(cfg) if cfg.warn_cross_mount else None)
    return tpl.TemplateResponse(request, name, ctx)


def _pending(props: dict) -> list[dict]:
    out = sorted((p for p in props.values() if p["status"] in ("pending", "ongoing")),
                 key=lambda p: p["start"], reverse=True)
    for p in out:                                     # a set for the template: `path in list` per photo
        p["excluded_set"] = set(p.get("excluded", []))   # was quadratic on a 2000-photo proposal
    return out


def figures_html(photos: list[dict], excluded=()) -> Markup:
    """One figure per photo of a proposal, as markup. No form control per photo (a password
    manager watches every input and button). Written out by hand: the template engine took
    80 microseconds per figure, and a review page has hundreds of them."""
    out = []
    for ph in photos:
        path, file_, zone, ts = ph["path"], esc(ph["file"]), esc(ph["zone"]), ph["ts"]
        place, q, when = ph.get("place") or "", quote(str(path), safe="/"), esc(ts[:16].replace("T", " "))
        classes = ("excluded" if path in excluded else "") + " " + ("uncertain" if ph.get("uncertain") else "")
        badge = '<span class="badge">video</span> ' if ph.get("media") == "video" else ""
        source = f" · {esc(ph['source'])}" if ph.get("source") else ""
        out.append(
            f'<figure class="{classes}" data-path="{esc(path)}">'
            f'<span class="pick" role="button" tabindex="0" title="{file_} · {zone} · {esc(place)} · conf '
            f'{esc(ph.get("conf"))} · click to exclude/include"><img src="/thumb?path={q}" loading="lazy" alt="">'
            f'</span><span class="view" role="button" tabindex="0" data-view="/media?path={q}" '
            f'data-type="{esc(ph.get("media"))}" data-title="{file_} · {when} · {esc(place or ph["zone"])}" '
            f'title="view full size">&#x2921;</span><figcaption>{badge}{esc(ts[5:16].replace("T", " "))} {zone}'
            f'{source}</figcaption></figure>')
    return Markup("".join(out))


tpl.env.globals["figures"] = figures_html

REVIEW_INLINE = 80                                    # photos rendered with the page for an open card
# ...and for the whole page: open cards beyond this budget arrive empty and load their first photos
# when they scroll near (a hundred open bursts were eight thousand figures and seconds on a small box)
REVIEW_INLINE_TOTAL = int(os.environ.get("PHOTOSORT_REVIEW_INLINE", "400"))
PHOTOS_CHUNK = 200                                    # photos per lazily loaded chunk


@app.get("/proposal/{pid}/photos", response_class=HTMLResponse)
def proposal_photos(request: Request, pid: str, offset: int = 0, limit: int = PHOTOS_CHUNK):
    """A chunk of a proposal's photo figures (the review page loads them when a list is opened)."""
    pr = cluster.load_proposals().get(pid)
    if not pr:
        return HTMLResponse("", status_code=404)
    offset, limit = max(0, offset), max(1, min(limit, 1000))
    photos = pr["photos"][offset:offset + limit]
    resp = HTMLResponse(str(figures_html(photos, set(pr.get("excluded", [])))))
    resp.headers["X-Total"] = str(len(pr["photos"]))
    resp.headers["X-Next"] = str(offset + len(photos)) if offset + len(photos) < len(pr["photos"]) else ""
    return resp


# --- pages ----------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    cfg = config.load()
    props = cluster.load_proposals()
    unnamed = [c for c in mover.list_clusters(cfg) if c["unnamed"]]
    return render(request, "dashboard.html", pending=_pending(props), unnamed=unnamed,
                  recent=events.read(limit=8), inputs=config.inbox_dirs(cfg))


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, open: str = ""):
    cfg = config.load()
    props = cluster.load_proposals()
    unnamed = [c for c in mover.list_clusters(cfg) if c["unnamed"]]
    applying = [dict(p, progress=_applying.get(p["id"])) for p in props.values() if p["status"] == "approved"]
    approved_all = [p for p in props.values() if p["status"] == "approved"]
    return render(request, "review.html", pending=_pending(props), unnamed=unnamed, open_id=open,
                  applying=sorted(applying, key=lambda p: p["start"], reverse=True),
                  n_approved=len(approved_all), inline=REVIEW_INLINE, inline_total=REVIEW_INLINE_TOTAL)


@app.get("/everyday", response_class=HTMLResponse)
def everyday(request: Request, month: str = "", q: str = ""):
    cfg = config.load()
    props = cluster.load_proposals()
    recs = cluster.everyday_records(cfg)               # cached until a record or a proposal changes
    q = q.strip()
    if q:                                              # find a photo by file name, whatever its month
        needle = q.lower()
        recs = [r for r in recs if needle in r["file"].lower()][:SEARCH_LIMIT]
    months_c = Counter([r["ts"][:7] for r in recs])
    months = [{"key": k, "n": months_c[k]} for k in sorted(months_c)]
    if not month and months:
        month = months[-1]["key"]
    keys = [m["key"] for m in months]
    idx = keys.index(month) if month in keys else -1
    prev_month = keys[idx - 1] if idx > 0 else None
    next_month = keys[idx + 1] if 0 <= idx < len(keys) - 1 else None
    days: dict[str, list] = {}
    for r in recs:
        if q or r["ts"].startswith(month):
            days.setdefault(r["ts"][:10], []).append(r)
    out = []
    for date in sorted(days, reverse=True):
        rs = sorted(days[date], key=lambda r: r["_t"])
        zones = Counter(r["zone"] for r in rs)
        places = Counter((r.get("place") or {}).get("place") for r in rs if r.get("place"))
        out.append({"date": date, "weekday": rs[0]["_t"].strftime("%A"), "n": len(rs),
                    "zones": ", ".join(f"{n} {z}" for z, n in zones.most_common()),
                    "places": ", ".join(p for p, _ in places.most_common(3) if p),
                    "photos": [{"path": r["path"], "file": r["file"], "ts": r["ts"], "zone": r["zone"],
                                "media": r.get("media", "photo"), "place": (r.get("place") or {}).get("place")}
                               for r in rs]})
    movable = len(mover.everyday_movable(cfg, cfg.everyday_keep_days))   # of all, whatever is searched
    moved = sorted((p for p in props.values() if p["status"] in ("applied", "approved")),
                   key=lambda p: p["start"], reverse=True)
    return render(request, "everyday.html", months=months, month=month, days=out, pending=_pending(props),
                  moved=moved, q=q, matches=sum(len(d["photos"]) for d in out) if q else 0,
                  total=len(recs), prev_month=prev_month, next_month=next_month,
                  month_n=months_c.get(month, 0), movable=movable, moving=_applying.get(EVERYDAY_JOB),
                  queued=EVERYDAY_JOB in list(_apply_queue.queue))


SEARCH_LIMIT = 500


@app.post("/everyday/move_all")
def everyday_move_all():
    """Move every everyday photo older than everyday_keep_days into YYYY/MM, in the background."""
    cfg = config.load()
    if cfg.dry_run:
        raise mover.DryRun("dry-run is on: everyday photos are not moved (switch it off in Settings)")
    if EVERYDAY_JOB not in _applying and EVERYDAY_JOB not in list(_apply_queue.queue):
        queue_apply(EVERYDAY_JOB)
    return RedirectResponse("/everyday", status_code=303)


def _assign(cfg: config.Config, paths: list[str], target: str, kind: str, name: str) -> dict | None:
    """Everyday photos (in no live proposal) join an existing proposal or form a new manual one.
    Returns the proposal, or None when there was nothing to add or no such target."""
    with _props_lock:
        props = cluster.load_proposals()
        wanted = set(paths)
        recs = [r for r in cluster.everyday_records(cfg, props) if r["path"] in wanted]
        if not recs:
            return None
        if target == "new":
            pr = cluster.create_manual(kind, name, recs)
            props[pr["id"]] = pr
            events.log("review", proposal=pr["id"], action="create", name=pr["name"], cluster_kind=kind, n=len(recs))
        else:
            pr = props.get(target)
            if not pr or pr["status"] not in ("pending", "ongoing", "approved", "applied"):
                return None
            n = cluster.add_to_proposal(pr, recs)
            events.log("review", proposal=pr["id"], action="add", name=pr["name"], n=n)
            if pr["status"] == "applied" and n:
                # the cluster's folder exists already: the additions are moved into it by the worker like
                # an approval (in dry-run they wait, like any approval); the manifest is extended
                pr["status"], pr["error"], pr["auto"] = "approved", None, False
                events.log("review", proposal=pr["id"], action="approve", name=pr["name"], by="add")
        cluster.save_proposals(props)
    if pr["status"] == "approved":
        queue_apply(pr["id"])
    return pr


@app.post("/everyday/assign")
async def everyday_assign(request: Request):
    """Ticked everyday photos join an existing proposal or form a new manual one."""
    cfg = config.load()
    form = await request.form()
    pr = _assign(cfg, [str(p) for p in form.getlist("paths")], str(form.get("target", "new")),
                 str(form.get("kind", "local")), str(form.get("name", "")))
    if pr is None:
        return RedirectResponse("/everyday", status_code=303)
    return RedirectResponse(f"/review?open={pr['id']}#{pr['id']}", status_code=303)


@app.get("/clusters", response_class=HTMLResponse)
def clusters(request: Request):
    cfg = config.load()
    return render(request, "clusters.html", clusters=mover.list_clusters(cfg))


def _cluster_folder(cfg: config.Config, folder: str) -> Path:
    """A folder named by a form must lie inside the sorted root: the cluster actions rename, undo
    and move files, and a request must not be able to point them anywhere else."""
    p = Path(folder)
    try:
        inside = p.resolve().is_relative_to(Path(cfg.root).resolve())
    except OSError:
        inside = False
    if not inside or p.resolve() == Path(cfg.root).resolve():
        raise HTTPException(status_code=400, detail=f"{folder} is not a cluster folder under {cfg.root}")
    return p


@app.get("/clusters/view", response_class=HTMLResponse)
def cluster_view(request: Request, folder: str):
    _cluster_folder(config.load(), folder)
    m = mover.read_manifest(Path(folder))
    props = cluster.load_proposals()
    own = (m or {}).get("proposal_id")
    moved = sorted((p for p in props.values() if p["status"] in ("applied", "approved") and p["id"] != own),
                   key=lambda p: p["start"], reverse=True)
    return render(request, "cluster.html", m=m, folder=folder, pending=_pending(props), moved=moved)


@app.post("/cluster/assign")
async def cluster_assign(request: Request):
    """Selected photos of a cluster go to another cluster (moved in by the worker), to an open
    proposal, to a new cluster, or back to the inbox: each is moved out like a correction (so the
    old cluster's manifest and proposal know), then assigned like an everyday photo. One journal
    batch covers the moves out; the move into an applied cluster is the worker's own batch."""
    cfg = config.load()
    form = await request.form()
    folder = str(form.get("folder", ""))
    folder_p = _cluster_folder(cfg, folder)
    target, kind, name = str(form.get("target", "inbox")), str(form.get("kind", "local")), str(form.get("name", ""))
    paths = [str(p) for p in form.getlist("paths")]
    back = f"/clusters/view?folder={quote(folder)}"
    for p in paths:
        if not Path(p).resolve().is_relative_to(folder_p.resolve()):
            raise HTTPException(status_code=400, detail="the photo is not in that folder")
    m = mover.read_manifest(folder_p) or {}
    if not paths or target == m.get("proposal_id"):          # nothing selected, or its own cluster: no-op
        return RedirectResponse(back, status_code=303)
    mover._guard(cfg)                                        # dry-run: refused before anything, like every move
    moved_out: list[str] = []
    with journal.batch("move_between", folder=folder, target=target, proposal=m.get("proposal_id")):
        for p in paths:
            entry = mover.move_out(cfg, folder_p, Path(p))
            if entry:
                _set_excluded(folder_p, entry["src"], True)
                src = Path(entry["src"])
                if ingest.read_sidecar(src, cfg) is None:
                    ingest.index_paths(cfg, [src])
                moved_out.append(entry["src"])
        pr = _assign(cfg, moved_out, target, kind, name) if target != "inbox" and moved_out else None
    events.log("correction", name=folder_p.name, n=len(moved_out), target=target,
               note="moved to another cluster" if pr else "moved back to the inbox")
    if pr is None:
        return RedirectResponse(back, status_code=303)
    return RedirectResponse(f"/review?open={pr['id']}#{pr['id']}", status_code=303)


@app.get("/history", response_class=HTMLResponse)
def history(request: Request, file: str = ""):
    """Every action that moved files, newest first, with Revert; and the trace of one file."""
    rows = journal.batches(limit=300)
    trace = journal.trace(file) if file.strip() else []
    return render(request, "history.html", rows=rows, file=file.strip(), trace=trace)


@app.post("/history/{batch}/revert")
def history_revert(batch: str, only: str = Form(""), back: str = Form("/history")):
    cfg = config.load()
    try:
        result = journal.revert(cfg, batch, only=only or None)
    except KeyError:
        return HTMLResponse("<h1>Unknown action</h1><p><a href='/history'>History</a></p>", status_code=404)
    events.log("revert", of=batch, reverted=result["reverted"], skipped=len(result["skipped"]), only=only or None)
    mover.invalidate_clusters()
    msg = f"{result['reverted']} file{'s' if result['reverted'] != 1 else ''} put back"
    if result["skipped"]:
        msg += f", {len(result['skipped'])} could not be (see the trace of each file)"
    target = back if back.startswith("/") and not back.startswith("//") else "/history"
    return RedirectResponse(target + ("&" if "?" in target else "?") + "msg=" + quote(msg), status_code=303)


@app.get("/log", response_class=HTMLResponse)
def log_page(request: Request, kind: str = ""):
    return render(request, "log.html", rows=events.read(limit=300, kind=kind or None), kind=kind)


SETTINGS_MODES = ("basic", "advanced", "expert")


def _overview(cfg: config.Config, discovered: list) -> list[dict]:
    """The facts a glance at Settings should give, each pointing at its field (and at the mode
    that shows it)."""
    switches = ((cfg.auto_apply_trips, "trips"), (cfg.auto_apply_local, "day outs"),
                (cfg.auto_apply_home, "home bursts"),
                (cfg.auto_apply_everyday, f"everyday photos older than {cfg.everyday_keep_days:g} days"))
    auto = [label for flag, label in switches if flag]
    if cfg.dry_run:
        moves = "nothing: dry-run is on"
    elif auto:
        moves = "without review: " + ", ".join(auto) + "; the rest after approval"
    else:
        moves = "only what you approve in Review"
    home = (f"{cfg.home_lat:.4f}, {cfg.home_lon:.4f}" if cfg.home_lat or cfg.home_lon
            else "not set (detected on the next run)")
    everyday = "stay in the inbox" if cfg.everyday_layout == "leave" else f"into {cfg.everyday_layout}/"
    found = f"{cfg.inbox_root} — {len(discovered)} found"
    if discovered:
        found += ": " + ", ".join(n for n, _ in discovered)
    sorted_into = cfg.root + (" (copies; originals stay)" if cfg.copy_instead_of_move else "")
    mode = "dry-run: nothing is moved, copied or deleted" if cfg.dry_run else "LIVE: approved proposals are moved"
    records = f"{cfg.sidecar_mode}, {'kept' if cfg.sidecar_cleanup == 'never' else 'dropped'} after a move"
    thumbs_ = "NAS thumbnails, generated when missing" if cfg.generate_thumbnails else "NAS thumbnails only"

    more_homes = f" · {len(cfg.homes)} more home{'s' if len(cfg.homes) != 1 else ''}" if cfg.homes else ""
    custom = "custom rules (the thresholds below do not apply)" if cfg.rules else "the rules built from the fields"

    def row(k, v, field, mode, **flags):
        return {"k": k, "v": v, "field": field, "mode": mode, **flags}
    return [
        row("Mode", mode, "dry", "basic", warn=cfg.dry_run, bad=not cfg.dry_run),
        row("What moves", moves, "at", "basic"),
        row("Inboxes", found, "f-inbox_root", "basic"),
        row("Sorted into", sorted_into, "f-root", "basic"),
        row("Everyday photos", everyday, "f-everyday_layout", "basic"),
        row("Home", f"{home} · {cfg.timezone}" + more_homes, "f-home_lat", "basic"),
        row("Rules", custom, "f-rules", "expert"),
        row("Scan", f"every {cfg.scan_interval_min} min", "f-scan_interval_min", "advanced"),
        row("Records", records, "f-sidecar_mode", "expert"),
        row("Thumbnails", thumbs_, "gt", "advanced"),
    ]


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request, msg: str = "", mode: str = ""):
    cfg = config.load()
    mode = mode if mode in SETTINGS_MODES else cfg.settings_mode if cfg.settings_mode in SETTINGS_MODES else "basic"
    discovered = config.discovered_inboxes(cfg)
    return render(request, "settings.html", inboxes_text=config.inboxes_text(cfg), discovered=discovered,
                  named_places_text=config.named_places_text(cfg), extensions=", ".join(cfg.photo_extensions),
                  msg=msg, mode=mode, modes=SETTINGS_MODES, overview=_overview(cfg, discovered),
                  homes_text=config.named_places_text(cfg, cfg.homes),
                  rules_text=rules.to_yaml(cfg.rules) if cfg.rules else "",
                  rules_in_force=rules.to_yaml(rules.effective(cfg)))


@app.post("/settings/mode")
def settings_mode(mode: str = Form("basic")):
    """The basic / advanced / expert switch: remembered, no other setting touched."""
    if mode in SETTINGS_MODES:
        cfg = config.load()
        if cfg.settings_mode != mode:
            cfg.settings_mode = mode
            config.save(cfg)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/sidecars/{action}")
def settings_sidecars(action: str):
    cfg = config.load()
    if action == "migrate":
        st = ingest.migrate_sidecars(cfg)
        msg = (f"Sidecars: {st['moved']} moved to the {cfg.sidecar_mode} location, {st['kept']} already there, "
               f"{st['photos']} photos checked.")
    elif action == "purge":
        st = ingest.purge_sidecars(cfg)
        msg = f"Sidecars removed: {st['sorted']} in the sorted tree, {st['orphans']} without a photo."
    else:
        return RedirectResponse("/settings", status_code=303)
    events.log("settings", changed=[f"sidecars:{action}"], **st)
    return RedirectResponse("/settings?mode=expert&msg=" + quote(msg), status_code=303)   # the buttons' own card


@app.post("/settings/hide_mount_note")
def settings_hide_mount_note(back: str = Form("/")):
    """The button on the "slow moves" note: the same as unticking the switch in Settings."""
    cfg = config.load()
    cfg.warn_cross_mount = False
    config.save(cfg)
    events.log("settings", changed=["warn_cross_mount"], warn_cross_mount=False)
    return RedirectResponse(back if back.startswith("/") and not back.startswith("//") else "/", status_code=303)


@app.post("/settings/detect_home")
def settings_detect_home():
    """Set home to where photos are taken on the most distinct days (~100 m cell)."""
    cfg = config.load()
    recs, _ = cluster.load_records(cfg)
    found = geo.detect_home(recs)
    if not found:
        return RedirectResponse("/settings?msg=" + quote("No photos with GPS indexed yet; run a scan first."),
                                status_code=303)
    cfg.home_lat, cfg.home_lon = found["lat"], found["lon"]
    config.save(cfg)
    events.log("settings", changed=["home_lat", "home_lon"], detected=found)
    msg = (f"Home set to {found['lat']:.4f}, {found['lon']:.4f}: {found['photos']} photos on {found['days']} "
           f"different days there. Zones are recomputed on the next run.")
    return RedirectResponse("/settings?msg=" + quote(msg), status_code=303)


# --- actions --------------------------------------------------------------------

@app.post("/settings")
async def settings_save(request: Request):
    form = dict(await request.form())
    before = config.load()
    rules_text = str(form.pop("rules", "")).strip()
    cfg = config.update_from_form(config.load(), form)
    rules_msg = ""
    try:
        cfg.rules = rules.parse(rules_text) if rules_text else {}
    except ValueError as e:                                    # the other fields are saved; the rules stay
        rules_msg = f" Rules not saved: {e}."
    config.save(cfg)
    scheduler.reschedule_job("scan", trigger="interval", minutes=max(1, cfg.scan_interval_min))
    events.log("settings", changed=sorted(form.keys()) + (["rules"] if rules_text or before.rules else []),
               dry_run=cfg.dry_run)
    msg = ""
    if before.dry_run and not cfg.dry_run:
        n = queue_approved()                                   # they waited for exactly this
        msg = f"Dry-run is off. {n} approved proposal{'s' if n != 1 else ''} will be moved now."
    elif not before.dry_run and cfg.dry_run:
        msg = "Dry-run is on: nothing will be moved, copied or deleted."
    msg = (msg + rules_msg).strip()
    return RedirectResponse("/settings" + (f"?msg={quote(msg)}" if msg else ""), status_code=303)


@app.post("/run")
def run_now():
    threading.Thread(target=run_pipeline, args=("manual",), daemon=True).start()
    return RedirectResponse("/", status_code=303)


@app.post("/proposal/{pid}/{action}")
async def proposal_action(pid: str, action: str, request: Request):
    raw = await request.form()
    form = dict(raw)
    form["paths"] = [str(p) for p in raw.getlist("path")]        # toggle: several photos in one request
    with _props_lock:
        return _proposal_action(pid, action, form, request)


def _rename(cfg: config.Config, pid: str, pr: dict, name: str, remember: bool) -> None:
    new = cluster.sanitize(name)
    if not new or new == pr["name"]:
        return
    pr["name"], pr["name_edited"] = new, True
    events.log("review", proposal=pid, action="rename", name=pr["name"])
    if remember:
        _remember_place(cfg, pr["name"], [(p.get("lat"), p.get("lon")) for p in pr["photos"]
                                          if p["path"] not in set(pr.get("excluded", []))])


def _proposal_action(pid: str, action: str, form: dict, request: Request):
    cfg = config.load()
    props = cluster.load_proposals()
    pr = props.get(pid)
    if not pr:
        return RedirectResponse("/review", status_code=303)
    queue_after_save = None
    # approve/reject may carry the name field of the same card: an edit made right before the
    # click must not be lost to a race with the autosave request
    if action in ("approve", "reject") and form.get("name") is not None:
        _rename(cfg, pid, pr, str(form["name"]), bool(form.get("remember_place")))
    if action == "approve" and (pr["status"] in ("pending", "ongoing") or
                                (pr["status"] == "approved" and pid not in _applying)):
        # pending: approve; approved with an error (the move failed): retry - the button did
        # nothing before because the proposal was already "approved"
        retry = pr["status"] == "approved"
        pr["status"], pr["error"] = "approved", None  # the apply worker moves the files, after the save below
        events.log("review", proposal=pid, action="retry" if retry else "approve", name=pr["name"])
        queue_after_save = pid
    elif action == "reject":
        pr["status"] = "rejected"
        events.log("review", proposal=pid, action="reject", name=pr["name"], cluster_kind=pr["kind"])
    elif action == "rename":
        _rename(cfg, pid, pr, str(form.get("name", pr["name"])), bool(form.get("remember_place")))
    elif action == "toggle":
        # one request may carry several clicks (ui.js batches them); each flips its photo, in
        # order, so clicking the same photo twice is a no-op as it looks on the page
        paths = form.get("paths") or [form.get("path", "")]
        ex = set(pr.get("excluded", []))
        for path in paths:
            ex.symmetric_difference_update({path})
            events.log("review", proposal=pid, action="toggle", photo=Path(path).name,
                       excluded=path in ex, decision_id=pr["decision"].get("id"))
        pr["excluded"] = sorted(ex)
    cluster.save_proposals(props)
    if queue_after_save:
        queue_apply(queue_after_save)
    if request.headers.get("x-requested-with") == "fetch":            # ui.js: no page reload
        ex = set(pr.get("excluded", []))
        last = (form.get("paths") or [form.get("path", "")])[-1]
        return JSONResponse({"ok": True, "status": pr["status"], "name": pr["name"],
                             "excluded": last in ex, "excluded_paths": sorted(ex)})
    return RedirectResponse(form.get("back", "/review"), status_code=303)


_DATE_PREFIX = re.compile(r"^\d{4}(-\d{2}){0,2}(\.\.[\d-]+)?\s*")


def _remember_place(cfg: config.Config, folder_name: str, points: list) -> dict | None:
    """'2026-06-01..04 Black Forest' -> named place 'Black Forest' covering the cluster's photos."""
    name = _DATE_PREFIX.sub("", folder_name).strip(" -_()")
    entry = geo.remember_place(cfg, name, points)
    if entry:
        config.save(cfg)
        events.log("settings", changed=["named_places"], place=entry)
    return entry


@app.post("/proposals/approve_all")
async def approve_all(request: Request):
    """Approve every pending proposal on the review page in one go (ongoing ones wait for their
    home photo). Names edited on the page arrive as name_<id>, like the single approve."""
    form = dict(await request.form())
    with _props_lock:
        cfg = config.load()
        props = cluster.load_proposals()
        queued = []
        for pid, pr in props.items():
            if pr["status"] != "pending":
                continue
            if form.get(f"name_{pid}") is not None:
                _rename(cfg, pid, pr, str(form[f"name_{pid}"]), bool(form.get(f"remember_place_{pid}")))
            pr["status"], pr["error"] = "approved", None
            events.log("review", proposal=pid, action="approve", name=pr["name"])
            queued.append(pid)
        cluster.save_proposals(props)
    for pid in queued:
        queue_apply(pid)
    return RedirectResponse("/review", status_code=303)


@app.post("/cluster/rename")
def cluster_rename(request: Request, folder: str = Form(...), name: str = Form(...), remember_place: str = Form("")):
    cfg = config.load()
    _cluster_folder(cfg, folder)
    try:
        dst = mover.rename(cfg, Path(folder), name)
    except mover.FolderInUse as e:
        if request.headers.get("x-requested-with") == "fetch":
            return JSONResponse({"ok": False, "error": str(e)}, status_code=423)
        back = f"/clusters/view?folder={quote(folder)}"
        return HTMLResponse(f"<h1>Not renamed</h1><p>{e}</p><p><a href='{back}'>back</a></p>", status_code=423)
    if remember_place:
        m = mover.read_manifest(dst) or {}
        pr = cluster.load_proposals().get(m.get("proposal_id") or "", {})
        pts = [(p.get("lat"), p.get("lon")) for p in pr.get("photos", [])]
        if not any(la is not None for la, _ in pts):                 # older proposal: try the records
            pts = []
            for p in m.get("photos", []):
                rec = ingest.read_sidecar(Path(p["dst"]), cfg) or {}
                pts.append((rec.get("lat"), rec.get("lon")))
        _remember_place(cfg, dst.name, pts)
    if request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": True, "name": dst.name, "folder": str(dst),
                             "redirect": f"/clusters/view?folder={quote(str(dst))}"})
    return RedirectResponse(f"/clusters/view?folder={dst}", status_code=303)


@app.post("/cluster/undo")
def cluster_undo(folder: str = Form(...)):
    cfg = config.load()
    _cluster_folder(cfg, folder)
    mover.undo(cfg, Path(folder))
    with _props_lock:
        props = cluster.load_proposals()
        for pr in props.values():
            if pr["status"] == "applied" and mover.target_folder(cfg, pr) == Path(folder):
                pr["status"] = "rejected"
        cluster.save_proposals(props)
    return RedirectResponse("/clusters", status_code=303)


def _set_excluded(folder: Path, path: str, excluded: bool) -> None:
    """Keep the proposal in step with a correction: a removed photo is excluded from its cluster
    (so it counts as everyday), a put-back one is not."""
    m = mover.read_manifest(folder) or {}
    pid = m.get("proposal_id")
    with _props_lock:
        props = cluster.load_proposals()
        pr = props.get(pid) if pid else None
        if pr is None:
            return
        ex = set(pr.get("excluded", []))
        (ex.add if excluded else ex.discard)(path)
        pr["excluded"] = sorted(ex)
        cluster.save_proposals(props)


@app.post("/cluster/move_out")
def cluster_move_out(folder: str = Form(...), photo: str = Form(...)):
    cfg = config.load()
    _cluster_folder(cfg, folder)
    if not Path(photo).resolve().is_relative_to(_cluster_folder(cfg, folder).resolve()):
        raise HTTPException(status_code=400, detail="the photo is not in that folder")
    entry = mover.move_out(cfg, Path(folder), Path(photo))
    if entry:
        _set_excluded(Path(folder), entry["src"], True)
        back = Path(entry["src"])
        if ingest.read_sidecar(back, cfg) is None:       # its record was dropped on the way in: index this one
            ingest.index_paths(cfg, [back])               # file now (a whole scan of 20 000 took seconds)
    return RedirectResponse(f"/clusters/view?folder={quote(folder)}", status_code=303)


@app.post("/cluster/put_back")
def cluster_put_back(folder: str = Form(...), src: str = Form(...)):
    cfg = config.load()
    _cluster_folder(cfg, folder)
    try:
        entry = mover.put_back(cfg, Path(folder), Path(src))
    except FileNotFoundError as e:
        back = f"/clusters/view?folder={quote(folder)}"
        return HTMLResponse(f"<h1>Not put back</h1><p>{e}</p><p><a href='{back}'>back</a></p>", status_code=404)
    if entry:
        _set_excluded(Path(folder), src, False)
    return RedirectResponse(f"/clusters/view?folder={quote(folder)}", status_code=303)


def _allowed(cfg: config.Config, p: Path) -> bool:
    """Only files under an inbox or the sorted root are served."""
    try:
        rp = p.resolve()
    except OSError:
        return False
    return any(rp.is_relative_to(r) for r in _served_roots(cfg))


_roots_cache: dict = {"key": None, "at": 0.0, "value": []}


def _served_roots(cfg: config.Config) -> list[Path]:
    """The sorted root and the inboxes, resolved. A page asks once per thumbnail, and resolving
    walks every path component on the share: kept for a few seconds."""
    folders = [Path(cfg.root)] + [folder for _, folder in config.inbox_dirs(cfg)]
    key = tuple(str(f) for f in folders)
    now = time.monotonic()
    if _roots_cache["key"] == key and now - _roots_cache["at"] < config.DISCOVERY_TTL_S:
        return _roots_cache["value"]
    value = []
    for f in folders:
        try:
            value.append(f.resolve())
        except OSError:
            continue
    _roots_cache.update(key=key if len(value) == len(folders) else None, at=now, value=value)
    return value


def _read_small(p: Path, limit: int = 3_000_000) -> bytes | None:
    """Whole small file in one go (photos may be moved by a review action while the page still
    loads thumbnails; an open FileResponse would 500 or, on Windows, block the move)."""
    try:
        if p.stat().st_size >= limit:
            return None
        return p.read_bytes()
    except OSError:
        return None


_PLACEHOLDER = {"image": "nothumb.svg", "raw": "nothumb.svg", "video": "novideo.svg"}


@app.get("/thumb")
def thumb(path: str):
    """Thumbnail: NAS thumbnail, cached/generated one, else a placeholder. Never an error."""
    cfg = config.load()
    p = Path(path)
    thumbs.touch()                                             # the background prefetch yields to pages
    t = thumbs.get(cfg, p) if _allowed(cfg, p) else None
    data = _read_small(t) if t else None
    if data is not None:
        return Response(data, media_type=mimetypes.guess_type(t.name)[0] or "image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})
    return FileResponse(BASE / "static" / _PLACEHOLDER.get(thumbs.kind(p), "nothumb.svg"), media_type="image/svg+xml")


@app.get("/media")
def media(path: str):
    """Full-size view: the original for formats browsers show natively (JPEG, PNG, MP4, ...),
    otherwise a large JPEG preview (HEIC, RAW)."""
    cfg = config.load()
    p = Path(path)
    if not _allowed(cfg, p) or not p.exists():
        return Response(status_code=404)
    if thumbs.browser_native(p):
        return FileResponse(p, media_type=mimetypes.guess_type(p.name)[0] or "application/octet-stream")
    prev = thumbs.get(cfg, p, variant="p")
    if prev:
        return FileResponse(prev, media_type="image/jpeg")
    return Response(status_code=404)


@app.get("/api/status")
def api_status():
    """Polled by every open page every few seconds: must stay cheap (nothing here reads the
    inbox or parses the proposals unless the file changed)."""
    cfg = config.load()
    counts = cluster.status_counts()
    now = time.time()
    applying = {k: {**v, "current": {**v["current"], "seconds": round(now - v["current"]["since"])}
                    if v.get("current") else None} for k, v in _applying.items()}
    queue = {pid: ("failed: " + err if err else "queued") for pid, err in counts["approved"].items()
             if pid not in applying}
    return {"state": _state, "pending": counts["pending"], "dry_run": cfg.dry_run,
            "approved": len(counts["approved"]), "applying": applying, "queue": queue,
            "everyday_queued": EVERYDAY_JOB in list(_apply_queue.queue)}
