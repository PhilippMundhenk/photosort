"""Edge cases and error paths that the feature tests do not reach: corrupt or vanished files,
tools that are missing or fail, races between the worker and the pages, odd inputs. Each test
names the behaviour it pins, not the line."""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from concurrent.futures.process import BrokenProcessPool
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import cluster, config, events, geo, ingest, main, mover, thumbs
from tests import synth
from tests.synth import LISBON, T0
from tests.test_cluster import h, rec


@pytest.fixture
def client(library):
    main._state.update({"last_run": None, "last_stats": {}, "running": False, "error": None, "progress": None,
                        "runs": 0})
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def _kind(kind: str) -> dict:
    return next(p for p in cluster.load_proposals().values() if p["kind"] == kind)


# --- proposals file ------------------------------------------------------------------------------

def test_unreadable_or_corrupt_proposals_file_reads_as_empty(data_dir, monkeypatch):
    cluster.PROPOSALS_PATH.write_text("{not json", encoding="utf-8")
    cluster._props_cache.update(stamp=None, text="", counts=None)
    assert cluster.load_proposals() == {}
    assert cluster.status_counts() == {"pending": 0, "approved": {}}
    real = Path.read_text

    def failing(self, *a, **k):
        if self.name == "proposals.json":
            raise OSError("gone")
        return real(self, *a, **k)
    cluster._props_cache.update(stamp=None, text="", counts=None)
    monkeypatch.setattr(Path, "read_text", failing)
    assert cluster.load_proposals() == {}
    monkeypatch.undo()
    cluster.save_proposals({"a": {"status": "pending", "photos": []}})
    real_stat = Path.stat

    def no_stat(self, *a, **k):
        if self.name == "proposals.json":
            raise OSError("no stat")
        return real_stat(self, *a, **k)
    monkeypatch.setattr(Path, "stat", no_stat)
    cluster.save_proposals({"b": {"status": "pending", "photos": []}})   # written, cache cannot be stamped
    assert cluster.load_proposals() == {}                                 # unreadable while stat fails
    monkeypatch.undo()
    assert set(cluster.load_proposals()) == {"b"}


# --- records --------------------------------------------------------------------------------------

def test_load_records_skips_photos_without_record_and_copied_originals(cfg, library):
    a, b = library.paths[0], library.paths[1]
    ingest.delete_sidecar(a, cfg)
    r = ingest.read_sidecar(b, cfg)
    r["copied_to"] = "/somewhere"
    ingest.write_sidecar(b, r, cfg)
    ingest.changed["all"] = True
    recs, _ = cluster.load_records(cfg)
    paths = {r["path"] for r in recs}
    assert str(a) not in paths and str(b) not in paths and len(recs) == len(library.paths) - 2


def test_same_area_needs_located_photos_on_both_sides():
    cfg = config.Config()
    a = [rec(T0, geo.ZONE_UNKNOWN), rec(T0 + h(1), geo.ZONE_AWAY, LISBON)]
    assert cluster._same_area(cfg, a, [rec(T0, geo.ZONE_UNKNOWN)]) is False        # b has no position
    assert cluster._same_area(cfg, a, [rec(T0 + h(2), geo.ZONE_AWAY, LISBON)]) is True   # a's GPS-less one skipped
    assert cluster.group_bursts(cfg, []) == []


def test_run_tolerates_applied_without_manifest_and_stale_additions(cfg, library):
    cluster.run(cfg)
    props = cluster.load_proposals()
    local = next(p for p in props.values() if p["kind"] == "local")
    local["status"] = "applied"                                            # says applied, but no folder/manifest
    props["zzz"] = dict(local, id="zzz", status="pending", kind="trip", added=[local["photos"][0]["path"]],
                        photos=[])                                           # a stale addition to a vanished proposal
    cluster.save_proposals(props)
    stats = cluster.run(cfg)
    again = cluster.load_proposals()
    assert again[local["id"]]["status"] == "applied" and "zzz" not in again
    assert stats["proposals"] >= 1


# --- config and geo ------------------------------------------------------------------------------

def test_named_places_parser_skips_malformed_lines():
    out = config.parse_named_places("Black Forest = 48.0, 8.2, 40\nBroken = abc, def\n= 1, 2\nHills = 47.5, 8.1")
    assert [p["name"] for p in out] == ["Black Forest", "Hills"] and out[1]["radius_km"] == 2.0


