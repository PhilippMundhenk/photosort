import json
import shutil
import struct
import subprocess
import zlib
from datetime import datetime
from pathlib import Path

import pytest

from app import config, geo, ingest
from tests.synth import HOME, LISBON

HAS_EXIFTOOL = shutil.which("exiftool") is not None


# --- timestamps -------------------------------------------------------------------

def test_parse_dt_plain_and_with_offset_tag():
    assert ingest._parse_dt({"DateTimeOriginal": "2026:10:03 14:22:31"})[0] == "2026-10-03T14:22:31"
    assert ingest._parse_dt({"DateTimeOriginal": "2026:10:03 14:22:31", "OffsetTimeOriginal": "+02:00"})[0] \
        == "2026-10-03T14:22:31+02:00"
    assert ingest._parse_dt({"CreateDate": "2026:10:03 14:22:31-05:00"})[0] == "2026-10-03T14:22:31-05:00"


def test_parse_dt_fallback_order_and_garbage():
    tags = {"DateTimeOriginal": "0000:00:00 00:00:00", "CreateDate": "not a date",
            "MediaCreateDate": "2026:01:02 03:04:05", "FileModifyDate": "2027:01:01 00:00:00"}
    assert ingest._parse_dt(tags)[0] == "2026-01-02T03:04:05"
    assert ingest._parse_dt({}) == (None, None)
    assert ingest._parse_dt({"FileModifyDate": ""}) == (None, None)


