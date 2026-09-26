"""Videos from iPhones and Android phones: timestamps (zone-aware, home zone), GPS from the
QuickTime keys, device fallback to the inbox, naming and UI badges. The real-exiftool tests
write metadata into crafted MP4 files exactly as the two phone families do."""
from __future__ import annotations

import shutil
import struct
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import cluster, config, geo, ingest
from app.kev import Decider
from tests import synth
from tests.synth import HOME, LISBON

BERLIN = ZoneInfo("Europe/Berlin")
HAS_EXIFTOOL = shutil.which("exiftool") is not None


# --- timestamp parsing --------------------------------------------------------------------

def test_photo_naive_time_stays_naive_and_offset_is_kept():
    assert ingest._parse_dt({"DateTimeOriginal": "2026:06:04 09:00:00"}, tz=BERLIN) == "2026-06-04T09:00:00"
    assert ingest._parse_dt({"DateTimeOriginal": "2026:06:04 09:00:00", "OffsetTimeOriginal": "+01:00"},
                            tz=BERLIN) == "2026-06-04T09:00:00+01:00"


def test_iphone_video_uses_creation_date_with_its_offset():
    tags = {"CreationDate": "2026:06:04 09:00:00+01:00", "CreateDate": "2026:06:04 08:00:00",   # Lisbon
            "MediaCreateDate": "2026:06:04 08:00:00"}
    assert ingest._parse_dt(tags, video=True, tz=BERLIN) == "2026-06-04T10:00:00+02:00"      # same instant, home zone
    assert datetime.fromisoformat(ingest._parse_dt(tags, video=True, tz=BERLIN)) == \
        datetime.fromisoformat("2026-06-04T09:00:00+01:00")


def test_android_video_create_date_is_utc_converted_to_home_zone():
    tags = {"CreateDate": "2026:06:04 07:00:00", "MediaCreateDate": "2026:06:04 07:00:00"}
    assert ingest._parse_dt(tags, video=True, tz=BERLIN) == "2026-06-04T09:00:00+02:00"
    assert ingest._parse_dt(tags, video=True, tz=ZoneInfo("America/New_York")) == "2026-06-04T03:00:00-04:00"
    assert ingest._parse_dt({"CreateDate": "2026:12:04 07:00:00"}, video=True, tz=BERLIN) == "2026-12-04T08:00:00+01:00"
    zulu = {"CreateDate": "2026:06:04 07:00:00Z"}
    assert ingest._parse_dt(zulu, video=True, tz=BERLIN) == "2026-06-04T09:00:00+02:00"


def test_video_falls_back_to_file_modify_date_and_skips_zero_dates():
    tags = {"CreateDate": "0000:00:00 00:00:00", "FileModifyDate": "2026:06:04 09:00:00+02:00"}
    assert ingest._parse_dt(tags, video=True, tz=BERLIN) == "2026-06-04T09:00:00+02:00"
    assert ingest._parse_dt({"CreateDate": "garbage"}, video=True, tz=BERLIN) is None


def test_file_name_timestamp_beats_mtime():
    mtime = {"FileModifyDate": "2026:09:26 15:00:00+00:00"}
    assert ingest._parse_dt(mtime, video=True, tz=BERLIN, name="20260101_000025.mp4") == "2026-01-01T00:00:25+01:00"
    assert ingest._parse_dt(mtime, name="IMG_20260604_090000.jpg") == "2026-06-04T09:00:00"
    assert ingest._parse_dt(mtime, name="PXL_20260604_090000123.mp4", video=True, tz=BERLIN) \
        == "2026-06-04T09:00:00+02:00"
    assert ingest._parse_dt(mtime, name="VID-20260604-090000.mp4") == "2026-06-04T09:00:00"
    assert ingest._parse_dt(mtime, name="DSC_1234.jpg") == "2026-09-26T15:00:00+00:00"        # no pattern: mtime
    assert ingest._parse_dt(mtime, name="20261399_990000.jpg") == "2026-09-26T15:00:00+00:00"  # invalid date
    tags = {"CreateDate": "2026:06:04 07:00:00"}
    assert ingest._parse_dt(tags, video=True, tz=BERLIN, name="20200101_000000.mp4") == "2026-06-04T09:00:00+02:00"


