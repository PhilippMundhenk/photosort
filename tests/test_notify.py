"""The Matrix notifier (app/notify.py) against a fake homeserver: a real HTTP server in a thread
that answers the client-server API's send endpoint and records what it got."""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app import cluster, config, events, main, notify

TOKEN = "syt_secret_token_do_not_log"
ROOM = "!room42:example.org"


class FakeHomeserver:
    def __init__(self):
        self.requests: list[dict] = []
        self.status = 200
        seen, me = self.requests, self

        class Handler(BaseHTTPRequestHandler):
            def do_PUT(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                seen.append({"path": unquote(self.path), "auth": self.headers["Authorization"],
                             "body": json.loads(body)})
                self.send_response(me.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"event_id": "$e"}' if me.status == 200 else b'{"errcode": "M_FORBIDDEN"}')

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def bodies(self) -> list[str]:
        return [r["body"]["body"] for r in self.requests]


@pytest.fixture
def hs():
    s = FakeHomeserver()
    yield s
    s.server.shutdown()
    s.server.server_close()


def _enable(cfg, url, **kw):
    cfg.matrix_enabled, cfg.matrix_homeserver, cfg.matrix_token, cfg.matrix_room = True, url + "/", TOKEN, ROOM
    cfg.public_url = "http://nas.local:8080/"
    for k, v in kw.items():
        setattr(cfg, k, v)
    config.save(cfg)
    return cfg


def _pending() -> dict:
    return {pid: pr for pid, pr in cluster.load_proposals().items() if pr["status"] == "pending"}


def test_off_by_default_nothing_is_sent(library, hs):
    assert config.Config().matrix_enabled is False and not notify.configured(config.Config())
    cfg = config.load()
    cfg.matrix_homeserver, cfg.matrix_token, cfg.matrix_room = hs.url, TOKEN, ROOM   # all set, switch off
    config.save(cfg)
    stats = main.run_pipeline("test")
    assert "error" not in stats and "notified" not in stats and _pending()
    assert hs.requests == [] and not notify.NOTIFIED_PATH.exists()


def test_configured_needs_every_piece():
    cfg = config.Config(matrix_enabled=True, matrix_homeserver="https://m", matrix_token="t", matrix_room="!r:m")
    assert notify.configured(cfg)
    for k in ("matrix_homeserver", "matrix_token", "matrix_room"):
        assert not notify.configured(config.Config(**{**cfg.as_dict(), k: "  "}))
    assert notify.new_proposals(config.Config(), {"x": {"id": "x", "status": "pending"}}) == {"sent": 0, "failed": 0}


def test_each_new_proposal_is_announced_once(library, hs, caplog):
    _enable(config.load(), hs.url)
    caplog.set_level(logging.DEBUG)
    stats = main.run_pipeline("test")
    pending = _pending()
    assert len(pending) >= 2 and stats["notified"] == {"sent": len(pending), "failed": 0}
    assert len(hs.requests) == len(pending)
    for r in hs.requests:
        assert r["auth"] == f"Bearer {TOKEN}" and r["body"]["msgtype"] == "m.text"
        assert r["path"].startswith(f"/_matrix/client/v3/rooms/{ROOM}/send/m.room.message/photosort-")
    for pid, pr in pending.items():
        text = next(b for b in hs.bodies() if f"?open={pid}" in b)
        assert pr["name"] in text and f"{pr['n']} photos" in text
        assert f"http://nas.local:8080/review?open={pid}" in text
        assert notify.KIND_LABEL[pr["kind"]] in text
    assert set(json.loads(notify.NOTIFIED_PATH.read_text())) == set(pending)

    again = main.run_pipeline("test")                         # nothing new: nothing sent
    assert again["notified"] == {"sent": 0, "failed": 0} and len(hs.requests) == len(pending)
    assert TOKEN not in caplog.text and TOKEN not in events.EVENTS_PATH.read_text(encoding="utf-8")


def test_approved_and_auto_applied_proposals_are_not_announced(library, hs):
    cfg = _enable(config.load(), hs.url)
    cluster.run(cfg)
    props = cluster.load_proposals()
    pid = next(p for p, pr in props.items() if pr["status"] == "pending")
    props[pid]["status"] = "approved"
    cluster.save_proposals(props)
    out = notify.new_proposals(cfg, props)
    assert out["sent"] == len(hs.requests) and not any(f"?open={pid}" in b for b in hs.bodies())


def test_many_new_proposals_become_one_summary(library, hs, monkeypatch):
    monkeypatch.setattr(notify, "MAX_SINGLE", 1)
    _enable(config.load(), hs.url, public_url="")
    stats = main.run_pipeline("test")
    n = len(_pending())
    assert stats["notified"] == {"sent": 1, "failed": 0} and len(hs.requests) == 1
    assert hs.bodies() == [f"{n} new proposals waiting in Review: /review"]
    assert "/send/m.room.message/photosort-summary-" in hs.requests[0]["path"]
    assert main.run_pipeline("test")["notified"]["sent"] == 0 and len(hs.requests) == 1


def test_refused_message_is_logged_without_token_and_retried(library, hs, caplog):
    _enable(config.load(), hs.url)
    hs.status = 403
    caplog.set_level(logging.WARNING, logger="photosort.notify")
    stats = main.run_pipeline("test")
    n = len(_pending())
    assert "error" not in stats and stats["notified"] == {"sent": 0, "failed": n}
    assert "HTTP 403" in caplog.text and TOKEN not in caplog.text
    assert json.loads(notify.NOTIFIED_PATH.read_text()) == []
    assert TOKEN not in events.EVENTS_PATH.read_text(encoding="utf-8")
    hs.status = 200                                           # the homeserver is back: the next run sends them
    assert main.run_pipeline("test")["notified"] == {"sent": n, "failed": 0}


def test_unreachable_homeserver_never_breaks_the_run(library, hs, caplog):
    url = hs.url
    hs.server.shutdown()
    hs.server.server_close()                                   # nobody listens there any more
    _enable(config.load(), url)
    caplog.set_level(logging.WARNING, logger="photosort.notify")
    stats = main.run_pipeline("test")
    assert "error" not in stats and stats["notified"]["sent"] == 0 and stats["notified"]["failed"] > 0
    assert "cannot reach the homeserver" in caplog.text and TOKEN not in caplog.text


def test_any_other_failure_is_contained(cfg, monkeypatch, caplog):
    cfg = _enable(cfg, "http://unused")
    pr = {"id": "t1", "kind": "trip", "name": "2026-06 Porto", "status": "pending", "photos": [{}]}
    caplog.set_level(logging.WARNING, logger="photosort.notify")

    def boom(*a, **k):
        raise ValueError(TOKEN)                                # even an error that carries the token
    monkeypatch.setattr(notify, "send", boom)
    assert notify.new_proposals(cfg, {"t1": pr}) == {"sent": 0, "failed": 1}
    assert "ValueError" in caplog.text and TOKEN not in caplog.text

    monkeypatch.setattr(notify, "send", lambda *a: None)
    monkeypatch.setattr(notify, "_save_notified", lambda ids: boom())
    assert notify.new_proposals(cfg, {"t1": pr}) == {"sent": 0, "failed": 1}   # the state file cannot be written
    assert TOKEN not in caplog.text


def test_notified_file_damaged_or_odd(data_dir):
    notify.NOTIFIED_PATH.write_text("{not json", encoding="utf-8")
    assert notify._load_notified() == set()
    notify.NOTIFIED_PATH.write_text('{"a": 1}', encoding="utf-8")
    assert notify._load_notified() == set()
    notify._save_notified({"b", "a"})
    assert notify._load_notified() == {"a", "b"}


def test_forgotten_proposals_leave_the_notified_file(cfg, monkeypatch):
    cfg = _enable(cfg, "http://unused")
    notify._save_notified({"gone", "t1"})
    sent = []
    monkeypatch.setattr(notify, "send", lambda c, text, txn: sent.append((text, txn)))
    pr = {"id": "t2", "kind": "odd", "name": "x", "status": "pending", "photos": [{}]}
    assert notify.new_proposals(cfg, {"t1": {"id": "t1", "status": "pending"}, "t2": pr})["sent"] == 1
    assert sent == [("New in Review: x (odd, 1 photo) http://nas.local:8080/review?open=t2", "photosort-t2")]
    assert notify._load_notified() == {"t1", "t2"}


def test_message_texts():
    cfg = config.Config()
    assert notify.review_link(cfg, "a b") == "/review?open=a%20b"
    assert notify.proposal_text(cfg, {"id": "p", "n": 0}) == "New in Review: p (proposal, 0 photos) /review?open=p"
    assert notify.proposal_text(cfg, {"id": "p", "kind": "local", "name": "Zoo", "n": 30}) == \
        "New in Review: Zoo (day out, 30 photos) /review?open=p"


def test_settings_offer_the_notifier_and_never_show_the_token(library):
    with TestClient(main.app, follow_redirects=False) as client:
        page = client.get("/settings?mode=advanced").text
        for name in ("matrix_enabled", "matrix_homeserver", "matrix_token", "matrix_room", "public_url"):
            assert f'name="{name}"' in page
        assert "Notifications" in page and "matrix_token_clear" not in page        # nothing saved yet
        assert 'data-level="advanced" hidden' in client.get("/settings").text      # basic mode hides the card

        def save(**kw):
            form = {k: str(v) for k, v in config.load().as_dict().items() if not isinstance(v, (bool, list))}
            form.update({"inboxes": config.inboxes_text(config.load()), "photo_extensions": "jpg", "dry_run": "on",
                         "matrix_token": ""}, **kw)
            assert client.post("/settings", data=form).status_code == 303
            return config.load()

        cfg = save(matrix_enabled="on", matrix_homeserver=" https://matrix.example.org ", matrix_token=TOKEN,
                   matrix_room=ROOM, public_url="http://nas:8080")
        assert cfg.matrix_enabled and cfg.matrix_homeserver == "https://matrix.example.org"
        assert cfg.matrix_token == TOKEN and cfg.matrix_room == ROOM and cfg.public_url == "http://nas:8080"
        page = client.get("/settings?mode=advanced").text
        assert TOKEN not in page and "saved, leave empty to keep it" in page and "matrix_token_clear" in page
        assert save(matrix_enabled="on").matrix_token == TOKEN                   # empty box: kept
        cfg = save(matrix_token_clear="on")
        assert cfg.matrix_token == "" and cfg.matrix_enabled is False             # forgotten; unticked: off
        assert TOKEN not in events.EVENTS_PATH.read_text(encoding="utf-8")