def test_scan_reindexes_records_of_an_older_version(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.jpg").write_bytes(b"x")
    cfg = config.Config(inboxes=[{"path": str(inbox), "name": "cam"}])
    monkeypatch.setattr(ingest, "exif_batch", lambda paths: {str(p): {"DateTimeOriginal": "2026:06:04 09:00:00"}
                                                            for p in paths})
    ingest.write_sidecar(inbox / "a.jpg", {"v": 1, "file": "a.jpg", "ts": "2026-06-04T09:00:00"}, cfg)
    assert ingest.scan(cfg)["new"] == 1                                    # v1 record: indexed again
    assert ingest.read_sidecar(inbox / "a.jpg", cfg)["v"] == ingest.SIDECAR_VERSION
    assert ingest.scan(cfg)["new"] == 0


# --- records & sidecars -----------------------------------------------------------

def test_build_record_with_and_without_gps(tmp_path):
    cfg = config.Config(home_lat=HOME[0], home_lon=HOME[1])
    p = tmp_path / "a.jpg"
    rec = ingest.build_record(cfg, p, {"DateTimeOriginal": "2026:06:04 09:00:00", "Make": "Apple",
                                       "Model": "iPhone", "GPSLatitude": LISBON[0], "GPSLongitude": LISBON[1]})
    assert rec["file"] == "a.jpg" and rec["ts"] == "2026-06-04T09:00:00" and rec["camera"] == "Apple iPhone"
    assert rec["gps_source"] == "exif" and rec["zone"] == geo.ZONE_AWAY and rec["dist_km"] > 1000
    assert rec["place"]["place"] == "Lisbon" and rec["v"] == ingest.SIDECAR_VERSION

    rec2 = ingest.build_record(cfg, p, {"Model": "cam"})
    assert rec2["ts"] is None and rec2["lat"] is None and rec2["zone"] == geo.ZONE_UNKNOWN
    assert rec2["place"] is None and rec2["gps_source"] is None and rec2["camera"] == "cam"


def test_malformed_gps_values_do_not_break_a_record(tmp_path, data_dir):
    cfg = config.Config(home_lat=HOME[0], home_lon=HOME[1])
    for lat, lon in (("", ""), ("n/a", "1"), (None, 9.1), (float("nan"), 9.1), ("48.9", "")):
        rec = ingest.build_record(cfg, tmp_path / "a.jpg", {"DateTimeOriginal": "2026:06:04 09:00:00",
                                                            "GPSLatitude": lat, "GPSLongitude": lon})
        assert rec["lat"] is None and rec["lon"] is None and rec["zone"] == geo.ZONE_UNKNOWN, (lat, lon)
    rec = ingest.build_record(cfg, tmp_path / "a.jpg", {"GPSLatitude": str(HOME[0]), "GPSLongitude": str(HOME[1])})
    assert rec["lat"] == HOME[0] and rec["zone"] == geo.ZONE_HOME                               # strings parse


def test_scan_survives_a_file_that_cannot_be_indexed(tmp_path, data_dir, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.jpg").write_bytes(b"x")
    (inbox / "b.jpg").write_bytes(b"x")
    cfg = config.Config(inboxes=[{"path": str(inbox), "name": "cam"}])
    monkeypatch.setattr(ingest, "exif_batch", lambda paths: {str(p): {"DateTimeOriginal": "2026:06:04 09:00:00"}
                                                            for p in paths})
    real = ingest.build_record

    def boom(cfg, photo, tags, source=None):
        if photo.name == "a.jpg":
            raise RuntimeError("odd file")
        return real(cfg, photo, tags, source=source)
    monkeypatch.setattr(ingest, "build_record", boom)
    stats = ingest.scan(cfg)
    assert stats["new"] == 1 and stats["failed"] == [str(inbox / "a.jpg")]
    assert ingest.read_sidecar(inbox / "b.jpg", cfg) and ingest.read_sidecar(inbox / "a.jpg", cfg) is None


def test_enrich_without_home_configured_gives_unknown_zone():
    rec = {"lat": LISBON[0], "lon": LISBON[1]}
    ingest.enrich_location(config.Config(), rec)
    assert rec["dist_km"] is None and rec["zone"] == geo.ZONE_UNKNOWN and rec["place"]["place"] == "Lisbon"


def test_sidecar_roundtrip_and_corruption(tmp_path, data_dir):
    cfg = config.Config(sidecar_mode="beside")
    p = tmp_path / "IMG.jpg"
    sp = ingest.sidecar_path(p, cfg)
    assert sp == tmp_path / "IMG.jpg.photosort.json"
    assert ingest.read_sidecar(p, cfg) is None
    ingest.write_sidecar(p, {"file": "IMG.jpg", "ts": None}, cfg)
    assert ingest.read_sidecar(p, cfg) == {"file": "IMG.jpg", "ts": None, "path": str(p)}
    assert not list(tmp_path.glob("*.tmp"))
    sp.write_text("{broken")
    assert ingest.read_sidecar(p, cfg) is None


def test_is_photo_and_list_photos(tmp_path):
    cfg = config.Config()
    for name in ("b.JPG", "a.heic", "c.txt", ".hidden.jpg", "sub/deep.dng", "@eaDir/b.JPG/SYNOPHOTO_THUMB_M.jpg",
                 "d.jpg.photosort.json"):
        f = tmp_path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    (tmp_path / "folder.jpg").mkdir()
    found = [str(p.relative_to(tmp_path)).replace("\\", "/") for p in ingest.list_photos(cfg, tmp_path)]
    assert found == ["a.heic", "b.JPG", "sub/deep.dng"]


# --- scan --------------------------------------------------------------------------

def test_scan_indexes_only_new_files_and_rezones(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    cfg = config.Config(inboxes=[{"path": str(inbox), "name": "cam"}], home_lat=HOME[0], home_lon=HOME[1])
    a, b = inbox / "a.jpg", inbox / "b.jpg"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    calls = []

    def fake_exif(paths):
        calls.append([p.name for p in paths])
        return {str(p): {"DateTimeOriginal": "2026:06:04 09:00:00", "GPSLatitude": HOME[0],
                         "GPSLongitude": HOME[1] + 0.05} for p in paths}
    monkeypatch.setattr(ingest, "exif_batch", fake_exif)

    stats = ingest.scan(cfg)
    assert stats == {"new": 2, "total": 2, "missing": [], "rezoned": True} and calls == [["a.jpg", "b.jpg"]]
    rec = ingest.read_sidecar(a, cfg)
    assert rec["zone"] == geo.ZONE_LOCAL and rec["source"] == "cam" and rec["inbox"] == str(inbox)

    (inbox / "c.jpg").write_bytes(b"x")
    stats = ingest.scan(cfg)
    assert stats["new"] == 1 and stats["total"] == 3 and calls[-1] == ["c.jpg"]

    # home moved onto the photos: existing sidecars are re-zoned without exiftool
    cfg.home_lon = HOME[1] + 0.05
    stats = ingest.scan(cfg)
    assert stats["new"] == 0 and len(calls) == 2                            # nothing new: exiftool not called
    assert ingest.read_sidecar(a, cfg)["zone"] == geo.ZONE_HOME


def test_scan_reports_progress_per_chunk(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for i in range(5):
        (inbox / f"{i}.jpg").write_bytes(b"x")
    cfg = config.Config(inboxes=[{"path": str(inbox), "name": "cam"}])
    monkeypatch.setattr(ingest, "exif_batch", lambda paths: {str(p): {"DateTimeOriginal": "2026:06:04 09:00:00"}
                                                            for p in paths})
    monkeypatch.setattr(ingest, "SCAN_CHUNK", 2)
    seen = []
    assert ingest.scan(cfg, progress=lambda d, t: seen.append((d, t)))["new"] == 5
    assert seen == [(0, 5), (2, 5), (4, 5), (5, 5)]
    seen.clear()
    ingest.scan(cfg, progress=lambda d, t: seen.append((d, t)))
    assert seen == [(0, 0)]                                                # nothing new: one call, done


def test_scan_reports_missing_inbox(tmp_path):
    cfg = config.Config(inboxes=[{"path": str(tmp_path / "nope"), "name": "x"}])
    st = ingest.scan(cfg)
    st.pop("rezoned")                                      # depends on what an earlier test left in the data dir
    assert st == {"new": 0, "total": 0, "missing": [str(tmp_path / "nope")]}


def test_scan_without_exiftool_leaves_photos_unindexed(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.jpg").write_bytes(b"x")
    cfg = config.Config(inboxes=[{"path": str(inbox), "name": "x"}])

    def boom(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(ingest.subprocess, "run", boom)
    stats = ingest.scan(cfg)
    assert stats["new"] == 0 and "exiftool" in stats["error"]
    assert ingest.read_sidecar(inbox / "a.jpg", cfg) is None  # no empty sidecar written


def test_exif_batch_empty_list_needs_no_exiftool():
    assert ingest.exif_batch([]) == {}
    assert ingest._name_dt("20260604_090000.jpg") == datetime(2026, 6, 4, 9, 0, 0)
    assert ingest._name_dt("random.jpg") is None


# --- real exiftool ------------------------------------------------------------------

def _png_1x1(path: Path) -> None:
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00\x00\x00")
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


@pytest.mark.skipif(not HAS_EXIFTOOL, reason="exiftool not installed")
def test_exif_batch_reads_real_tags(tmp_path):
    p = tmp_path / "shot.png"
    _png_1x1(p)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-DateTimeOriginal=2026:06:04 09:00:00",
                    "-OffsetTimeOriginal=+01:00", "-GPSLatitude=38.72", "-GPSLatitudeRef=N",
                    "-GPSLongitude=9.14", "-GPSLongitudeRef=W", "-Make=Test", "-Model=Cam", str(p)], check=True)
    tags = ingest.exif_batch([p])[str(p)]
    assert tags["DateTimeOriginal"] == "2026:06:04 09:00:00"
    assert abs(tags["GPSLatitude"] - 38.72) < 1e-4 and abs(tags["GPSLongitude"] + 9.14) < 1e-4
    rec = ingest.build_record(config.Config(home_lat=HOME[0], home_lon=HOME[1]), p, tags)
    assert rec["ts"] == "2026-06-04T09:00:00+01:00" and rec["place"]["place"] == "Lisbon"
    assert rec["camera"] == "Test Cam" and rec["zone"] == geo.ZONE_AWAY


@pytest.mark.skipif(not HAS_EXIFTOOL, reason="exiftool not installed")
def test_write_xmp_keywords_creates_and_appends(tmp_path):
    p = tmp_path / "shot.png"
    _png_1x1(p)
    ingest.write_xmp_keywords(p, ["zone/away"])
    ingest.write_xmp_keywords(p, ["cluster/2026-06 Lisbon", "zone/away"])
    xmp = p.with_suffix(".xmp")
    assert xmp.exists()
    out = subprocess.run(["exiftool", "-json", "-XMP-dc:Subject", str(xmp)], capture_output=True, text=True)
    subjects = json.loads(out.stdout)[0]["Subject"]
    assert sorted(subjects) == ["photosort/cluster/2026-06 Lisbon", "photosort/zone/away"]
