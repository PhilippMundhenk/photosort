"""Matrix notifier (issue #8): after a run, one message per new proposal waiting in Review.

The review queue stays authoritative; the message only points at it. A proposal is announced
once: the ids already announced are kept in <data>/notified.json, and an id is added only after
the homeserver accepted the message, so a failed post is retried on the next run. Nothing here
may break a run: every failure is logged (without the access token) and the run goes on."""
from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.request
from urllib.parse import quote

from .config import DATA_DIR, Config

NOTIFIED_PATH = DATA_DIR / "notified.json"
TIMEOUT_S = 10.0
MAX_SINGLE = 5            # more new proposals than this in one run (the first run after switching it
                          # on, a big import): one summary message instead of a flood
KIND_LABEL = {"trip": "trip", "local": "day out", "home": "occasion at home"}

log = logging.getLogger("photosort.notify")


def configured(cfg: Config) -> bool:
    return bool(cfg.matrix_enabled and cfg.matrix_homeserver.strip() and cfg.matrix_token.strip()
                and cfg.matrix_room.strip())


def _load_notified() -> set[str]:
    try:
        data = json.loads(NOTIFIED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(x) for x in data} if isinstance(data, list) else set()


def _save_notified(ids: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = NOTIFIED_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(ids)), encoding="utf-8")
    tmp.replace(NOTIFIED_PATH)


def review_link(cfg: Config, pid: str | None = None) -> str:
    base = cfg.public_url.strip().rstrip("/")
    return f"{base}/review" + (f"?open={quote(pid, safe='')}" if pid else "")


def proposal_text(cfg: Config, pr: dict) -> str:
    n = pr.get("n", len(pr.get("photos", [])))
    kind = KIND_LABEL.get(pr.get("kind"), pr.get("kind") or "proposal")
    return (f"New in Review: {pr.get('name', pr['id'])} ({kind}, {n} photo{'s' if n != 1 else ''}) "
            f"{review_link(cfg, pr['id'])}")


def summary_text(cfg: Config, prs: list[dict]) -> str:
    return f"{len(prs)} new proposals waiting in Review: {review_link(cfg)}"


def send(cfg: Config, text: str, txn: str) -> None:
    """One m.room.message into the room. The transaction id makes a retry of the same message
    idempotent on the homeserver. Raises on any failure."""
    url = (f"{cfg.matrix_homeserver.strip().rstrip('/')}/_matrix/client/v3/rooms/"
           f"{quote(cfg.matrix_room.strip(), safe='')}/send/m.room.message/{quote(txn, safe='')}")
    body = json.dumps({"msgtype": "m.text", "body": text}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="PUT", headers={
        "Authorization": f"Bearer {cfg.matrix_token.strip()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        resp.read()


def _why(e: Exception) -> str:
    """The error without anything that could carry the token (the request, its headers)."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"cannot reach the homeserver ({type(e.reason).__name__})"
    return type(e).__name__


def new_proposals(cfg: Config, props: dict[str, dict]) -> dict:
    """Announce the pending proposals not announced before. Returns what happened, for the
    run's stats: {"sent": messages accepted, "failed": messages refused or lost}."""
    if not configured(cfg):
        return {"sent": 0, "failed": 0}
    try:
        notified = _load_notified()
        fresh = [pr for pid, pr in sorted(props.items()) if pr.get("status") == "pending" and pid not in notified]
        if not fresh:
            return {"sent": 0, "failed": 0}
        if len(fresh) > MAX_SINGLE:
            ids = [pr["id"] for pr in fresh]
            digest = hashlib.sha1(",".join(ids).encode("utf-8")).hexdigest()[:16]
            batches = [(summary_text(cfg, fresh), "photosort-summary-" + digest, ids)]
        else:
            batches = [(proposal_text(cfg, pr), "photosort-" + pr["id"], [pr["id"]]) for pr in fresh]
        sent = failed = 0
        for text, txn, ids in batches:
            try:
                send(cfg, text, txn)
            except Exception as e:  # noqa: BLE001
                failed += 1
                log.warning("Matrix notification failed: %s; retried on the next run", _why(e))
                continue
            sent += 1
            notified.update(ids)
        notified &= set(props)                     # forget ids that are gone, the file stays small
        _save_notified(notified)
        return {"sent": sent, "failed": failed}
    except Exception as e:  # noqa: BLE001
        log.warning("Matrix notifier: %s", _why(e))
        return {"sent": 0, "failed": 1}
