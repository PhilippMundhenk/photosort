"""The .xmp sidecar holds photosort's keywords only. Older versions created it with "exiftool -o",
which also copied the file's dates: a photo's without its offset, a video's UTC clock as local
time. Photo managers prefer the sidecar, so those files showed up an hour or two off.
repair_xmp_dates removes exactly those copies and nothing else."""
from __future__ import annotations

import json
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, events, ingest, main

HAS_EXIFTOOL = shutil.which("exiftool") is not None
needs_exiftool = pytest.mark.skipif(not HAS_EXIFTOOL, reason="exiftool not installed")
KW = "photosort/zone/home"


# --- which dates are copies (no exiftool needed) ----------------------------------------------------

def test_copied_dates_are_naive_copies_of_a_file_date():
    file_tags = {"ExifIFD:DateTimeOriginal": "2026:10:31 17:50:14", "ExifIFD:OffsetTimeOriginal": "+01:00",
                 "IFD0:ModifyDate": "2026:10:31 17:50:14", "System:FileModifyDate": "2026:11:01 10:00:00+01:00",
                 "Composite:SubSecDateTimeOriginal": "2026:10:31 17:50:15+01:00"}
    xmp = {"XMP-dc:Subject": [KW, "family"], "XMP-exif:DateTimeOriginal": "2026:10:31 17:50:14",
           "XMP-xmp:CreateDate": "2026:10:31 17:50:14", "XMP-xmp:ModifyDate": "2026:10:31 17:50:14"}
    assert ingest._copied_dates(xmp, file_tags) == ["XMP-exif:DateTimeOriginal", "XMP-xmp:CreateDate",
                                                    "XMP-xmp:ModifyDate"]


def test_copied_dates_of_a_video_are_its_utc_clock():
    file_tags = {"QuickTime:CreateDate": "2026:10:31 16:37:02", "QuickTime:ModifyDate": "2026:10:31 16:37:02",
                 "Track1:TrackCreateDate": "2026:10:31 16:37:02"}
    xmp = {"XMP-dc:Subject": KW, "XMP-xmp:CreateDate": "2026:10:31 16:37:02",
           "XMP-xmp:ModifyDate": "2026:10:31 16:37:02"}
    assert ingest._copied_dates(xmp, file_tags) == ["XMP-xmp:CreateDate", "XMP-xmp:ModifyDate"]


def test_copied_dates_keep_what_photosort_did_not_copy():
    file_tags = {"ExifIFD:DateTimeOriginal": "2026:10:31 17:50:14",
                 "Keys:CreationDate": "2026:10:31 17:36:40+01:00",
                 "System:FileModifyDate": "2026:10:31 18:00:00+01:00",
                 "Composite:SubSecCreateDate": "2026:10:31 19:00:00"}
    base = {"XMP-dc:Subject": KW}
    # a date set by hand in another program differs from the file
    assert ingest._copied_dates({**base, "XMP-exif:DateTimeOriginal": "2026:10:31 12:00:00"}, file_tags) == []
    # a date with an offset is not the lossy copy (it is correct, or someone wrote it on purpose)
    assert ingest._copied_dates({**base, "XMP-exif:DateTimeOriginal": "2026:10:31 17:50:14+01:00"}, file_tags) == []
    # file system and composite dates are not the file's own metadata
    assert ingest._copied_dates({**base, "XMP-xmp:ModifyDate": "2026:10:31 18:00:00"}, file_tags) == []
    assert ingest._copied_dates({**base, "XMP-xmp:CreateDate": "2026:10:31 19:00:00"}, file_tags) == []
    # the wall clock of a file date with an offset: a copy that dropped it
    assert ingest._copied_dates({**base, "XMP-photoshop:DateCreated": "2026:10:31 17:36:40"},
                                file_tags) == ["XMP-photoshop:DateCreated"]
    # sub-seconds without an offset are still a copy
    assert ingest._copied_dates({**base, "XMP-exif:DateTimeOriginal": "2026:10:31 17:50:14.25"},
                                file_tags) == ["XMP-exif:DateTimeOriginal"]


def test_copied_dates_only_in_photosort_sidecars():
    file_tags = {"ExifIFD:DateTimeOriginal": "2026:10:31 17:50:14"}
    xmp = {"XMP-exif:DateTimeOriginal": "2026:10:31 17:50:14"}
    assert ingest._copied_dates(xmp, file_tags) == []                                   # no keywords at all
    assert ingest._copied_dates({**xmp, "XMP-dc:Subject": ["family", "photosortish"]}, file_tags) == []
    assert ingest._copied_dates({**xmp, "XMP-dc:Subject": KW}, {}) == []                # file unreadable


