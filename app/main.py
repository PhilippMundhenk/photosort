"""photosort web service: scheduler + review UI. Run: uvicorn app.main:app"""
from __future__ import annotations

import logging
import mimetypes
import queue
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import cluster, config, events, geo, ingest, mover, thumbs

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
_state = {"last_run": None, "last_stats": {}, "running": False, "error": None, "progress": None}

# Approving a proposal only marks it "approved"; this worker moves the files (hundreds of them
# over a share take a while) so the request returns at once. Progress is shown on the review page.
_apply_queue: queue.Queue = queue.Queue()
_applying: dict[str, dict] = {}
_props_lock = threading.Lock()                # every read-modify-write of proposals.json from a request


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
            mover.apply(cfg, pr, reviewed=True,           # a human looked at it: nothing goes to _review
                        progress=_progress(pid))
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
    try:
        cfg = config.load()
        cluster.busy = True                       # page requests serve a slightly stale cache meanwhile
        s1 = ingest.scan(cfg, progress=scanned)
        cluster.busy = False
        _state["progress"] = {"phase": "clustering", "done": 0, "total": 0}
        s2 = cluster.run(cfg)
        thumbs.prefetch(cfg, [p for _, folder in config.inbox_dirs(cfg) if folder.exists()
                              for p in ingest.list_photos(cfg, folder)])
        applied = 0
        if not cfg.dry_run:
            props = cluster.load_proposals()
            auto = {"trip": cfg.auto_apply_trips, "local": cfg.auto_apply_local, "home": cfg.auto_apply_home}
            for pr in props.values():
                if pr["status"] == "approved" or (pr["status"] == "pending" and auto.get(pr["kind"])):
                    mover.apply(cfg, pr, reviewed=pr["status"] == "approved")
                    pr["status"] = "applied"
                    applied += 1
            cluster.save_proposals(props)
            if cfg.auto_apply_everyday:
                applied += mover.apply_everyday(cfg, min_age_days=cfg.everyday_keep_days)
        stats = {"ingest": s1, "cluster": s2, "applied": applied, "trigger": trigger}
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
        _lock.release()


scheduler = BackgroundScheduler()


@app.on_event("startup")
def _start():
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


@app.exception_handler(mover.DryRun)
async def _dry_run_refused(request: Request, exc: mover.DryRun):
    """Any request that would move a file while dry-run is on ends here, never in a move."""
    return HTMLResponse(f"<h1>Dry-run is on</h1><p>{exc}</p><p><a href='/review'>Review</a> · "
                        f"<a href='/settings'>Settings</a></p>", status_code=409)


# --- helpers --------------------------------------------------------------------

def render(request: Request, name: str, **ctx):
    cfg = config.load()
    ctx.update(request=request, cfg=cfg, state=_state, page=name.split(".")[0], v=STATIC_VERSION)
    return tpl.TemplateResponse(request, name, ctx)


def _pending(props: dict) -> list[dict]:
    return sorted((p for p in props.values() if p["status"] in ("pending", "ongoing")),
                  key=lambda p: p["start"], reverse=True)


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
                  n_approved=len(approved_all))


def _month_key(ts: str) -> str:
    return ts[:7]


@app.get("/everyday", response_class=HTMLResponse)
def everyday(request: Request, month: str = ""):
    cfg = config.load()
    props = cluster.load_proposals()
    recs = cluster.everyday_records(cfg, props)
    months_c = Counter(_month_key(r["ts"]) for r in recs)
    months = [{"key": k, "n": months_c[k]} for k in sorted(months_c)]
    if not month and months:
        month = months[-1]["key"]
    keys = [m["key"] for m in months]
    idx = keys.index(month) if month in keys else -1
    prev_month = keys[idx - 1] if idx > 0 else None
    next_month = keys[idx + 1] if 0 <= idx < len(keys) - 1 else None
    days: dict[str, list] = {}
    for r in recs:
        if _month_key(r["ts"]) == month:
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
    movable = len(mover.everyday_movable(cfg, cfg.everyday_keep_days))
    return render(request, "everyday.html", months=months, month=month, days=out, pending=_pending(props),
                  total=len(recs), prev_month=prev_month, next_month=next_month,
                  month_n=months_c.get(month, 0), movable=movable, moving=_applying.get(EVERYDAY_JOB),
                  queued=EVERYDAY_JOB in list(_apply_queue.queue))


@app.post("/everyday/move_all")
def everyday_move_all():
    """Move every everyday photo older than everyday_keep_days into YYYY/MM, in the background."""
    cfg = config.load()
    if cfg.dry_run:
        raise mover.DryRun("dry-run is on: everyday photos are not moved (switch it off in Settings)")
    if EVERYDAY_JOB not in _applying and EVERYDAY_JOB not in list(_apply_queue.queue):
        queue_apply(EVERYDAY_JOB)
    return RedirectResponse("/everyday", status_code=303)


