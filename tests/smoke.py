"""Smoke test with synthetic photos (no exiftool needed): writes fake JPGs + sidecars,
runs clustering, applies, undoes. Run: python -m tests.smoke"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="photosort-"))
os.environ["PHOTOSORT_DATA"] = str(tmp / "data")
os.environ.pop("PHOTOSORT_KEV_URL", None)

from app import cluster, config, mover  # noqa: E402
from app.kev import Decider  # noqa: E402
from tests import synth  # noqa: E402

cfg = synth.make_config(tmp)
config.save(cfg)
inboxA, inboxB, root = Path(cfg.inboxes[0]["path"]), Path(cfg.inboxes[1]["path"]), Path(cfg.root)
lib = synth.populate(cfg)
n = lib.n

stats = cluster.run(cfg, Decider(cfg))
props = cluster.load_proposals()
print("stats:", stats)
for p in props.values():
    print(f"  [{p['kind']:5}] {p['name']:45} n={p['n']:3} uncertain={p['n_uncertain']} status={p['status']} by={p['decision']['by']} conf={p['decision']['conf']}")

kinds = {p["kind"] for p in props.values()}
assert kinds == {"trip", "local", "home"}, kinds
trip = next(p for p in props.values() if p["kind"] == "trip")
assert "Lisbon" in trip["name"] and "Sevilla" in trip["name"], trip["name"]
assert trip["name"].count("Lisbon") == 1
assert trip["status"] == "pending"

m = mover.apply(cfg, trip)
folder = mover.target_folder(cfg, trip)
print("applied to", folder, "->", sorted(str(x.relative_to(folder)) for x in folder.rglob("*") if x.is_dir()))
assert (folder / "phone-a").exists() and (folder / "phone-b").exists()
assert not (folder / "phone-a" / "_review").exists()  # neighbour-GPS photos are trusted
assert (folder / "manifest.json").exists()

# rename an unnamed home burst after applying it
home = next(p for p in props.values() if p["kind"] == "home")
mover.apply(cfg, home)
hf = mover.target_folder(cfg, home)
new = mover.rename(cfg, hf, "2026-06-29 Hannas Geburtstag")
assert new.parent == root and new.exists(), new

# move one photo out, then undo the trip entirely
first = Path(m["photos"][0]["dst"])
mover.move_out(cfg, folder, first)
assert first.parent.name != "phone-a" or not first.exists()
undone = mover.undo(cfg, folder)
print("undo moved back", undone)
assert not folder.exists()
assert len(list(inboxA.rglob("*.jpg"))) + len(list(inboxB.rglob("*.jpg"))) == n - 25

ev = (tmp / "data" / "events.jsonl").read_text().splitlines()
print("events:", len(ev), "kinds:", sorted({json.loads(l)['kind'] for l in ev}))
shutil.rmtree(tmp)
print("SMOKE OK")
