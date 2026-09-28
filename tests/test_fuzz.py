"""Randomized inputs to everything that parses what phones, exiftool and users hand in: the
parsers must never raise, and what they return must be well-formed. A thousand cases each,
seeded, so a failure reproduces."""
from __future__ import annotations

import random
import string
from datetime import datetime
from pathlib import Path

from app import cluster, config, geo, ingest

SEED = 20260929


def _rand_text(rnd: random.Random, n: int = 12) -> str:
    alphabet = string.printable + "äöüß€漢字🙂"
    return "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, n)))


def _rand_tag_value(rnd: random.Random):
    return rnd.choice([
        None, "", 0, 0.0, -1, 1e308, float("nan"), float("inf"), True, [], {}, "0000:00:00 00:00:00",
        "2026:06:04 09:15:30", "2026:06:04 09:15:30+02:00", "2026:06:04 09:15:30Z", "2026-06-04T09:15:30",
        "2026:13:40 25:61:61", "garbage", _rand_text(rnd), rnd.uniform(-200, 200), str(rnd.uniform(-200, 200)),
        "48.944", "9.118", "-9.14", "+02:00", "-14:00", "+25:00", "02:00",
    ])


def test_build_record_never_raises_and_is_well_formed(tmp_path):
    rnd = random.Random(SEED)
    cfg = config.Config(home_lat=48.944, home_lon=9.118)
    keys = ["DateTimeOriginal", "OffsetTimeOriginal", "GPSDateTime", "CreationDate", "CreateDate", "MediaCreateDate",
            "FileModifyDate", "MIMEType", "GPSLatitude", "GPSLongitude", "Make", "Model", "AndroidManufacturer",
            "AndroidModel", "Unexpected"]
    exts = ["jpg", "JPG", "heic", "mp4", "MOV", "dng", "png", "bin", ""]
    for _ in range(1000):
        tags = {k: _rand_tag_value(rnd) for k in rnd.sample(keys, rnd.randint(0, len(keys)))}
        name = f"{_rand_text(rnd, 6) or 'x'}{rnd.choice(['', '_20260604_091530', 'PXL_20260604_091530123'])}"
        photo = tmp_path / f"{name}.{rnd.choice(exts)}".replace("/", "_").replace("\\", "_").replace("\0", "")
        rec = ingest.build_record(cfg, photo, tags, source=rnd.choice([None, "phone-a", ""]))
        assert rec["v"] == ingest.SIDECAR_VERSION and rec["file"] == photo.name
        assert rec["media"] in ("photo", "video")
        if rec["ts"] is not None:
            datetime.fromisoformat(rec["ts"])                             # an ISO timestamp or nothing
            assert rec["ts_source"] in ("exif", "exif+offset", "gps", "utc-home", "name", "mtime")
        assert (rec["lat"] is None) == (rec["lon"] is None)
        if rec["lat"] is not None:
            assert abs(rec["lat"]) <= 200 and abs(rec["lon"]) <= 200      # finite numbers only
            assert rec["zone"] in (geo.ZONE_HOME, geo.ZONE_LOCAL, geo.ZONE_AWAY)
            assert isinstance(rec["place"], dict) and "place" in rec["place"]
        else:
            assert rec["zone"] == geo.ZONE_UNKNOWN and rec["place"] is None


def test_time_parsers_never_raise():
    rnd = random.Random(SEED + 1)
    for _ in range(1000):
        s = rnd.choice(["2026:06:04 09:15:30", "2026:06:04 09:15:30+02:00", _rand_text(rnd, 25), "", "Z",
                        "2026:06:04 09:15:30-14:00", "2026:06:04 09:15:30+2:00", "2026:06:04 09:15"])
        off = rnd.choice(["", "+02:00", "Z", "+2", "garbage", "-00:30", "+15:00"])
        dt = ingest._exif_dt(s, off)
        assert dt is None or isinstance(dt, datetime)
        local = datetime(2026, 6, 4, rnd.randint(0, 23), rnd.randint(0, 59))
        tz = ingest._gps_offset(local, {"GPSDateTime": s})
        assert tz is None or abs(tz.utcoffset(None).total_seconds()) <= 14 * 3600
        ingest._name_dt(_rand_text(rnd, 30))                                 # any text: a datetime or None
        assert isinstance(ingest._num(_rand_tag_value(rnd)), float | None)


def test_user_text_parsers_never_raise():
    rnd = random.Random(SEED + 2)
    for _ in range(1000):
        text = "\n".join(_rand_text(rnd, 40) for _ in range(rnd.randint(0, 5)))
        places = config.parse_named_places(text)
        assert all(set(p) == {"name", "lat", "lon", "radius_km"} for p in places)
        assert all(p["radius_km"] > 0 and p["name"] for p in places)
        name = cluster.sanitize(_rand_text(rnd, 60))
        assert not any(ch in name for ch in '\\/:*?"<>|') and not name.startswith(".") and name == name.strip()
        form = {k: _rand_text(rnd, 8) for k in rnd.sample(list(config.Config().as_dict()), rnd.randint(0, 10))}
        try:
            cfg = config.update_from_form(config.Config(), form)
            assert isinstance(cfg.scan_interval_min, int) and isinstance(cfg.dry_run, bool)
        except ValueError:
            pass                                                        # a number field given text: refused


def test_clustering_any_timeline_never_raises():
    rnd = random.Random(SEED + 3)
    cfg = config.Config(home_lat=48.944, home_lon=9.118)
    zones = [geo.ZONE_HOME, geo.ZONE_LOCAL, geo.ZONE_AWAY, geo.ZONE_UNKNOWN]
    for _ in range(200):
        recs = []
        t = datetime(2026, 1, 1, tzinfo=None)
        from datetime import timedelta, timezone
        t = t.replace(tzinfo=timezone.utc)
        for i in range(rnd.randint(0, 60)):
            t += timedelta(minutes=rnd.choice([1, 30, 600, 5000, 100000]))
            z = rnd.choice(zones)
            pos = None if z == geo.ZONE_UNKNOWN else (rnd.uniform(-90, 90), rnd.uniform(-180, 180))
            recs.append({"file": f"f{i}.jpg", "path": f"/in/{rnd.choice(['a', 'b'])}/f{i}.jpg", "ts": t.isoformat(),
                         "_t": t, "zone": z, "lat": pos[0] if pos else None, "lon": pos[1] if pos else None,
                         "gps_source": "exif" if pos else None, "source": rnd.choice(["a", "b", None]),
                         "place": rnd.choice([None, {"place": "X", "country": "Y"}, {"place": "", "country": ""}]),
                         "media": rnd.choice(["photo", "video"]), "camera": rnd.choice(["cam", None])})
        recs.sort(key=lambda r: r["_t"])
        cluster.fill_gps_from_neighbours(cfg, recs)
        for run in cluster.find_excursions(cfg, recs):
            assert run and run[0]["zone"] != geo.ZONE_UNKNOWN
            kind = cluster.excursion_kind(cfg, run)
            if kind == "trip":
                pr = cluster.trip_proposal(cfg, run, datetime.now(timezone.utc))
            elif kind == "local":
                pr = cluster.local_proposal(cfg, run, datetime.now(timezone.utc))
            else:
                continue
            assert pr["n"] == len(run) and pr["name"] == cluster.sanitize(pr["name"]) and pr["start"] <= pr["end"]
        for burst in cluster.group_bursts(cfg, recs):
            assert burst
            if len(burst) >= 3:
                pr = cluster.home_proposal(cfg, burst, 3.0)
                assert pr["n"] == len(burst) and Path(pr["name"]).name == pr["name"]