def test_exif_batch_reads_videos_without_fast2(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R:
            returncode, stdout = 0, "[]"
        return R()
    monkeypatch.setattr(ingest.subprocess, "run", fake_run)
    files = [tmp_path / "a.jpg", tmp_path / "b.mp4", tmp_path / "c.MOV", tmp_path / "d.heic"]
    ingest.exif_batch(files)
    assert len(calls) == 2
    photos, videos = calls
    assert "-fast2" in photos and str(files[0]) in photos and str(files[3]) in photos
    assert "-fast2" not in videos and str(files[1]) in videos and str(files[2]) in videos
    ingest.exif_batch([tmp_path / "only.mp4"])
    assert len(calls) == 3                                                      # no empty photo call


def test_records_sort_by_instant_across_photos_and_videos(tmp_path):
    """A naive photo time and a converted video time of the same moment compare equal."""
    cfg = config.Config(timezone="Europe/Berlin")
    tz = config.tzinfo(cfg)
    photo = ingest.build_record(cfg, tmp_path / "a.jpg", {"DateTimeOriginal": "2026:06:04 09:00:00"})
    video = ingest.build_record(cfg, tmp_path / "a.mp4", {"CreateDate": "2026:06:04 07:00:00", "MIMEType": "video/mp4"})
    assert cluster._dt(photo["ts"], tz) == cluster._dt(video["ts"], tz)
    assert photo["media"] == "photo" and video["media"] == "video"


def test_timezone_config_and_fallback(monkeypatch):
    assert str(config.tzinfo(config.Config(timezone="Europe/Berlin"))) == "Europe/Berlin"
    assert str(config.tzinfo(config.Config(timezone="Not/AZone"))) == "UTC"
    assert str(config.tzinfo(config.Config(timezone=""))) == "UTC"
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert config.Config().timezone == "Asia/Tokyo"


# --- device ------------------------------------------------------------------------------

def test_camera_fallback_chain():
    assert ingest._camera({"Make": "Apple", "Model": "iPhone 15"}, "phone-a") == ("Apple iPhone 15", "exif")
    assert ingest._camera({"AndroidManufacturer": "samsung", "AndroidModel": "SM-S911B"}, "phone-b") \
        == ("samsung SM-S911B", "exif")
    assert ingest._camera({}, "phone-b") == ("phone-b", "inbox")
    assert ingest._camera({}, None) == (None, None)


def test_count_devices_merges_inbox_fallback_with_single_named_device():
    def r(cam, how, src):
        return {"camera": cam, "camera_source": how, "source": src}
    a_photo, a_video = r("samsung SM-S911B", "exif", "phone-a"), r("phone-a", "inbox", "phone-a")
    b_photo = r("Apple iPhone 15", "exif", "phone-b")
    assert cluster.count_devices([a_photo, a_video]) == 1                   # Android video = its phone
    assert cluster.count_devices([a_photo, a_video, b_photo]) == 2
    assert cluster.count_devices([a_video, r("phone-b", "inbox", "phone-b")]) == 2   # nothing named: by inbox
    two_cams = [r("Canon", "exif", "cam"), r("Nikon", "exif", "cam"), r("cam", "inbox", "cam")]
    assert cluster.count_devices(two_cams) == 3                             # ambiguous inbox: own key


# --- naming & pipeline -----------------------------------------------------------------------

def test_media_label():
    assert cluster.media_label([{"media": "photo"}] * 3) == "3 Fotos"
    assert cluster.media_label([{"media": "photo"}] * 3 + [{"media": "video"}]) == "3 Fotos, 1 Videos"
    assert cluster.media_label([{"media": "video"}] * 2) == "2 Videos"
    assert cluster.media_label([]) == "0 Fotos"


def test_pipeline_with_videos(cfg):
    lib = synth.populate(cfg)
    a = Path(cfg.inboxes[0]["path"])
    when = synth.T0 + timedelta(days=28, hours=15)                       # inside the birthday burst
    lib.video(a, when, HOME, kind="android")
    lib.video(a, when + timedelta(minutes=5), HOME, kind="iphone")
    lib.video(a, synth.T0 + timedelta(days=5, hours=12), LISBON, kind="android")   # inside the trip
    cluster.run(cfg, Decider(cfg))
    kinds = {p["kind"]: p for p in cluster.load_proposals().values()}
    home, trip = kinds["home"], kinds["trip"]
    assert home["name"] == "2026-06-30 (25 Fotos, 2 Videos)" and home["n"] == 27
    assert sum(p["media"] == "video" for p in home["photos"]) == 2
    assert trip["n"] == 68 and any(p["media"] == "video" and p["zone"] == geo.ZONE_AWAY for p in trip["photos"])
    assert all(p["conf"] == 1.0 for p in trip["photos"] if p["media"] == "video")   # GPS from the video itself


# --- real exiftool on crafted MP4s ---------------------------------------------------------------

def _mp4(path: Path) -> None:
    """Smallest thing exiftool accepts as an MP4: ftyp + moov(mvhd) + mdat."""
    def box(t, payload):
        return struct.pack(">I", 8 + len(payload)) + t + payload
    mvhd = box(b"mvhd", b"\0\0\0\0" + struct.pack(">IIII", 0, 0, 1000, 0) + struct.pack(">IHH", 0x00010000, 0x0100, 0)
               + b"\0" * 8 + struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000) + b"\0" * 24
               + struct.pack(">I", 2))
    path.write_bytes(box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41") + box(b"moov", mvhd)
                     + box(b"mdat", b"\0" * 16))


