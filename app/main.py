"""photosort web service: scheduler + review UI. Run: uvicorn app.main:app"""
from __future__ import annotations

import logging
import mimetypes
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import cluster, config, events, geo, ingest, mover, thumbs
from .kev import Decider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("photosort")

BASE = Path(__file__).parent
app = FastAPI(title="photosort")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
tpl = Jinja2Templates(directory=BASE / "templates")

_lock = threading.Lock()
_state = {"last_run": None, "last_stats": {}, "running": False, "error": None}


# --- pipeline -------------------------------------------------------------------

def run_pipeline(trigger: str = "schedule") -> dict:
    if not _lock.acquire(blocking=False):
        return {"skipped": "already running"}
    _state["running"], _state["error"] = True, None
    try:
        cfg = config.load()
        s1 = ingest.scan(cfg)
        s2 = cluster.run(cfg, Decider(cfg))
        if s1.get("new"):
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
            applied += mover.apply_everyday(cfg, min_age_days=cfg.trip_gap_days)
        stats = {"ingest": s1, "cluster": s2, "applied": applied, "trigger": trigger}
        _state["last_run"], _state["last_stats"] = datetime.now(timezone.utc).isoformat(timespec="seconds"), stats
        events.log("run", **{k: v for k, v in stats.items() if k != "ingest"}, new_photos=s1.get("new"))
        return stats
    except Exception as e:  # noqa: BLE001
        log.exception("pipeline failed")
        _state["error"] = str(e)
        return {"error": str(e)}
    finally:
        _state["running"] = False
        _lock.release()


scheduler = BackgroundScheduler()


@app.on_event("startup")
def _start():
    cfg = config.load()
    if not config.CONFIG_PATH.exists():
        config.save(cfg)
    scheduler.add_job(run_pipeline, "interval", minutes=max(1, cfg.scan_interval_min), id="scan",
                      replace_existing=True)
    scheduler.start()


@app.on_event("shutdown")
def _stop():
    scheduler.shutdown(wait=False)


# --- helpers --------------------------------------------------------------------

def render(request: Request, name: str, **ctx):
    cfg = config.load()
    ctx.update(request=request, cfg=cfg, state=_state, page=name.split(".")[0])
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
                  kev=Decider(cfg).status(), recent=events.read(limit=8))


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, open: str = ""):
    cfg = config.load()
    props = cluster.load_proposals()
    unnamed = [c for c in mover.list_clusters(cfg) if c["unnamed"]]
    return render(request, "review.html", pending=_pending(props), unnamed=unnamed, open_id=open)


def _month_key(ts: str) -> str:
    return ts[:7]


@app.get("/everyday", response_class=HTMLResponse)
def everyday(request: Request, month: str = ""):
    cfg = config.load()
    props = cluster.load_proposals()
    recs = cluster.everyday_records(cfg, props)
    months_c = Counter(_month_key(r["ts"]) for r in recs)
    months = [{"key": k, "n": months_c[k]} for k in sorted(months_c, reverse=True)]
    if not month and months:
        month = months[0]["key"]
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
    return render(request, "everyday.html", months=months, month=month, days=out, pending=_pending(props),
                  total=len(recs))


@app.post("/everyday/assign")
async def everyday_assign(request: Request):
    """Ticked everyday photos join an existing proposal or form a new manual one."""
    cfg = config.load()
    form = await request.form()
    paths = [str(p) for p in form.getlist("paths")]
    target, kind, name = form.get("target", "new"), form.get("kind", "local"), str(form.get("name", ""))
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
    return render(request, "log.html", rows=events.read(limit=300, kind=kind or None), kind=kind,
                  calibration=events.calibration())


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request, msg: str = ""):
    cfg = config.load()
    return render(request, "settings.html", inboxes_text=config.inboxes_text(cfg),
                  named_places_text=config.named_places_text(cfg),
                  extensions=", ".join(cfg.photo_extensions), msg=msg)


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
    cfg = config.update_from_form(config.load(), form)
    config.save(cfg)
    scheduler.reschedule_job("scan", trigger="interval", minutes=max(1, cfg.scan_interval_min))
    events.log("settings", changed=sorted(form.keys()))
    return RedirectResponse("/settings", status_code=303)


@app.post("/run")
def run_now():
    threading.Thread(target=run_pipeline, args=("manual",), daemon=True).start()
    return RedirectResponse("/", status_code=303)


@app.post("/proposal/{pid}/{action}")
async def proposal_action(pid: str, action: str, request: Request):
    cfg = config.load()
    props = cluster.load_proposals()
    pr = props.get(pid)
    if not pr:
        return RedirectResponse("/review", status_code=303)
    form = dict(await request.form())
    if action == "approve":
        mover.apply(cfg, pr, reviewed=True)          # a human looked at it: nothing goes to _review
        pr["status"] = "applied"
        events.log("review", proposal=pid, action="approve", name=pr["name"])
    elif action == "reject":
        pr["status"] = "rejected"
        events.log("review", proposal=pid, action="reject", name=pr["name"],
                   decision_id=pr["decision"].get("id"))
        if pr["decision"].get("by") == "kev":
            events.log("correction", decision_id=pr["decision"]["id"], note="proposal rejected")
    elif action == "rename":
        pr["name"], pr["name_edited"] = cluster.sanitize(form.get("name", pr["name"])), True
        events.log("review", proposal=pid, action="rename", name=pr["name"])
        if form.get("remember_place"):
            _remember_place(cfg, pr["name"], [(p.get("lat"), p.get("lon")) for p in pr["photos"]
                                              if p["path"] not in set(pr.get("excluded", []))])
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
    """'2026-06-01..04 Harz' -> named place 'Harz' covering the cluster's photos."""
    name = _DATE_PREFIX.sub("", folder_name).strip(" -_()")
    entry = geo.remember_place(cfg, name, points)
    if entry:
        config.save(cfg)
        events.log("settings", changed=["named_places"], place=entry)
    return entry


@app.post("/cluster/rename")
def cluster_rename(folder: str = Form(...), name: str = Form(...), remember_place: str = Form("")):
    cfg = config.load()
    dst = mover.rename(cfg, Path(folder), name)
    if remember_place:
        m = mover.read_manifest(dst) or {}
        pts = []
        for p in m.get("photos", []):
            rec = ingest.read_sidecar(Path(p["dst"])) or {}
            pts.append((rec.get("lat"), rec.get("lon")))
        _remember_place(cfg, dst.name, pts)
    return RedirectResponse(f"/clusters/view?folder={dst}", status_code=303)


@app.post("/cluster/undo")
def cluster_undo(folder: str = Form(...)):
    cfg = config.load()
    mover.undo(cfg, Path(folder))
    props = cluster.load_proposals()
    for pr in props.values():
        if pr["status"] == "applied" and mover.target_folder(cfg, pr) == Path(folder):
            pr["status"] = "rejected"
    cluster.save_proposals(props)
    return RedirectResponse("/clusters", status_code=303)


@app.post("/cluster/move_out")
def cluster_move_out(folder: str = Form(...), photo: str = Form(...)):
    cfg = config.load()
    mover.move_out(cfg, Path(folder), Path(photo))
    return RedirectResponse(f"/clusters/view?folder={folder}", status_code=303)


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
    return {"state": _state, "pending": len(_pending(props)), "kev": Decider(cfg).status(), "dry_run": cfg.dry_run}
