"""Where per-photo records live (central index vs beside the photo), the name pattern,
cleanup after sorting, migration between layouts, orphan purge, the Settings actions, and the
config cache that makes per-record config lookups cheap."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cluster, config, events, ingest, main, mover
from tests import synth


def _jpgs(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*.jpg") if p.is_file())


def _jsons(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*.json") if p.is_file())


# --- paths ---------------------------------------------------------------------------------

def test_central_path_mirrors_inbox_and_relative_path(cfg):
    inbox = Path(cfg.inboxes[0]["path"])
    p = inbox / "2026" / "01" / "IMG_1.jpg"
    sp = ingest.sidecar_path(p, cfg)
    assert sp == ingest.INDEX_DIR / "phone-a" / "2026" / "01" / "IMG_1.jpg.photosort.json"
    sorted_p = Path(cfg.root) / "2026-06 Lisbon" / "IMG_1.jpg"
    expected = ingest.INDEX_DIR / "sorted" / "2026-06 Lisbon" / "IMG_1.jpg.photosort.json"
    assert ingest.sidecar_path(sorted_p, cfg) == expected
    other = Path("/somewhere/else/x.jpg")
    sp = ingest.sidecar_path(other, cfg)
    assert sp.parent.parent == ingest.INDEX_DIR / "_other" and sp.name.endswith("_x.jpg.photosort.json")
    assert ingest.sidecar_path(other, cfg) == sp                       # stable


def test_beside_path_and_name_pattern(cfg, tmp_path):
    p = tmp_path / "IMG_1.JPG"
    cfg.sidecar_mode = "beside"
    assert ingest.sidecar_path(p, cfg) == tmp_path / "IMG_1.JPG.photosort.json"
    cfg.sidecar_name = ".{stem}.json"
    assert ingest.sidecar_path(p, cfg) == tmp_path / ".IMG_1.json"
    cfg.sidecar_name = "{stem}.{ext}.meta"
    assert ingest.sidecar_path(p, cfg) == tmp_path / "IMG_1.JPG.meta"
    cfg.sidecar_name = "{bogus}"                                        # bad pattern: default name
    assert ingest.sidecar_path(p, cfg) == tmp_path / "IMG_1.JPG.photosort.json"
    cfg.sidecar_name = "{name}"                                         # would overwrite the photo: default name
    assert ingest.sidecar_path(p, cfg) == tmp_path / "IMG_1.JPG.photosort.json"
    assert ingest.sidecar_path(p, cfg, mode="central").is_relative_to(ingest.INDEX_DIR)


def test_default_is_central_and_photo_folders_stay_clean(cfg, library):
    inbox = Path(cfg.inboxes[0]["path"])
    assert cfg.sidecar_mode == "central"
    assert not _jsons(inbox) and len(_jsons(ingest.INDEX_DIR / "phone-a")) == len(_jpgs(inbox))
    rec = ingest.read_sidecar(library.paths[0], cfg)
    assert rec["path"] == str(library.paths[0]) and rec["file"] == library.paths[0].name


def test_write_read_delete_move_copy(cfg, tmp_path):
    a, b = tmp_path / "a.jpg", tmp_path / "sub" / "b.jpg"
    assert ingest.read_sidecar(a, cfg) is None and not ingest.delete_sidecar(a, cfg)
    ingest.write_sidecar(a, {"ts": "x"}, cfg)
    assert ingest.read_sidecar(a, cfg) == {"ts": "x", "path": str(a)}
    assert ingest.move_sidecar(a, b, cfg)
    assert ingest.read_sidecar(a, cfg) is None and ingest.read_sidecar(b, cfg)["path"] == str(b)
    assert ingest.move_sidecar(b, a, cfg, keep_source=True)
    assert ingest.read_sidecar(a, cfg) and ingest.read_sidecar(b, cfg)
    assert not ingest.move_sidecar(tmp_path / "none.jpg", a, cfg)
    assert ingest.delete_sidecar(b, cfg) and ingest.read_sidecar(b, cfg) is None
    assert not ingest.sidecar_path(b, cfg).parent.exists()             # empty index folders are pruned


def test_scan_uses_the_configured_store(tmp_path, data_dir, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.jpg").write_bytes(b"x")
    monkeypatch.setattr(ingest, "exif_batch", lambda paths: {str(p): {"DateTimeOriginal": "2026:06:04 09:00:00"}
                                                            for p in paths})
    central = config.Config(inboxes=[{"path": str(inbox), "name": "cam"}], sidecar_mode="central")
    assert ingest.scan(central)["new"] == 1
    assert list(inbox.iterdir()) == [inbox / "a.jpg"] and (ingest.INDEX_DIR / "cam" / "a.jpg.photosort.json").exists()
    beside = config.Config(inboxes=[{"path": str(inbox), "name": "cam"}], sidecar_mode="beside")
    assert ingest.scan(beside)["new"] == 1                              # not found beside: indexed again there
    assert (inbox / "a.jpg.photosort.json").exists()
    assert ingest.scan(beside)["new"] == 0 and ingest.scan(central)["new"] == 0


# --- moving photos -------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["central", "beside"])
def test_record_follows_the_photo_and_is_dropped_after_sorting(cfg, mode):
    cfg.sidecar_mode, cfg.sidecar_cleanup, cfg.dry_run = mode, "after_move", False
    config.save(cfg)
    lib = synth.populate(cfg)
    cluster.run(cfg)
    props = cluster.load_proposals()
    local = next(p for p in props.values() if p["kind"] == "local")
    m = mover.apply(cfg, local, reviewed=True)
    src, dst = Path(m["photos"][0]["src"]), Path(m["photos"][0]["dst"])
    assert dst.exists() and not src.exists()
    assert ingest.read_sidecar(src, cfg) is None and ingest.read_sidecar(dst, cfg) is None
    assert all(p.name == "manifest.json" for p in _jsons(Path(cfg.root)))   # no records in the sorted tree
    # undo brings the photo back; it is re-indexed on the next scan
    assert mover.undo(cfg, mover.target_folder(cfg, local)) == local["n"]
    assert src.exists() and ingest.read_sidecar(src, cfg) is None
    assert len(cluster.load_records(cfg)[0]) == lib.n - local["n"]
    ingest_calls = []
    import app.ingest as ing
    real = ing.exif_batch
    ing.exif_batch = lambda paths: ingest_calls.append(len(paths)) or real(paths)
    try:
        ingest.scan(cfg)
    except ingest.ExifToolMissing:
        pass
    finally:
        ing.exif_batch = real
    assert [c for c in ingest_calls if c] == [local["n"]]              # one exiftool batch, only the returned photos


def test_cleanup_never_keeps_the_record_with_cluster_and_decision(cfg, library):
    cfg.sidecar_cleanup, cfg.dry_run = "never", False
    config.save(cfg)
    cluster.run(cfg)
    local = next(p for p in cluster.load_proposals().values() if p["kind"] == "local")
    m = mover.apply(cfg, local)
    rec = ingest.read_sidecar(Path(m["photos"][0]["dst"]), cfg)
    assert rec["cluster"] == local["name"] and rec["decision"]["kind"] == "local"
    assert ingest.read_sidecar(Path(m["photos"][0]["src"]), cfg) is None
    n = mover.apply_everyday(cfg, min_age_days=4)
    root = Path(cfg.root)
    moved = [p for p in _jpgs(root / "2026")]
    assert n == len(moved) and ingest.read_sidecar(moved[0], cfg)["cluster"] == "2026/06"


def test_copy_mode_keeps_source_record_in_central_index(cfg, library):
    cfg.copy_instead_of_move, cfg.dry_run = True, False
    config.save(cfg)
    cluster.run(cfg)
    local = next(p for p in cluster.load_proposals().values() if p["kind"] == "local")
    m = mover.apply(cfg, local)
    src, dst = Path(m["photos"][0]["src"]), Path(m["photos"][0]["dst"])
    assert src.exists() and dst.exists()
    assert ingest.read_sidecar(src, cfg)["copied_to"] == str(dst)
    assert ingest.read_sidecar(dst, cfg) is None                       # after_move: the copy has no record
    assert len(cluster.load_records(cfg)[0]) == library.n - local["n"]
    mover.undo(cfg, mover.target_folder(cfg, local))
    assert "copied_to" not in ingest.read_sidecar(src, cfg) and not dst.exists()


def test_rename_and_move_out_keep_records_in_sync(cfg, library):
    cfg.sidecar_cleanup, cfg.dry_run = "never", False
    config.save(cfg)
    cluster.run(cfg)
    home = next(p for p in cluster.load_proposals().values() if p["kind"] == "home")
    m = mover.apply(cfg, home)
    folder = mover.target_folder(cfg, home)
    new = mover.rename(cfg, folder, "2026-06-30 Geburtstag")
    m2 = mover.read_manifest(new)
    for p in m2["photos"]:
        assert ingest.read_sidecar(Path(p["dst"]), cfg)["cluster"] == "2026-06-30 Geburtstag"
    assert not (ingest.INDEX_DIR / "sorted" / "_unnamed").exists()   # old central records moved along
    photo = Path(m2["photos"][0]["dst"])
    mover.move_out(cfg, new, photo)
    back = Path(m["photos"][0]["inbox"]) / photo.name
    assert back.exists() and ingest.read_sidecar(back, cfg)["cluster"] == "2026-06-30 Geburtstag"
    assert ingest.read_sidecar(photo, cfg) is None


# --- migration & purge ----------------------------------------------------------------------

def test_migrate_between_layouts_and_name_patterns(cfg, library):
    inbox = Path(cfg.inboxes[0]["path"])
    n = len(_jpgs(inbox)) + len(_jpgs(Path(cfg.inboxes[1]["path"])))
    cfg.sidecar_mode = "beside"
    st = ingest.migrate_sidecars(cfg)
    assert st == {"moved": n, "kept": 0, "photos": n}
    assert len(_jsons(inbox)) == len(_jpgs(inbox)) and not _jsons(ingest.INDEX_DIR)
    assert ingest.migrate_sidecars(cfg) == {"moved": 0, "kept": n, "photos": n}
    cfg.sidecar_name = ".{stem}.json"                                  # renamed pattern, still beside
    assert ingest.migrate_sidecars(cfg)["moved"] == n
    assert (inbox / f".{library.paths[0].stem}.json").exists()
    assert not (inbox / (library.paths[0].name + ".photosort.json")).exists()
    cfg.sidecar_mode, cfg.sidecar_name = "central", "{name}.photosort.json"
    assert ingest.migrate_sidecars(cfg)["moved"] == n
    assert not _jsons(inbox) and len(_jsons(ingest.INDEX_DIR)) == n
    assert len(cluster.load_records(cfg)[0]) == n                      # nothing lost on the way


def test_the_first_scan_of_a_process_keeps_what_was_written_before_it(cfg, library, monkeypatch):
    """Records written before the first scan (an index run, a photo that came back) count as
    current: the first scan must not read every record again just to learn where they live;
    a switch of the store afterwards does forget them."""
    assert ingest.scan(cfg)["rezoned"] is True                          # the re-zone pass reads them once
    ingest._current_store = ()                                          # a fresh process, records written
    reads = []
    real = ingest.read_sidecar
    monkeypatch.setattr(ingest, "read_sidecar", lambda p, c=None: (reads.append(p), real(p, c))[1])
    assert ingest.scan(cfg)["new"] == 0
    assert not [p for p in reads if p in library.paths]                 # not one record re-read for "needs index"
    cfg.sidecar_mode = "beside"
    ingest.migrate_sidecars(cfg)
    reads.clear()
    assert ingest.scan(cfg)["new"] == 0
    assert len([p for p in reads if p in library.paths]) >= len(library.paths)   # elsewhere now: read again


def test_purge_sorted_tree_and_orphans(cfg, library):
    cfg.sidecar_cleanup, cfg.dry_run = "never", False
    config.save(cfg)
    cluster.run(cfg)
    local = next(p for p in cluster.load_proposals().values() if p["kind"] == "local")
    mover.apply(cfg, local)
    gone = library.paths[0]
    gone.unlink()                                                      # photo removed by another tool
    st = ingest.purge_sidecars(cfg)
    assert st == {"sorted": local["n"], "orphans": 1}
    assert ingest.read_sidecar(gone, cfg) is None
    assert ingest.purge_sidecars(cfg) == {"sorted": 0, "orphans": 0}
    assert len(cluster.load_records(cfg)[0]) == library.n - local["n"] - 1


def test_purge_ignores_unreadable_index_files(cfg, library):
    junk = ingest.INDEX_DIR / "phone-a" / "junk.json"
    junk.write_text("{not json", encoding="utf-8")
    (ingest.INDEX_DIR / "phone-a" / "note.txt").write_text("x", encoding="utf-8")
    assert ingest.purge_sidecars(cfg)["orphans"] == 0 and junk.exists()


# --- config cache ----------------------------------------------------------------------------

def test_config_load_is_cached_and_invalidated(data_dir):
    config.save(config.Config(home_lat=1.0))
    a = config.load()
    b = config.load()
    assert a == b and a is not b                                       # copies, never the shared object
    a.home_lat = 99.0
    assert config.load().home_lat == 1.0                               # mutation without save does not leak
    time.sleep(0.01)
    config.save(config.Config(home_lat=2.0))
    assert config.load().home_lat == 2.0
    config.CONFIG_PATH.write_text("home_lat: 3.0\n", encoding="utf-8")   # edited by hand: mtime/size changed
    assert config.load().home_lat == 3.0


# --- settings routes ---------------------------------------------------------------------------

@pytest.fixture
def client(library):
    main._state.update({"last_run": None, "last_stats": {}, "running": False, "error": None})
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def test_settings_sidecar_actions(client, cfg, library):
    page = client.get("/settings").text
    assert 'name="sidecar_mode"' in page and "Move existing records" in page
    r = client.post("/settings", data={"sidecar_mode": "beside", "sidecar_name": "{name}.photosort.json",
                                       "sidecar_cleanup": "never", "inboxes": config.inboxes_text(cfg),
                                       "root": cfg.root, "home_lat": "48.944", "home_lon": "9.118",
                                       "timezone": "UTC", "dry_run": "on"})
    assert r.status_code == 303 and config.load().sidecar_mode == "beside"
    r = client.post("/settings/sidecars/migrate")
    assert r.status_code == 303 and "moved%20to%20the%20beside" in r.headers["location"]
    assert "moved to the beside location" in client.get(r.headers["location"]).text
    assert _jsons(Path(cfg.inboxes[0]["path"]))
    library.paths[0].unlink()
    r = client.post("/settings/sidecars/purge")
    assert "Sidecars%20removed" in r.headers["location"]
    ev = events.read(limit=1, kind="settings")[0]
    assert ev["changed"] == ["sidecars:purge"] and ev["orphans"] == 0     # beside-mode records have no index
    assert client.post("/settings/sidecars/bogus").headers["location"] == "/settings"


def test_records_carry_their_path_for_orphan_detection(cfg, library):
    sp = ingest.sidecar_path(library.paths[3], cfg)
    assert json.loads(sp.read_text(encoding="utf-8"))["path"] == str(library.paths[3])