def _write(path: Path, *tags: str) -> None:
    subprocess.run(["exiftool", "-q", "-overwrite_original", *tags, str(path)], check=True)


@pytest.mark.skipif(not HAS_EXIFTOOL, reason="exiftool not installed")
def test_iphone_mov_real_exiftool(tmp_path):
    p = tmp_path / "IMG_0001.MOV"
    _mp4(p)
    _write(p, "-QuickTime:CreateDate=2026:06:04 07:00:00", "-Keys:CreationDate=2026:06:04 08:00:00+01:00",
           "-Keys:GPSCoordinates=38.72 -9.14", "-Keys:Make=Apple", "-Keys:Model=iPhone 15")
    cfg = config.Config(home_lat=HOME[0], home_lon=HOME[1], timezone="Europe/Berlin")
    rec = ingest.build_record(cfg, p, ingest.exif_batch([p])[str(p)], source="phone-a")
    assert rec["media"] == "video" and rec["ts"] == "2026-06-04T09:00:00+02:00"
    assert abs(rec["lat"] - 38.72) < 1e-4 and abs(rec["lon"] + 9.14) < 1e-4
    assert rec["zone"] == geo.ZONE_AWAY and rec["place"]["place"] == "Lisbon"
    assert rec["camera"] == "Apple iPhone 15" and rec["camera_source"] == "exif"


@pytest.mark.skipif(not HAS_EXIFTOOL, reason="exiftool not installed")
def test_android_mp4_real_exiftool(tmp_path):
    p = tmp_path / "VID_20260604_090000.mp4"
    _mp4(p)
    _write(p, "-QuickTime:CreateDate=2026:06:04 07:00:00", "-QuickTime:ModifyDate=2026:06:04 07:00:10",
           "-UserData:GPSCoordinates=48.944 9.118")                 # Android writes ©xyz in udta
    cfg = config.Config(home_lat=HOME[0], home_lon=HOME[1], timezone="Europe/Berlin")
    rec = ingest.build_record(cfg, p, ingest.exif_batch([p])[str(p)], source="phone-b")
    assert rec["ts"] == "2026-06-04T09:00:00+02:00"
    assert rec["zone"] == geo.ZONE_HOME and rec["gps_source"] == "exif"
    assert rec["camera"] == "phone-b" and rec["camera_source"] == "inbox"


@pytest.mark.skipif(not HAS_EXIFTOOL, reason="exiftool not installed")
def test_video_without_gps_inherits_neighbour(tmp_path):
    p = tmp_path / "clip.mp4"
    _mp4(p)
    _write(p, "-QuickTime:CreateDate=2026:06:04 07:00:00")
    cfg = config.Config(home_lat=HOME[0], home_lon=HOME[1], timezone="Europe/Berlin")
    rec = ingest.build_record(cfg, p, ingest.exif_batch([p])[str(p)], source="phone-b")
    assert rec["lat"] is None and rec["zone"] == geo.ZONE_UNKNOWN and rec["ts"] == "2026-06-04T09:00:00+02:00"