def test_geocoder_falls_back_to_the_public_api_and_handles_no_candidates(monkeypatch):
    import reverse_geocode
    geo._lookup.cache_clear()
    real_cls, calls = reverse_geocode.GeocodeData, []

    def flaky(*a, **k):                                    # the internals fail once; the public API still works
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("internals")
        return real_cls(*a, **k)
    monkeypatch.setattr(reverse_geocode, "GeocodeData", flaky)
    r = geo.reverse(config.Config(), 38.72, -9.14)
    assert r["country_code"] == "PT" and r["city"]                          # public API answered
    geo._lookup.cache_clear()

    class Empty:
        class _tree:
            @staticmethod
            def query(pts, k):
                return None, [[]]

            @staticmethod
            def query_ball_point(pts, r):
                return [[]]
        _locations, _countries = [], {}
    monkeypatch.setattr(reverse_geocode, "GeocodeData", lambda *a, **k: Empty())
    assert geo.reverse(config.Config(), 38.72, -9.14)["place"] == ""
    geo._lookup.cache_clear()


# --- ingest --------------------------------------------------------------------------------------

def test_record_housekeeping_survives_bad_files_and_missing_folders(cfg, library, tmp_path, monkeypatch):
    bad = ingest.INDEX_DIR / "junk.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{oops", encoding="utf-8")
    (Path(cfg.root) / "manifest.json").write_text("{}", encoding="utf-8")
    cfg.inboxes.append({"path": str(tmp_path / "missing"), "name": "gone"})
    plain = Path(cfg.inboxes[0]["path"]) / "norecord.jpg"                    # a photo nobody indexed yet
    plain.write_bytes(b"\xff\xd8x")
    st = ingest.migrate_sidecars(cfg)
    assert st["photos"] == len(library.paths) + 1 and st["moved"] == 0
    assert ingest.purge_sidecars(cfg, sorted_tree=False, orphans=False) == {"sorted": 0, "orphans": 0}
    real = Path.rmdir
    monkeypatch.setattr(Path, "rmdir", lambda self: (_ for _ in ()).throw(OSError("busy")))
    assert ingest.delete_sidecar(library.paths[0], cfg) is True              # pruning failure is swallowed
    monkeypatch.setattr(Path, "rmdir", real)


def test_exiftool_failures_and_odd_tags(monkeypatch, tmp_path):
    class R:
        returncode, stdout = 2, ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    assert ingest.exif_batch([tmp_path / "a.jpg"]) == {}
    from datetime import datetime
    assert ingest._gps_offset(datetime(2026, 6, 1, 12), {"GPSDateTime": "garbage"}) is None
    assert ingest._gps_offset(datetime(2026, 6, 1, 12), {}) is None
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("exiftool")))
    ingest.write_xmp_keywords_batch([(tmp_path / "a.jpg", ["zone/home"])])   # no exiftool: quietly nothing


# --- the apply worker ----------------------------------------------------------------------------

def test_everyday_job_respects_dry_run_and_reports_failures(client, library, monkeypatch):
    cfg = config.load()
    cfg.dry_run = True
    config.save(cfg)
    main._apply_everyday_job()                                               # dry-run: nothing, no event
    assert not [e for e in events.read() if e.get("action", "").startswith("move_everyday")]
    cfg.dry_run = False
    config.save(cfg)
    monkeypatch.setattr(mover, "apply_everyday", lambda *a, **k: (_ for _ in ()).throw(mover.DryRun("late")))
    main._apply_everyday_job()                                               # switched on meanwhile: silent
    assert not [e for e in events.read() if e.get("action", "").startswith("move_everyday")]
    monkeypatch.setattr(mover, "apply_everyday", lambda *a, **k: (_ for _ in ()).throw(OSError("share gone")))
    main._apply_everyday_job()
    ev = [e for e in events.read() if e.get("action") == "move_everyday_failed"]
    assert ev and "share gone" in ev[0]["error"] and main.EVERYDAY_JOB not in main._applying


