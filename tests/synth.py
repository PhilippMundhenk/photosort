"""Synthetic photo library shared by the smoke test, the unit/e2e tests and local demos.

No exiftool needed: each fake JPG gets a sidecar written directly. Layout (UTC, June 2026):

  everyday      1 photo/day at home, except while travelling
  trip          Lisbon 14 d (2 phones) -> Seville 3 d -> Lisbon 4 d (with no-GPS photos) -> home
  day out       15 photos in Ludwigsburg (local zone)
  occasion      25 photos at home in ~4 h from 2 devices (birthday)
  busy day      14 photos at home spread over 9 h from 1 device (optional)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import config, ingest

HOME = (48.944, 9.118)                      # Bietigheim-Bissingen
LISBON, SEVILLE, LUDWIGSBURG = (38.72, -9.14), (37.39, -5.98), (48.897, 9.192)
T0 = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)


def make_config(base: Path, **overrides) -> config.Config:
    kw = dict(inboxes=[{"path": str(base / "phone-a"), "name": "phone-a"},
                       {"path": str(base / "phone-b"), "name": "phone-b"}],
              root=str(base / "sorted"), home_lat=HOME[0], home_lon=HOME[1], timezone="UTC",
              write_xmp_sidecar=False, subfolder_by_source=True)
    kw.update(overrides)
    return config.Config(**kw)


class Library:
    def __init__(self, cfg: config.Config):
        self.cfg = cfg
        self.n = 0
        self.paths: list[Path] = []

    def photo(self, inbox: Path, when: datetime, pos=None, cam: str = "phone") -> Path:
        self.n += 1
        p = inbox / f"IMG_{self.n:04d}.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\xff\xd8fake\xff\xd9")
        tags = {"DateTimeOriginal": when.strftime("%Y:%m:%d %H:%M:%S"), "Model": cam}
        if pos:
            tags["GPSLatitude"], tags["GPSLongitude"] = pos
        rec = {**ingest.build_record(self.cfg, p, tags, source=inbox.name), "source": inbox.name, "inbox": str(inbox)}
        ingest.write_sidecar(p, rec, self.cfg)
        self.paths.append(p)
        return p

    def video(self, inbox: Path, when: datetime, pos=None, kind: str = "android") -> Path:
        """A fake MP4 with the tags exiftool would report for that phone family. `when` is the
        instant (aware); Android files carry it as spec-UTC CreateDate only, iPhone files also as
        Keys:CreationDate with an offset plus Make/Model."""
        self.n += 1
        p = inbox / f"VID_{self.n:04d}.mp4"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0\0\0\x18ftypisom")
        utc = when.astimezone(timezone.utc)
        tags = {"MIMEType": "video/mp4", "CreateDate": utc.strftime("%Y:%m:%d %H:%M:%S"),
                "MediaCreateDate": utc.strftime("%Y:%m:%d %H:%M:%S")}
        if kind == "iphone":
            tags.update({"CreationDate": when.strftime("%Y:%m:%d %H:%M:%S%z")[:-2] + ":" + when.strftime("%z")[-2:],
                         "Make": "Apple", "Model": "iPhone 15"})
        if pos:
            tags["GPSLatitude"], tags["GPSLongitude"] = pos
        rec = {**ingest.build_record(self.cfg, p, tags, source=inbox.name), "source": inbox.name, "inbox": str(inbox)}
        ingest.write_sidecar(p, rec, self.cfg)
        self.paths.append(p)
        return p


def populate(cfg: config.Config, busy_day: bool = False) -> Library:
    lib = Library(cfg)
    a, b = Path(cfg.inboxes[0]["path"]), Path(cfg.inboxes[1]["path"])
    for d in range(0, 30):
        if not 3 <= d < 24:                  # not while travelling
            lib.photo(a, T0 + timedelta(days=d, hours=8), HOME)
    day = T0 + timedelta(days=3)
    for d in range(14):
        for h in (9, 15):
            lib.photo(a, day + timedelta(days=d, hours=h), LISBON)
            lib.photo(b, day + timedelta(days=d, hours=h, minutes=5), LISBON, cam="phone-b")
    for d in range(14, 17):
        lib.photo(a, day + timedelta(days=d, hours=12), SEVILLE)
    for d in range(17, 21):
        lib.photo(a, day + timedelta(days=d, hours=12), LISBON)
        lib.photo(a, day + timedelta(days=d, hours=13))          # no GPS -> neighbour
    lib.photo(a, day + timedelta(days=21, hours=9), HOME)        # back home ends the trip
    for i in range(15):                                          # day out
        lib.photo(a, T0 + timedelta(days=26, hours=11, minutes=i * 10), LUDWIGSBURG)
    for i in range(25):                                          # birthday, 2 devices
        lib.photo(a if i % 2 else b, T0 + timedelta(days=28, hours=14, minutes=i * 9), HOME,
                  cam="phone-b" if i % 2 else "phone")
    if busy_day:
        for i in range(14):
            lib.photo(a, T0 + timedelta(days=29, hours=8, minutes=i * 40), HOME)
    return lib
