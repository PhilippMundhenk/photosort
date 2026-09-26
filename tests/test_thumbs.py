"""Thumbnail generation: Pillow for images (with EXIF orientation), exiftool preview for RAW,
ffmpeg for videos, NAS thumbnails first, cache, background prefetch."""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from PIL import Image

from app import config, thumbs

HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_EXIFTOOL = shutil.which("exiftool") is not None


def _img(p: Path) -> tuple[tuple[int, int], str, tuple]:
    """(size, format, pixel(10,10)) with the file closed afterwards (Windows cannot delete open files)."""
    with Image.open(p) as im:
        return im.size, im.format or "", im.convert("RGB").getpixel((10, 10))


@pytest.fixture
def cfg(tmp_path, data_dir):
    return config.Config(inboxes=[{"path": str(tmp_path), "name": "in"}], root=str(tmp_path / "sorted"))


def test_kind_and_cache_path(tmp_path):
    assert thumbs.kind(Path("a.JPG")) == "image" and thumbs.kind(Path("a.dng")) == "raw"
    assert thumbs.kind(Path("a.MOV")) == "video" and thumbs.kind(Path("a.heic")) == "image"
    c = thumbs.cache_path(tmp_path / "a.jpg")
    assert c.parent.parent == thumbs.CACHE_DIR and c.name.endswith("_t.jpg")
    assert thumbs.cache_path(tmp_path / "a.jpg", "p") != c
    assert thumbs.browser_native(Path("x.jpg")) and thumbs.browser_native(Path("x.mp4"))
    assert not thumbs.browser_native(Path("x.heic")) and not thumbs.browser_native(Path("x.dng"))


def test_generate_jpeg_respects_orientation_and_size(cfg, tmp_path):
    src = tmp_path / "IMG_0001.jpg"
    im = Image.new("RGB", (1600, 1200), "red")
    exif = Image.Exif()
    exif[0x0112] = 6                                      # rotate 90 CW
    im.save(src, "JPEG", exif=exif)
    t = thumbs.get(cfg, src)
    assert t == thumbs.cache_path(src) and t.exists()
    size, fmt, _ = _img(t)
    assert size[1] == 330 and 246 <= size[0] <= 249 and fmt == "JPEG"   # portrait after transpose, fits 440x330
    mtime = t.stat().st_mtime
    time.sleep(0.01)
    assert thumbs.get(cfg, src) == t and t.stat().st_mtime == mtime       # cached, not regenerated


def test_generate_png_and_preview_variant(cfg, tmp_path):
    src = tmp_path / "shot.png"
    Image.new("RGBA", (3000, 1000), (0, 255, 0, 128)).save(src, "PNG")
    t = thumbs.get(cfg, src)
    assert _img(t)[0][0] == 440 and 145 <= _img(t)[0][1] <= 147
    p = thumbs.get(cfg, src, variant="p")
    assert _img(p)[0][0] == 2000 and 665 <= _img(p)[0][1] <= 667 and p != t


def test_nas_thumbnail_wins_and_is_not_cached(cfg, tmp_path):
    src = tmp_path / "IMG.jpg"
    Image.new("RGB", (10, 10)).save(src, "JPEG")
    nas = tmp_path / "@eaDir" / "IMG.jpg" / "SYNOPHOTO_THUMB_M.jpg"
    nas.parent.mkdir(parents=True)
    nas.write_bytes(b"x")
    assert thumbs.get(cfg, src) == nas and not thumbs.cache_path(src).exists()
    cfg.thumb_pattern = "{bogus}/x.jpg"
    assert thumbs.nas_thumb(cfg, src) is None


def test_bad_or_missing_files_give_none(cfg, tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"\xff\xd8not really")
    assert thumbs.get(cfg, bad) is None and not thumbs.cache_path(bad).exists()
    assert not list(thumbs.CACHE_DIR.rglob("*.tmp.jpg"))
    assert thumbs.get(cfg, tmp_path / "missing.jpg") is None
    cfg.generate_thumbnails = False
    ok = tmp_path / "ok.jpg"
    Image.new("RGB", (10, 10)).save(ok, "JPEG")
    assert thumbs.get(cfg, ok) is None


def test_prefetch_generates_in_background(cfg, tmp_path):
    srcs = []
    for i in range(3):
        p = tmp_path / f"p{i}.jpg"
        Image.new("RGB", (100, 80), "blue").save(p, "JPEG")
        srcs.append(p)
    t = thumbs.prefetch(cfg, srcs + [tmp_path / "missing.jpg"])
    assert t is not None
    t.join(30)
    assert all(thumbs.cache_path(p).exists() for p in srcs)
    assert thumbs.prefetch(cfg, srcs) is None                     # nothing left to do
    cfg.generate_thumbnails = False
    assert thumbs.prefetch(cfg, [tmp_path / "new.jpg"]) is None


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_video_frame_with_ffmpeg(cfg, tmp_path):
    src = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=orange:s=640x360:d=2",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    t = thumbs.get(cfg, src)
    assert t and t.exists()
    size, _, px = _img(t)
    assert size == (440, 248)
    assert px[0] > 200                                            # orange-ish frame, not black


@pytest.mark.skipif(not HAS_EXIFTOOL, reason="exiftool not installed")
def test_raw_preview_via_exiftool(cfg, tmp_path):
    """exiftool cannot write preview tags into a bare TIFF, so the 'RAW' here is a JPEG under a
    .dng name with an EXIF ThumbnailImage: the extension selects the RAW path (no Pillow decode
    of the file itself), the embedded image is what gets extracted."""
    src = tmp_path / "shot.dng"
    Image.new("RGB", (400, 300), "purple").save(src, "JPEG")
    preview = tmp_path / "prev.jpg"
    Image.new("RGB", (800, 600), "purple").save(preview, "JPEG")
    subprocess.run(["exiftool", "-q", "-overwrite_original", f"-ThumbnailImage<={preview}", str(src)], check=True)
    assert thumbs._raw_preview(src).size == (800, 600)
    t = thumbs.get(cfg, src)
    assert t and _img(t)[0] == (440, 330)
    bare = tmp_path / "bare.dng"
    Image.new("RGB", (40, 30)).save(bare, "TIFF")
    assert thumbs._raw_preview(bare) is None and thumbs.get(cfg, bare) is None