def test_repair_without_sorted_tree_or_exiftool(tmp_path, monkeypatch):
    assert ingest.repair_xmp_dates(config.Config(root=str(tmp_path / "missing"))) == \
        {"checked": 0, "repaired": 0, "tags": 0}
    root = tmp_path / "sorted"
    root.mkdir()
    (root / "a.jpg").write_bytes(b"x")
    (root / "a.xmp").write_text("<x/>")

    def missing(*a, **k):
        raise FileNotFoundError("exiftool")
    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(ingest.ExifToolMissing):
        ingest.repair_xmp_dates(config.Config(root=str(root)))


# --- real exiftool -------------------------------------------------------------------------------

def _png(path: Path) -> None:
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\0\0\0\0"))
                     + chunk(b"IEND", b""))


def _mp4(path: Path) -> None:
    """Smallest thing exiftool accepts as an MP4: ftyp + moov(mvhd) + mdat."""
    def box(t, payload):
        return struct.pack(">I", 8 + len(payload)) + t + payload
    mvhd = box(b"mvhd", b"\0\0\0\0" + struct.pack(">IIII", 0, 0, 1000, 0) + struct.pack(">IHH", 0x00010000, 0x0100, 0)
               + b"\0" * 8 + struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000) + b"\0" * 24
               + struct.pack(">I", 2))
    path.write_bytes(box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41") + box(b"moov", mvhd)
                     + box(b"mdat", b"\0" * 16))


def _exiftool(*args: str) -> None:
    subprocess.run(["exiftool", "-q", "-overwrite_original", *args], check=True, capture_output=True)


def _read(path: Path) -> dict:
    out = subprocess.run(["exiftool", "-json", "-G1", "-XMP:all", str(path)], capture_output=True, text=True)
    return json.loads(out.stdout)[0]


def _photo(path: Path) -> Path:
    _png(path)
    _exiftool("-DateTimeOriginal=2026:10:31 17:50:14", "-OffsetTimeOriginal=+01:00", "-ModifyDate=2026:10:31 17:50:14",
              str(path))
    return path


def _video(path: Path) -> Path:
    _mp4(path)
    _exiftool("-QuickTime:CreateDate=2026:10:31 16:37:02", "-QuickTime:ModifyDate=2026:10:31 16:37:02", str(path))
    return path


def _old_sidecar(media: Path) -> Path:
    """An .xmp as photosort made it before November 2026."""
    subprocess.run(["exiftool", "-q", "-o", str(media.with_suffix(".xmp")), f"-XMP-dc:Subject+={KW}", str(media)],
                   check=True, capture_output=True)
    return media.with_suffix(".xmp")


@needs_exiftool
def test_unreadable_files_give_no_tags(tmp_path):
    assert ingest._exiftool_json(["-XMP-dc:Subject"], [tmp_path / "missing.xmp"]) == {}


@needs_exiftool
def test_new_sidecar_holds_keywords_only(tmp_path):
    for media in (_photo(tmp_path / "p.png"), _video(tmp_path / "v.mp4")):
        ingest.write_xmp_keywords(media, ["zone/home", "cluster/2026-10-31 Halloween"])
        tags = _read(media.with_suffix(".xmp"))
        assert sorted(tags["XMP-dc:Subject"]) == ["photosort/cluster/2026-10-31 Halloween", "photosort/zone/home"]
        assert not [k for k in tags if k.split(":")[0] not in ("XMP-dc", "XMP-x", "SourceFile")], tags


@needs_exiftool
def test_old_sidecars_had_lossy_dates(tmp_path):
    """The bug this file is about, so the repair test below works on the real thing."""
    photo, video = _photo(tmp_path / "p.png"), _video(tmp_path / "v.mp4")
    assert _read(_old_sidecar(photo))["XMP-exif:DateTimeOriginal"] == "2026:10:31 17:50:14"      # offset lost
    assert _read(_old_sidecar(video))["XMP-xmp:CreateDate"] == "2026:10:31 16:37:02"           # UTC as local