@app.post("/everyday/assign")
async def everyday_assign(request: Request):
    """Ticked everyday photos join an existing proposal or form a new manual one."""
    cfg = config.load()
    form = await request.form()
    paths = [str(p) for p in form.getlist("paths")]
    target, kind, name = form.get("target", "new"), form.get("kind", "local"), str(form.get("name", ""))
    with _props_lock:
        props = cluster.load_proposals()
        wanted = set(paths)
        recs = [r for r in cluster.everyday_records(cfg, props) if r["path"] in wanted]
        if not recs:
            return RedirectResponse("/everyday", status_code=303)
        if target == "new":
            pr = cluster.create_manual(kind, name, recs)
            props[pr["id"]] = pr
            events.log("review", proposal=pr["id"], action="create", name=pr["name"], cluster_kind=kind, n=len(recs))
        else:
            pr = props.get(target)
            if not pr or pr["status"] not in ("pending", "ongoing", "approved"):
                return RedirectResponse("/everyday", status_code=303)
            n = cluster.add_to_proposal(pr, recs)
            events.log("review", proposal=pr["id"], action="add", name=pr["name"], n=n)
        cluster.save_proposals(props)
    return RedirectResponse(f"/review?open={pr['id']}#{pr['id']}", status_code=303)


@app.get("/clusters", response_class=HTMLResponse)
def clusters(request: Request):
    cfg = config.load()
    return render(request, "clusters.html", clusters=mover.list_clusters(cfg))


@app.get("/clusters/view", response_class=HTMLResponse)
def cluster_view(request: Request, folder: str):
    m = mover.read_manifest(Path(folder))
    return render(request, "cluster.html", m=m, folder=folder)


@app.get("/log", response_class=HTMLResponse)
def log_page(request: Request, kind: str = ""):
    return render(request, "log.html", rows=events.read(limit=300, kind=kind or None), kind=kind)


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request, msg: str = ""):
    cfg = config.load()
    return render(request, "settings.html", inboxes_text=config.inboxes_text(cfg),
                  discovered=config.discovered_inboxes(cfg), named_places_text=config.named_places_text(cfg),
                  extensions=", ".join(cfg.photo_extensions), msg=msg)


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
    return RedirectResponse("/settings?msg=" + quote(msg), status_code=303)


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
    cfg = config.update_from_form(config.load(), form)
    config.save(cfg)
    scheduler.reschedule_job("scan", trigger="interval", minutes=max(1, cfg.scan_interval_min))
    events.log("settings", changed=sorted(form.keys()), dry_run=cfg.dry_run)
    msg = ""
    if before.dry_run and not cfg.dry_run:
        n = queue_approved()                                   # they waited for exactly this
        msg = f"Dry-run is off. {n} approved proposal{'s' if n != 1 else ''} will be moved now."
    elif not before.dry_run and cfg.dry_run:
        msg = "Dry-run is on: nothing will be moved, copied or deleted."
    return RedirectResponse("/settings" + (f"?msg={quote(msg)}" if msg else ""), status_code=303)


@app.post("/run")
def run_now():
    threading.Thread(target=run_pipeline, args=("manual",), daemon=True).start()
    return RedirectResponse("/", status_code=303)


@app.post("/proposal/{pid}/{action}")
async def proposal_action(pid: str, action: str, request: Request):
    form = dict(await request.form())
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
    # approve/reject may carry the name field of the same card: an edit made right before the
    # click must not be lost to a race with the autosave request
    if action in ("approve", "reject") and form.get("name") is not None:
        _rename(cfg, pid, pr, str(form["name"]), bool(form.get("remember_place")))
    if action == "approve" and pr["status"] in ("pending", "ongoing"):
        pr["status"], pr["error"] = "approved", None  # the apply worker moves the files
        events.log("review", proposal=pid, action="approve", name=pr["name"])
        cluster.save_proposals(props)
        queue_apply(pid)
    elif action == "reject":
        pr["status"] = "rejected"
        events.log("review", proposal=pid, action="reject", name=pr["name"], cluster_kind=pr["kind"])
    elif action == "rename":
        _rename(cfg, pid, pr, str(form.get("name", pr["name"])), bool(form.get("remember_place")))
    elif action == "toggle":
        path = form.get("path", "")
        ex = set(pr.get("excluded", []))
        ex.symmetric_difference_update({path})
        pr["excluded"] = sorted(ex)
        events.log("review", proposal=pid, action="toggle", photo=Path(path).name,
                   excluded=path in ex, decision_id=pr["decision"].get("id"))
    cluster.save_proposals(props)
    if request.headers.get("x-requested-with") == "fetch":            # ui.js: no page reload
        return JSONResponse({"ok": True, "status": pr["status"], "name": pr["name"],
                             "excluded": form.get("path", "") in set(pr.get("excluded", []))})
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
    entry = mover.move_out(cfg, Path(folder), Path(photo))
    if entry:
        _set_excluded(Path(folder), entry["src"], True)
        with _lock:
            ingest.scan(cfg)                              # index the returned file now, not in 10 minutes
    return RedirectResponse(f"/clusters/view?folder={quote(folder)}", status_code=303)


@app.post("/cluster/put_back")
def cluster_put_back(folder: str = Form(...), src: str = Form(...)):
    cfg = config.load()
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
    roots = [Path(cfg.root)] + [folder for _, folder in config.inbox_dirs(cfg)]
    for r in roots:
        try:
            if rp.is_relative_to(r.resolve()):
                return True
        except OSError:
            continue
    return False


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
    cfg = config.load()
    props = cluster.load_proposals()
    approved = [p["id"] for p in props.values() if p["status"] == "approved"]
    now = time.time()
    applying = {k: {**v, "current": {**v["current"], "seconds": round(now - v["current"]["since"])}
                    if v.get("current") else None} for k, v in _applying.items()}
    return {"state": _state, "pending": len(_pending(props)), "dry_run": cfg.dry_run,
            "approved": len(approved), "applying": applying}
