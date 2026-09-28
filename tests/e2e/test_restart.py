"""The service is killed in the middle of moving a big trip (power cut, a container restart)
and started again with the same data: the move continues, every file ends up in the folder
exactly once, and the manifest knows all of them (it is written incrementally, so a crash
loses at most a handful of entries, which the continuation fills in)."""
from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from tests import synth
from tests.e2e.test_browser import _status, make_library, serve

pytestmark = pytest.mark.e2e


def _wait(cond, timeout: float = 120) -> None:
    t0 = time.time()
    while not cond():
        assert time.time() - t0 < timeout, "condition never became true"
        time.sleep(0.2)


def test_a_move_survives_a_kill_and_restart(tmp_path_factory):
    base, lib = make_library(tmp_path_factory, "e2e-restart")
    inbox = Path(lib.cfg.inboxes[0]["path"])
    day = synth.T0 + timedelta(days=3)
    for i in range(2500):                                                 # a trip big enough to take seconds
        lib.photo(inbox, day + timedelta(days=(i % 14), minutes=10 + (i // 14) * 7), synth.LISBON)
    data, cfg = base / "data", lib.cfg

    with serve(base, lib) as s:
        url = s["url"]
        httpx.post(url + "/run", timeout=5)
        _wait(lambda: _status(url)["pending"] == 3 and not _status(url)["state"]["running"])
        props = json.loads((data / "proposals.json").read_text(encoding="utf-8"))
        trip = next(p for p in props.values() if p["kind"] == "trip")
        assert trip["n"] > 2500
        httpx.post(url + f"/proposal/{trip['id']}/approve", data={"name": "2026-06 Portugal"}, timeout=10)
        _wait(lambda: (_status(url)["applying"].get(trip["id"]) or {}).get("done", 0) > 300)
        s["proc"].kill()                                                  # the plug is pulled mid-move
        s["proc"].wait(10)

    folder = Path(cfg.root) / "2026-06 Portugal"
    in_folder = {f.name for f in folder.rglob("*.jpg")}
    left = {Path(p["path"]).name for p in trip["photos"] if Path(p["path"]).exists()}
    assert 300 < len(in_folder) < trip["n"] and left                     # really interrupted
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["partial"] is True
    assert len(in_folder) - len(manifest["photos"]) < 25                # at most one batch unrecorded

    with serve(base, lib) as s:                                          # started again, same data
        url = s["url"]
        _wait(lambda: not _status(url)["applying"] and not _status(url)["queue"], 180)
        props = json.loads((data / "proposals.json").read_text(encoding="utf-8"))
        assert props[trip["id"]]["status"] == "applied"
        assert not any(Path(p["path"]).exists() for p in trip["photos"])   # all gone from the inbox
        names = [f.name for f in folder.rglob("*.jpg")]
        assert len(names) == trip["n"] and len(set(names)) == trip["n"]      # each file once, no _1 copies
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        assert "partial" not in manifest
        assert len(manifest["photos"]) == trip["n"]                          # the manifest knows every file
        assert len({p["dst"] for p in manifest["photos"]}) == trip["n"]
        r = httpx.get(url + "/clusters/view", params={"folder": str(folder)}, timeout=30)
        assert r.status_code == 200 and r.text.count("<figure") == trip["n"]
        httpx.post(url + "/cluster/undo", data={"folder": str(folder)}, timeout=120)   # and undo covers it all
        assert all(Path(p["path"]).exists() for p in trip["photos"])
        assert not folder.exists()