@needs_exiftool
def test_repair_removes_copied_dates_and_keeps_the_rest(tmp_path):
    root = tmp_path / "sorted"
    trip = root / "2026-10-31 Halloween" / "pm"
    trip.mkdir(parents=True)
    photo, video = _photo(trip / "p.png"), _video(trip / "v.mp4")
    for media in (photo, video):
        _exiftool("-XMP-dc:Subject+=family", "-XMP-xmp:Rating=4", str(_old_sidecar(media)))
    # a date set by hand in another program, and a sidecar from another program
    edited = _photo(trip / "edited.png")
    _exiftool("-XMP-exif:DateTimeOriginal=2026:10:31 12:00:00", str(_old_sidecar(edited)))
    foreign = _photo(trip / "foreign.png")
    subprocess.run(["exiftool", "-q", "-o", str(foreign.with_suffix(".xmp")), "-XMP-dc:Subject+=lightroom",
                    str(foreign)], check=True, capture_output=True)
    _photo(trip / "no_sidecar.png")

    stats = ingest.repair_xmp_dates(config.Config(root=str(root)))
    # photo: DateTimeOriginal + ModifyDate, video: CreateDate + ModifyDate, edited: ModifyDate only
    assert stats == {"checked": 4, "repaired": 3, "tags": 5}

    p = _read(photo.with_suffix(".xmp"))
    assert not [k for k in p if k in ["XMP-exif:DateTimeOriginal", "XMP-xmp:CreateDate", "XMP-xmp:ModifyDate"]]
    assert sorted(p["XMP-dc:Subject"]) == ["family", KW] and p["XMP-xmp:Rating"] == 4
    assert "XMP-exif:FlashpixVersion" in p or len(p) > 3                      # the rest of the old copy stays
    v = _read(video.with_suffix(".xmp"))
    assert "XMP-xmp:CreateDate" not in v and "XMP-xmp:ModifyDate" not in v and v["XMP-xmp:Rating"] == 4
    e = _read(edited.with_suffix(".xmp"))
    assert e["XMP-exif:DateTimeOriginal"] == "2026:10:31 12:00:00"           # the hand-set date stays
    assert "XMP-xmp:ModifyDate" not in e                                      # its copied dates go
    f = _read(foreign.with_suffix(".xmp"))
    assert f["XMP-exif:DateTimeOriginal"] == "2026:10:31 17:50:14"           # not photosort's: untouched
    assert not (trip / "no_sidecar.xmp").exists()

    assert ingest.repair_xmp_dates(config.Config(root=str(root))) == {"checked": 4, "repaired": 0, "tags": 0}


@needs_exiftool
def test_repair_works_in_batches(tmp_path, monkeypatch):
    root = tmp_path / "sorted"
    root.mkdir()
    for i in range(3):
        _old_sidecar(_photo(root / f"p{i}.png"))
    runs = []
    real = subprocess.run

    def counting(cmd, *a, **k):
        runs.append(cmd)
        return real(cmd, *a, **k)
    monkeypatch.setattr(subprocess, "run", counting)
    monkeypatch.setattr(ingest, "REPAIR_BATCH", 2)
    assert ingest.repair_xmp_dates(config.Config(root=str(root)))["repaired"] == 3
    writes = [c for c in runs if "-json" not in c]
    assert len(writes) == 2 and writes[0].count("-execute") == 1               # two batches, one write each


# --- the settings button ---------------------------------------------------------------------------

@pytest.fixture
def client(library):
    main._state.update({"last_run": None, "last_stats": {}, "running": False, "error": None})
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def test_settings_button_repairs_and_logs(client, cfg, monkeypatch):
    page = client.get("/settings?mode=expert").text
    assert 'action="/settings/sidecars/xmp"' in page and "Remove copied dates" in page
    monkeypatch.setattr(ingest, "repair_xmp_dates", lambda c: {"checked": 5, "repaired": 2, "tags": 4})
    r = client.post("/settings/sidecars/xmp")
    assert r.status_code == 303 and "copied%20dates%20removed%20from%202%20of%205" in r.headers["location"]
    assert "copied dates removed from 2 of 5 (4 dates)" in client.get(r.headers["location"]).text
    ev = events.read(limit=1, kind="settings")[0]
    assert ev["changed"] == ["sidecars:xmp"] and ev["repaired"] == 2


def test_settings_button_without_exiftool(client, cfg, monkeypatch):
    def missing(c):
        raise ingest.ExifToolMissing("no exiftool")
    monkeypatch.setattr(ingest, "repair_xmp_dates", missing)
    r = client.post("/settings/sidecars/xmp")
    assert "exiftool%20is%20not%20installed" in r.headers["location"]
    assert not [e for e in events.read(limit=5, kind="settings") if e.get("changed") == ["sidecars:xmp"]]
