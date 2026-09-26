"""photosort web service: scheduler + review UI. Run: uvicorn app.main:app"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import cluster, config, events, ingest, mover
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
def review(request: Request):
    cfg = config.load()
    props = cluster.load_proposals()
    unnamed = [c for c in mover.list_clusters(cfg) if c["unnamed"]]
    return render(request, "review.html", pending=_pending(props), unnamed=unnamed)


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
def settings(request: Request):
    cfg = config.load()
    return render(request, "settings.html", inboxes_text=config.inboxes_text(cfg),
                  extensions=", ".join(cfg.photo_extensions))


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
    elif action == "toggle":
        path = form.get("path", "")
        ex = set(pr.get("excluded", []))
        ex.symmetric_difference_update({path})
        pr["excluded"] = sorted(ex)
        events.log("review", proposal=pid, action="toggle", photo=Path(path).name,
                   excluded=path in ex, decision_id=pr["decision"].get("id"))
    cluster.save_proposals(props)
    return RedirectResponse(form.get("back", "/review"), status_code=303)


@app.post("/cluster/rename")
def cluster_rename(folder: str = Form(...), name: str = Form(...)):
    cfg = config.load()
    dst = mover.rename(cfg, Path(folder), name)
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


@app.get("/thumb")
def thumb(path: str):
    """Serve the NAS thumbnail for a photo if one exists; the original is never decoded here."""
    cfg = config.load()
    p = Path(path)
    t = mover.thumb_for(cfg, p)
    if t:
        return FileResponse(t)
    if p.suffix.lower() in (".jpg", ".jpeg", ".png") and p.exists() and p.stat().st_size < 3_000_000:
        return FileResponse(p)  # small originals only
    return FileResponse(BASE / "static" / "nothumb.svg", media_type="image/svg+xml")


@app.get("/api/status")
def api_status():
    cfg = config.load()
    props = cluster.load_proposals()
    return {"state": _state, "pending": len(_pending(props)), "kev": Decider(cfg).status(), "dry_run": cfg.dry_run}