def test_apply_one_ignores_unknown_and_late_dry_run_and_vanished_proposals(client, library, monkeypatch):
    cfg = config.load()
    cfg.dry_run = False
    config.save(cfg)
    cluster.run(cfg)
    main._apply_one("nope")                                                 # unknown id: nothing
    local = _kind("local")
    main._apply_one(local["id"])                                            # pending, not approved: nothing
    assert all(Path(p["path"]).exists() for p in local["photos"])
    props = cluster.load_proposals()
    props[local["id"]]["status"] = "approved"
    cluster.save_proposals(props)
    monkeypatch.setattr(mover, "apply", lambda *a, **k: (_ for _ in ()).throw(mover.DryRun("switched on")))
    main._apply_one(local["id"])
    assert cluster.load_proposals()[local["id"]]["status"] == "approved"    # untouched, waits

    def apply_and_vanish(cfg, pr, **k):
        cluster.save_proposals({})                                          # the file was rewritten meanwhile
        return {}
    monkeypatch.setattr(mover, "apply", apply_and_vanish)
    main._apply_one(local["id"])
    assert cluster.load_proposals() == {}                                   # nothing resurrected
    monkeypatch.setattr(main, "_apply_one", lambda pid: (_ for _ in ()).throw(RuntimeError("boom")))
    main.queue_apply("anything")
    main.wait_for_apply()
    assert any(t.name == "apply" and t.is_alive() for t in threading.enumerate())   # the worker survived


@pytest.fixture
def bare_client(photos):
    main._state.update({"last_run": None, "last_stats": {}, "running": False, "error": None, "progress": None,
                        "runs": 0})
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def test_pipeline_without_gps_photos_cannot_detect_home_and_survives_prefetch_errors(bare_client, monkeypatch):
    cfg = config.load()
    cfg.home_lat = cfg.home_lon = 0.0
    config.save(cfg)
    lib = synth.Library(cfg)
    lib.photo(Path(cfg.inboxes[0]["path"]), T0, None)                        # no GPS anywhere
    monkeypatch.setattr(thumbs, "prefetch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no thumbs")))
    stats = main.run_pipeline("test")
    assert "error" not in stats and "home_detected" not in stats["ingest"]
    assert config.load().home_lat == 0.0


def test_startup_writes_a_missing_config_and_warm_cache_swallows_errors(library, monkeypatch):
    cfg = config.load()
    cfg.dry_run = True
    config.save(cfg)
    config.CONFIG_PATH.unlink()
    with TestClient(main.app):
        assert config.CONFIG_PATH.exists()                                  # defaults written at startup
    monkeypatch.setattr(cluster, "records_filled", lambda c: (_ for _ in ()).throw(RuntimeError("disk")))
    main._warm_cache()                                                      # logged, not raised


# --- pages: odd inputs ----------------------------------------------------------------------------

def test_everyday_assign_to_a_dead_target_and_remember_place_without_points(client, library):
    cfg = config.load()
    cluster.run(cfg)
    local = _kind("local")
    props = cluster.load_proposals()
    props[local["id"]]["status"] = "rejected"
    cluster.save_proposals(props)
    r = client.post("/everyday/assign", data={"paths": [library.paths[0].as_posix()], "target": local["id"]})
    assert r.status_code == 303 and r.headers["location"] == "/everyday"
    r = client.post("/everyday/assign", data={"paths": ["/nowhere.jpg"], "target": "new", "name": "x"})
    assert r.status_code == 303 and r.headers["location"] == "/everyday"    # no matching record
    assert main._remember_place(cfg, "2026-06 Nowhere", [(None, None)]) is None


def test_cluster_rename_remembers_place_from_records_when_the_proposal_has_none(client, library):
    cfg = config.load()
    cfg.dry_run, cfg.sidecar_cleanup = False, "never"
    config.save(cfg)
    cluster.run(cfg)
    local = _kind("local")
    mover.apply(cfg, local, reviewed=True)
    props = cluster.load_proposals()
    for p in props[local["id"]]["photos"]:
        p.pop("lat", None)
        p.pop("lon", None)                                                  # an older proposal: no positions
    cluster.save_proposals(props)
    folder = mover.target_folder(cfg, local)
    r = client.post("/cluster/rename", data={"folder": str(folder), "name": "2026-06-27 Barock", "remember_place": "1"})
    assert r.status_code == 303
    assert any(p["name"] == "Barock" for p in config.load().named_places)  # positions came from the records


