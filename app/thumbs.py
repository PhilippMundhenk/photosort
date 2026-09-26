"""Thumbnails and previews for the review UI.

Order of preference for a thumbnail:
  1. the NAS's own thumbnail (Synology @eaDir, cfg.thumb_pattern) - free
  2. the cache <data>/thumbs/<sha1>.jpg
  3. generate: Pillow for JPEG/PNG/WebP/HEIC (JPEG draft mode decodes at 1/8 scale, so a 12 MP
     photo costs ~30 ms), the embedded preview via exiftool for RAW files, one frame via ffmpeg
     for videos. Missing tools degrade to "no thumbnail", never to an error.
A background thread pre-generates thumbnails for newly scanned files so review pages are fast.
"""
from __future__ import annotations

import hashlib
import io
import logging
import shutil
import subprocess
import threading
from pathlib import Path

from .config import DATA_DIR, Config

log = logging.getLogger("photosort.thumbs")

THUMB_SIZE = (440, 330)          # 110x82 css px at 4x for crisp hidpi thumbs, still ~30 kB
PREVIEW_SIZE = (2000, 2000)      # "full image" view for formats browsers cannot show (HEIC, RAW)
JPEG_QUALITY = 82
CACHE_DIR = DATA_DIR / "thumbs"

PIL_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "heic", "heif"}
RAW_EXTENSIONS = {"dng", "cr2", "cr3", "nef", "arw", "orf", "rw2", "raf", "pef", "srw"}
VIDEO_EXTENSIONS = {"mp4", "mov", "m4v", "3gp", "mkv", "avi", "webm", "mts", "m2ts"}
BROWSER_NATIVE = {"jpg", "jpeg", "png", "webp", "gif", "mp4", "mov", "m4v", "webm"}

_lock = threading.Lock()
_inflight: set[str] = set()


def _ext(p: Path) -> str:
    return p.suffix.lower().lstrip(".")


def kind(p: Path) -> str:
    e = _ext(p)
    if e in VIDEO_EXTENSIONS:
        return "video"
    if e in RAW_EXTENSIONS:
        return "raw"
    return "image"


def cache_path(photo: Path, variant: str = "t") -> Path:
    h = hashlib.sha1(str(photo).encode("utf-8", "surrogateescape")).hexdigest()
    return CACHE_DIR / h[:2] / f"{h}_{variant}.jpg"


def nas_thumb(cfg: Config, photo: Path) -> Path | None:
    try:
        cand = photo.parent / cfg.thumb_pattern.format(name=photo.name)
    except (KeyError, IndexError, ValueError):
        return None
    return cand if cand.exists() else None


# --- generators -----------------------------------------------------------------------

def _pil_image(src: Path, size: tuple[int, int]):
    from PIL import Image, ImageOps
    if _ext(src) in ("heic", "heif"):
        import pillow_heif
        pillow_heif.register_heif_opener()
    im = Image.open(src)
    if im.format == "JPEG":
        im.draft("RGB", (size[0] * 2, size[1] * 2))          # decode at reduced scale, cheap
    im = ImageOps.exif_transpose(im)
    im.thumbnail(size)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    return im


def _raw_preview(src: Path):
    """Embedded JPEG preview of a RAW file via exiftool (no RAW decoding)."""
    if not shutil.which("exiftool"):
        return None
    for tag in ("-JpgFromRaw", "-PreviewImage", "-OtherImage", "-ThumbnailImage"):
        res = subprocess.run(["exiftool", "-b", tag, str(src)], capture_output=True)
        if res.returncode == 0 and len(res.stdout) > 1000:
            from PIL import Image, ImageOps
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(res.stdout)))
            return im
    return None


def _video_frame(src: Path, dst: Path, width: int) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    for seek in ("1", "0"):                                   # short clips have no second 1
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-ss", seek, "-i", str(src), "-frames:v", "1",
               "-vf", f"scale='min({width},iw)':-2", "-q:v", "4", str(dst)]
        res = subprocess.run(cmd, capture_output=True)
        if res.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
            return True
    return False


def generate(photo: Path, dst: Path, size: tuple[int, int]) -> Path | None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.jpg")
    try:
        k = kind(photo)
        if k == "video":
            ok = _video_frame(photo, tmp, size[0])
        else:
            im = _raw_preview(photo) if k == "raw" else _pil_image(photo, size)
            if im is None:
                return None
            im.thumbnail(size)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(tmp, "JPEG", quality=JPEG_QUALITY, optimize=True)
            ok = True
        if not ok:
            return None
        tmp.replace(dst)
        return dst
    except Exception as e:  # noqa: BLE001  (corrupt file, unsupported codec, ...)
        log.info("no thumbnail for %s: %s", photo.name, e)
        return None
    finally:
        tmp.unlink(missing_ok=True)


# --- public -----------------------------------------------------------------------------

def get(cfg: Config, photo: Path, variant: str = "t") -> Path | None:
    """Thumbnail ('t') or large preview ('p') for a photo, generating it if needed."""
    if variant == "t":
        nas = nas_thumb(cfg, photo)
        if nas:
            return nas
    cached = cache_path(photo, variant)
    if cached.exists():
        return cached
    if not cfg.generate_thumbnails or not photo.exists():
        return None
    key = f"{variant}:{photo}"
    with _lock:
        if key in _inflight:
            return None                                       # someone else is on it; placeholder for now
        _inflight.add(key)
    try:
        return generate(photo, cached, THUMB_SIZE if variant == "t" else PREVIEW_SIZE)
    finally:
        with _lock:
            _inflight.discard(key)


def browser_native(photo: Path) -> bool:
    return _ext(photo) in BROWSER_NATIVE


def prefetch(cfg: Config, photos: list[Path]) -> threading.Thread | None:
    """Generate missing thumbnails in the background, one at a time (the T430 has two cores)."""
    if not cfg.generate_thumbnails:
        return None
    todo = [p for p in photos if not nas_thumb(cfg, p) and not cache_path(p).exists()]
    if not todo:
        return None

    def work():
        n = 0
        for p in todo:
            if get(cfg, p):
                n += 1
        log.info("thumbnails: %d generated, %d skipped", n, len(todo) - n)

    t = threading.Thread(target=work, name="thumbs", daemon=True)
    t.start()
    return t
