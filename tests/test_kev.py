"""HttpBackend against a fake System One server (no model needed) + rule fallback.
Run: python -m tests.test_kev   (also works under pytest)"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("PHOTOSORT_DATA", tempfile.mkdtemp(prefix="photosort-test-"))

from app import config, events  # noqa: E402
from app.cluster import HOME_BURST_CRITERIA, HOME_BURST_INSTRUCTIONS, HOME_BURST_OPTIONS  # noqa: E402
from app.kev import Decider, HttpBackend  # noqa: E402

STATE = {"photos": 25, "duration_h": 3.6, "weekday": "Monday", "devices": 2, "burst_ratio": 2.08}


class FakeSystemOne(BaseHTTPRequestHandler):
    """Answers like laya-server / Kev: {"answers": {id: {...}}}. Records every request."""
    requests: list[dict] = []

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        body = b'{"status":"ok","model":"fake"}' if self.path in ("/", "/health") else b"{}"
        self.send_response(200 if self.path in ("/", "/health") else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        assert self.path == "/v1/systemone", self.path
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeSystemOne.requests.append(req)
        answers = {}
        for qid, q in req["questions"].items():
            if q["type"] == "choice":
                opts = list(q["criteria"])
                probs = {o: (0.83 if o == "occasion" else 0.17) for o in opts}
                answers[qid] = {"type": "choice", "choice": "occasion", "probabilities": probs, "confidence": 0.71}
            elif q["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": 0.9, "confidence": 0.9}
        out = json.dumps({"model": "fake", "answers": answers, "usage": {"input_tokens": 1, "output_tokens": 0}})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out.encode())


def _serve() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeSystemOne)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_wire_format_and_parsing():
    srv, url = _serve()
    try:
        cfg = config.Config(kev_url=url + "/", kev_model="laya")
        be = HttpBackend(cfg)
        assert be.url == url + "/v1/systemone" and be.base == url
        assert HttpBackend(config.Config(kev_url=url + "/v1/systemone")).url == url + "/v1/systemone"

        best, probs, conf = be.choice(STATE, HOME_BURST_OPTIONS, HOME_BURST_INSTRUCTIONS, HOME_BURST_CRITERIA)
        assert (best, conf) == ("occasion", 0.71) and probs == {"occasion": 0.83, "busy_day": 0.17}
        req = FakeSystemOne.requests[-1]
        q = req["questions"]["q"]
        assert req["model"] == "laya" and "photos: 25" in req["state"]
        assert q["type"] == "choice" and q["instructions"] == HOME_BURST_INSTRUCTIONS
        assert q["criteria"] == HOME_BURST_CRITERIA          # descriptions travel with the options

        p, c = be.noul(STATE, "Is this an occasion?")
        assert (p, c) == (0.9, 0.9) and FakeSystemOne.requests[-1]["questions"]["q"]["type"] == "noul"
        assert be.health()["ok"]

        d = Decider(cfg)
        assert d.status()["backend"] == "kev" and d.status()["ok"]
        dec = d.choice("home_burst", STATE, HOME_BURST_OPTIONS, HOME_BURST_INSTRUCTIONS, HOME_BURST_CRITERIA)
        assert dec["by"] == "kev" and dec["answer"] == "occasion"
        ev = events.read(limit=1, kind="decision")[0]
        assert ev["id"] == dec["id"] and ev["by"] == "kev" and "error" not in ev
    finally:
        srv.shutdown()


def test_fallback_to_rules_when_server_down():
    srv, url = _serve()
    srv.shutdown(); srv.server_close()                      # port is now closed
    cfg = config.Config(kev_url=url, kev_timeout_s=2)
    d = Decider(cfg)
    assert d.status()["ok"] is False
    dec = d.choice("home_burst", STATE, HOME_BURST_OPTIONS, HOME_BURST_INSTRUCTIONS, HOME_BURST_CRITERIA)
    assert dec["by"] == "rule" and dec["answer"] in HOME_BURST_OPTIONS
    ev = events.read(limit=1, kind="decision")[0]
    assert ev["fallback_from"] == "kev" and "error" in ev


def test_rule_backend_without_url():
    d = Decider(config.Config(kev_url=""))
    assert d.status() == {"backend": "rule", "ok": True, "detail": "no kev_url configured"}
    dec = d.choice("home_burst", STATE, HOME_BURST_OPTIONS)
    assert dec["by"] == "rule" and dec["answer"] == "occasion"   # 2 devices, ratio 2 -> occasion


if __name__ == "__main__":
    for fn in (test_wire_format_and_parsing, test_fallback_to_rules_when_server_down, test_rule_backend_without_url):
        fn()
        print("ok", fn.__name__)
    print("KEV TESTS OK")