def test_move_out_and_put_back_odd_cases(client, library):
    cfg = config.load()
    cfg.dry_run = False
    config.save(cfg)
    cluster.run(cfg)
    local = _kind("local")
    mover.apply(cfg, local, reviewed=True)
    folder = mover.target_folder(cfg, local)
    r = client.post("/cluster/move_out", data={"folder": str(folder), "photo": str(folder / "not-there.jpg")})
    assert r.status_code == 303                                             # nothing to move: back to the page
    stray = folder / "stray.jpg"                                            # a file nobody moved there
    stray.write_bytes(b"\xff\xd8x")
    entry = mover.move_out(cfg, folder, stray)
    assert entry is None and not stray.exists()                             # sent to the first inbox anyway
    assert mover.put_back(cfg, Path(cfg.root) / "no-such-folder", stray) is None
    assert mover.put_back(cfg, folder, Path("/never/corrected.jpg")) is None
    m = mover.read_manifest(folder)
    m["proposal_id"] = "unknown"
    mover.write_manifest(folder, m)
    dst = Path(m["photos"][0]["dst"])
    r = client.post("/cluster/move_out", data={"folder": str(folder), "photo": str(dst)})
    assert r.status_code == 303 and Path(m["photos"][0]["src"]).exists()   # moved out; no proposal to update


def test_thumb_and_media_endpoints_handle_unreadable_and_huge_files(client, library, tmp_path, monkeypatch):
    photo = library.paths[0]
    nas = photo.parent / "@eaDir" / photo.name / "SYNOPHOTO_THUMB_M.jpg"
    nas.parent.mkdir(parents=True)
    nas.write_bytes(b"\0" * 3_100_000)                                       # a "thumbnail" too big to inline
    r = client.get("/thumb", params={"path": str(photo)})
    assert r.headers["content-type"].startswith("image/svg+xml")
    nas.write_bytes(b"THUMB")
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes",
                        lambda self: (_ for _ in ()).throw(OSError("io")) if self == nas else real(self))
    assert client.get("/thumb", params={"path": str(photo)}).headers["content-type"].startswith("image/svg+xml")
    monkeypatch.undo()
    real_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: (_ for _ in ()).throw(OSError("loop")))
    assert client.get("/thumb", params={"path": str(photo)}).headers["content-type"].startswith("image/svg+xml")
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: real_resolve(self) if str(self) == str(photo)
                        else (_ for _ in ()).throw(OSError("root")))
    assert client.get("/thumb", params={"path": str(photo)}).headers["content-type"].startswith("image/svg+xml")
    monkeypatch.undo()
    heic = Path(config.load().inboxes[0]["path"]) / "shot.heic"
    heic.write_bytes(b"not a heic")
    assert client.get("/media", params={"path": str(heic)}).status_code == 404   # no preview possible


# --- mover odds and ends --------------------------------------------------------------------------

def test_transfer_verifies_the_destination_and_current_tolerates_missing_files(cfg, library, monkeypatch):
    assert mover._current(Path("/no/such/file.jpg"))["bytes"] == 0
    src = library.paths[0]
    monkeypatch.setattr(shutil, "move", lambda a, b: None)                  # a move that does nothing
    with pytest.raises(OSError, match="missing or incomplete"):
        mover._transfer(cfg, src, Path(cfg.root) / "x")
    assert src.exists()
    mover._mark_source(cfg, Path("/no/record.jpg"), None, None)            # no record: nothing to mark


def test_apply_failure_on_the_first_photo_writes_no_manifest(cfg, library, monkeypatch):
    cluster.run(cfg)
    local = _kind("local")
    monkeypatch.setattr(mover, "_transfer", lambda *a, **k: (_ for _ in ()).throw(OSError("share")))
    with pytest.raises(OSError):
        mover.apply(cfg, local, reviewed=True)
    assert not (mover.target_folder(cfg, local) / mover.MANIFEST).exists()


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")
def test_apply_writes_xmp_keywords_for_the_whole_cluster_at_once(cfg, library):
    cfg.write_xmp_sidecar = True
    cluster.run(cfg)
    local = _kind("local")
    seen = []
    mover.apply(cfg, local, reviewed=True, progress=lambda d, t, c: seen.append(c))
    folder = mover.target_folder(cfg, local)
    xmps = list(folder.rglob("*.xmp"))
    assert len(xmps) == local["n"]
    assert any(c and "keywords for" in c["file"] for c in seen)            # the progress line said so


def test_everyday_layout_leave_and_no_source_subfolders(cfg, library):
    cluster.run(cfg)
    cfg.everyday_layout = "leave"
    assert mover.everyday_movable(cfg, 0) == [] and mover.apply_everyday(cfg, 0) == 0
    cfg.everyday_layout, cfg.subfolder_by_source = "YYYY/MM", False
    n = mover.apply_everyday(cfg, 0)
    assert n == 10 and (Path(cfg.root) / "2026" / "06").is_dir()
    assert not any(p.is_dir() for p in (Path(cfg.root) / "2026" / "06").iterdir())   # flat: no phone-a/


def test_undo_skips_vanished_files_and_leaves_stray_files_alone(cfg, library):
    cluster.run(cfg)
    local = _kind("local")
    mover.apply(cfg, local, reviewed=True)
    folder = mover.target_folder(cfg, local)
    m = mover.read_manifest(folder)
    Path(m["photos"][0]["dst"]).unlink()                                    # one photo deleted by hand
    (folder / "notes.txt").write_text("keep", encoding="utf-8")            # a stray file
    assert mover.undo(cfg, folder) == local["n"] - 1
    assert (folder / "notes.txt").exists()                                  # folder kept because of it


def test_rename_without_manifest_and_folder_in_use(cfg, tmp_path, monkeypatch):
    plain = Path(cfg.root) / "2026-01 Plain"
    plain.mkdir(parents=True)
    dst = mover.rename(cfg, plain, "2026-01 Renamed")
    assert dst.is_dir() and not plain.exists()
    monkeypatch.setattr(mover.time, "sleep", lambda s: None)
    calls = []
    real = Path.rename

    def busy(self, target):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("in use")
        return real(self, target)
    monkeypatch.setattr(Path, "rename", busy)
    assert mover.rename(cfg, dst, "2026-01 Third").is_dir() and len(calls) == 3   # retried, then fine
    monkeypatch.setattr(Path, "rename", lambda self, t: (_ for _ in ()).throw(PermissionError("stuck")))
    with pytest.raises(mover.FolderInUse):
        mover.rename(cfg, Path(cfg.root) / "2026-01 Third", "2026-01 Fourth")


def test_cluster_list_ignores_nas_folders_and_broken_manifests(cfg):
    root = Path(cfg.root)
    (root / "@eaDir" / "x").mkdir(parents=True)
    (root / "@eaDir" / "x" / mover.MANIFEST).write_text("{}", encoding="utf-8")
    (root / "bad").mkdir()
    (root / "bad" / mover.MANIFEST).write_text("{not json", encoding="utf-8")
    mover.invalidate_clusters()
    assert mover.list_clusters(cfg) == []
    assert mover.cross_mount_note(config.Config(**{**cfg.as_dict(), "root": str(root / "absent")})) is None


def test_cross_mount_note_swallows_stat_errors(cfg, monkeypatch):
    mover._mount_cache["at"] = 0.0
    monkeypatch.setattr(Path, "stat", lambda self, *a, **k: (_ for _ in ()).throw(OSError("stat")))
    assert mover.cross_mount_note(cfg) is None


# --- thumbnails ----------------------------------------------------------------------------------

def test_helper_timeouts_and_broken_pools_are_retried_once_then_give_up(cfg, tmp_path, monkeypatch):
    src = tmp_path / "a.jpg"
    Image.new("RGB", (20, 20)).save(src, "JPEG")
    monkeypatch.setattr(thumbs, "IN_PROCESS", False)

    class Fut:
        def __init__(self, exc):
            self.exc = exc

        def result(self, timeout=None):
            raise self.exc

    class Pool:
        def __init__(self, excs):
            self.excs, self._processes = list(excs), {}

        def submit(self, fn, *a):
            exc = self.excs.pop(0)
            if exc is None:
                return type("Ok", (), {"result": lambda s, timeout=None: fn(*a)})()
            return Fut(exc)

        def shutdown(self, **k):
            pass
    pools = [Pool([FutureTimeout()])]
    monkeypatch.setattr(thumbs, "_helper", lambda: pools[0])
    assert thumbs._run_generate(src, thumbs.cache_path(src), thumbs.THUMB_SIZE) is None   # timeout: no retry
    pools[0] = Pool([BrokenProcessPool(), BrokenProcessPool()])
    assert thumbs._run_generate(src, thumbs.cache_path(src), thumbs.THUMB_SIZE) is None   # twice: give up
    pools[0] = Pool([BrokenProcessPool(), None])
    assert thumbs._run_generate(src, thumbs.cache_path(src), thumbs.THUMB_SIZE) == thumbs.cache_path(src)


def test_thumbnail_in_flight_and_unwritable_marker(cfg, tmp_path, monkeypatch):
    src = tmp_path / "b.jpg"
    Image.new("RGB", (20, 20)).save(src, "JPEG")
    thumbs._inflight.add(f"t:{src}")
    assert thumbs.get(cfg, src) is None                                     # someone else is on it
    thumbs._inflight.discard(f"t:{src}")
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"nope")
    real = Path.write_text
    monkeypatch.setattr(Path, "write_text", lambda self, *a, **k: (_ for _ in ()).throw(OSError("ro"))
                        if self.suffix == ".none" else real(self, *a, **k))
    assert thumbs.get(cfg, bad) is None and not thumbs._failed_marker(thumbs.cache_path(bad)).exists()


def test_prefetch_yields_while_pages_load_thumbnails(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(thumbs, "YIELD_S", 0.6)
    src = tmp_path / "c.jpg"
    Image.new("RGB", (20, 20)).save(src, "JPEG")
    thumbs.touch()
    t0 = time.time()
    thumbs.prefetch(cfg, [src])
    thumbs.wait()
    assert thumbs.cache_path(src).exists() and time.time() - t0 >= 0.3      # it waited for the page first


def test_everyday_move_button_while_a_job_is_already_queued(client, library):
    cfg = config.load()
    cfg.dry_run = False
    config.save(cfg)
    main._apply_queue.put(main.EVERYDAY_JOB)                                # already waiting
    r = client.post("/everyday/move_all")
    assert r.status_code == 303
    assert list(main._apply_queue.queue).count(main.EVERYDAY_JOB) <= 1      # not queued twice
    main.wait_for_apply()


# --- the last corners -----------------------------------------------------------------------------

def test_helper_limits_and_missing_tools(monkeypatch, tmp_path):
    thumbs._limit_memory()                                                  # runs in the helper normally
    thumbs._unlimited()                                                     # and in its ffmpeg/exiftool children
    assert thumbs._stamp(Path("/no/such/file")) == ""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert thumbs._raw_preview(tmp_path / "x.dng") is None                  # no exiftool
    assert thumbs._video_frame(tmp_path / "x.mp4", tmp_path / "t.jpg", 440) is False   # no ffmpeg


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_broken_video_gives_no_frame(cfg, tmp_path):
    clip = tmp_path / "broken.mp4"
    clip.write_bytes(b"\0\0\0\x18ftypisom" + b"\0" * 100)
    assert thumbs.get(cfg, clip) is None                                    # both seeks failed


def test_raw_preview_in_an_odd_mode_is_converted(cfg, tmp_path, monkeypatch):
    raw = tmp_path / "shot.dng"
    raw.write_bytes(b"II*\0")
    monkeypatch.setattr(thumbs, "_raw_preview", lambda src: Image.new("RGBA", (600, 400), (1, 2, 3, 4)))
    t = thumbs.get(cfg, raw)
    with Image.open(t) as im:
        assert im.mode == "RGB" and im.size == (440, 293)


def test_index_housekeeping_with_non_record_json_and_a_busy_folder(cfg, library, monkeypatch):
    (ingest.INDEX_DIR / "list.json").parent.mkdir(parents=True, exist_ok=True)
    (ingest.INDEX_DIR / "list.json").write_text("[1, 2]", encoding="utf-8")   # valid JSON, not a record
    assert ingest.migrate_sidecars(cfg)["moved"] == 0
    sub = Path(cfg.inboxes[0]["path"]) / "alone"
    lonely = library.photo(sub, T0 + timedelta(days=100), synth.HOME)         # a record in its own folder
    monkeypatch.setattr(Path, "rmdir", lambda self: (_ for _ in ()).throw(OSError("busy")))
    assert ingest.delete_sidecar(lonely, cfg) is True                       # the empty folder stays, no error
    assert ingest.sidecar_path(lonely, cfg).parent.exists()


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")
def test_apply_with_keywords_and_without_progress(cfg, library):
    cfg.write_xmp_sidecar = True
    cluster.run(cfg)
    local = _kind("local")
    mover.apply(cfg, local, reviewed=True)
    assert len(list(mover.target_folder(cfg, local).rglob("*.xmp"))) == local["n"]


def test_resource_limits_cope_with_finite_hard_limits_and_refusals(monkeypatch):
    import resource
    calls = []
    monkeypatch.setattr(resource, "getrlimit", lambda kind: (10 * 1024 ** 2, 20 * 1024 ** 2))   # a finite hard cap
    monkeypatch.setattr(resource, "setrlimit", lambda kind, lim: calls.append(lim))
    thumbs._limit_memory()
    assert calls == [(20 * 1024 ** 2, 20 * 1024 ** 2)]                       # never above the hard cap
    monkeypatch.setattr(resource, "setrlimit", lambda kind, lim: (_ for _ in ()).throw(ValueError("not permitted")))
    thumbs._limit_memory()                                                  # refused: the helper runs uncapped
    thumbs._unlimited()


def test_geocoder_gives_nothing_when_both_apis_fail(monkeypatch):
    import reverse_geocode
    geo._lookup.cache_clear()
    monkeypatch.setattr(reverse_geocode, "GeocodeData", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(reverse_geocode, "get", lambda *a, **k: (_ for _ in ()).throw(ValueError("y")))
    assert geo.reverse(config.Config(), 38.72, -9.14) == {"city": "", "region": "", "place": "", "country": "",
                                                          "country_code": ""}
    geo._lookup.cache_clear()


def test_apply_records_files_already_in_the_folder_and_skips_vanished_ones(cfg, library):
    """A continuation after a crash finds files in the folder without a manifest entry and
    records them; a source that vanished from the inbox and is not in the folder is skipped."""
    cluster.run(cfg)
    local = _kind("local")
    folder = mover.target_folder(cfg, local)
    first, second = Path(local["photos"][0]["path"]), Path(local["photos"][1]["path"])
    dest = folder / local["photos"][0]["source"] / first.name                    # "moved" before the crash
    dest.parent.mkdir(parents=True)
    first.rename(dest)
    second.unlink()                                                              # gone for good
    m = mover.apply(cfg, local, reviewed=True)
    assert len(m["photos"]) == local["n"] - 1
    assert str(dest) in {p["dst"] for p in m["photos"]}                          # recorded although not moved now
    assert str(second) not in {p["src"] for p in m["photos"]}
    assert mover.undo(cfg, folder) == local["n"] - 1 and first.exists()


def test_cluster_actions_refuse_folders_outside_the_sorted_root(client, library, tmp_path):
    """The forms name a folder; a request must not be able to rename, undo or empty anything but
    a cluster folder under the root (nor the root itself)."""
    cfg = config.load()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    inbox = Path(cfg.inboxes[0]["path"])
    for folder in (str(outside), cfg.root, str(inbox), str(Path(cfg.root) / ".." / "phone-a"), "/", "relative/x"):
        assert client.get("/clusters/view", params={"folder": folder}).status_code == 400, folder
        assert client.post("/cluster/rename", data={"folder": folder, "name": "gotcha"}).status_code == 400, folder
        assert client.post("/cluster/undo", data={"folder": folder}).status_code == 400, folder
        r = client.post("/cluster/move_out", data={"folder": folder, "photo": str(outside / "keep.txt")})
        assert r.status_code == 400, folder
        assert client.post("/cluster/put_back", data={"folder": folder, "src": "x"}).status_code == 400, folder
    assert (outside / "keep.txt").exists() and all(p.exists() for p in library.paths)
    cluster.run(cfg)
    local = _kind("local")
    mover.apply(cfg, local, reviewed=True)
    folder = mover.target_folder(cfg, local)
    r = client.post("/cluster/move_out", data={"folder": str(folder), "photo": str(library.paths[0])})
    assert r.status_code == 400 and library.paths[0].exists()                # a photo outside that folder
    assert client.get("/clusters/view", params={"folder": str(folder)}).status_code == 200


def test_a_folder_that_cannot_be_resolved_is_refused_too(client, library, monkeypatch):
    real = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: (_ for _ in ()).throw(OSError("loop"))
                        if "loop" in str(self) else real(self, *a, **k))
    assert client.get("/clusters/view", params={"folder": str(Path(config.load().root) / "loop")}).status_code == 400
