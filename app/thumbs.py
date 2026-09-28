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

import atexit
import contextlib
import hashlib
import io
import logging
import os
import shutil
import subprocess
import threading
import time
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
_worker: threading.Thread | None = None
_last_request = 0.0              # when a page last asked for a thumbnail: the prefetch yields to it


def touch() -> None:
    """A page is loading thumbnails right now; the background prefetch pauses for a moment."""
    global _last_request
    _last_request = time.time()


def wait(timeout: float = 60) -> None:
    """Block until the background prefetch (if any) is done; tests use this before cleaning up."""
    if _worker is not None and _worker.is_alive():
        _worker.join(timeout)


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


def _unlimited() -> None:
    """preexec for exiftool/ffmpeg started by the helper: the helper's address-space cap is
    for Pillow; ffmpeg reserves large mappings for its threads and fails under it."""
    try:
        import resource
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (hard, hard))
    except Exception:  # noqa: BLE001
        pass


_PREEXEC = {"preexec_fn": _unlimited} if os.name == "posix" else {}


def _raw_preview(src: Path):
    """Embedded JPEG preview of a RAW file via exiftool (no RAW decoding)."""
    if not shutil.which("exiftool"):
        return None
    for tag in ("-JpgFromRaw", "-PreviewImage", "-OtherImage", "-ThumbnailImage"):
        res = subprocess.run(["exiftool", "-b", tag, str(src)], capture_output=True, **_PREEXEC)
        if res.returncode == 0 and len(res.stdout) > 1000:
            from PIL import Image, ImageOps
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(res.stdout)))
            return im
    return None


def _video_frame(src: Path, dst: Path, width: int) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    nice = [shutil.which("nice"), "-n", "19"] if shutil.which("nice") else []   # decoding 4K must not starve the UI
    for seek in ("1", "0"):                                   # short clips have no second 1
        cmd = [*nice, ffmpeg, "-y", "-loglevel", "error", "-threads", "1", "-ss", seek, "-i", str(src),
               "-frames:v", "1", "-vf", f"scale='min({width},iw)':-2", "-q:v", "4", str(dst)]
        res = subprocess.run(cmd, capture_output=True, **_PREEXEC)
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


# --- helper process ---------------------------------------------------------------------
# Decoding happens in a helper process, never in the server: a corrupt file that crashes the
# native decoder, or one huge image that needs more memory than the container has, takes the
# helper down and the server answers "no thumbnail". Before this, the container restarted a
# few seconds after every run, without a traceback, as soon as the prefetch reached such files.

GENERATE_TIMEOUT_S = 90          # a long video over a slow share; the helper is killed after this
HELPER_MEMORY_MB = 768           # a decode needing more raises MemoryError in the helper (Linux)
IN_PROCESS = False               # tests that patch the generators set this
_pool = None
_pool_lock = threading.Lock()


def _limit_memory() -> None:
    try:
        import resource
        limit = HELPER_MEMORY_MB * 1024 * 1024
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    except Exception:  # noqa: BLE001  (Windows, or not permitted)
        pass


def _helper():
    global _pool
    with _pool_lock:
        if _pool is None:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor
            _pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"),
                                        initializer=_limit_memory)
            atexit.register(shutdown)                          # a clean stop, no noise at interpreter exit
        return _pool


def _drop_helper(pool) -> None:
    global _pool
    with _pool_lock:
        if _pool is pool:
            _pool = None
    for p in list(getattr(pool, "_processes", {}).values()):
        with contextlib.suppress(Exception):
            p.kill()
    pool.shutdown(wait=False, cancel_futures=True)


def _run_generate(photo: Path, dst: Path, size: tuple[int, int]) -> Path | None:
    if IN_PROCESS:
        return generate(photo, dst, size)
    from concurrent.futures import CancelledError
    from concurrent.futures import TimeoutError as FutureTimeout
    from concurrent.futures.process import BrokenProcessPool
    for attempt in (1, 2):                       # the helper may have been replaced under us by another
        pool = _helper()                         # thread (its job crashed it): once more on a fresh one
        try:
            return pool.submit(generate, photo, dst, size).result(timeout=GENERATE_TIMEOUT_S)
        except (BrokenProcessPool, FutureTimeout, CancelledError, OSError, RuntimeError) as e:
            log.warning("thumbnail helper failed on %s (%s): %s", photo.name, attempt, type(e).__name__)
            if isinstance(e, FutureTimeout) or attempt == 2:
                _drop_helper(pool)
                return None
            _drop_helper(pool)
    return None


def shutdown() -> None:
    """Stop the helper process (tests, server shutdown)."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=True, cancel_futures=True)


def _failed_marker(cached: Path) -> Path:
    return cached.with_suffix(".none")


def _stamp(photo: Path) -> str:
    try:
        st = photo.stat()
        return f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        return ""


def known_failure(photo: Path, cached: Path) -> bool:
    """This very file (same size and mtime) gave no thumbnail before; a replaced file is tried again."""
    try:
        return _failed_marker(cached).read_text(encoding="utf-8") == _stamp(photo)
    except OSError:
        return False


# --- public -----------------------------------------------------------------------------

def get(cfg: Config, photo: Path, variant: str = "t") -> Path | None:
    """Thumbnail ('t') or large preview ('p') for a photo, generating it if needed. A photo
    that gave no thumbnail is remembered (<cache>.none) and not tried again on every run."""
    if variant == "t":
        nas = nas_thumb(cfg, photo)
        if nas:
            return nas
    cached = cache_path(photo, variant)
    if cached.exists():
        return cached
    if not cfg.generate_thumbnails or not photo.exists() or known_failure(photo, cached):
        return None
    key = f"{variant}:{photo}"
    with _lock:
        if key in _inflight:
            return None                                       # someone else is on it; placeholder for now
        _inflight.add(key)
    try:
        out = _run_generate(photo, cached, THUMB_SIZE if variant == "t" else PREVIEW_SIZE)
        if out is None:
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
                _failed_marker(cached).write_text(_stamp(photo), encoding="utf-8")
            except OSError:
                pass
        return out
    finally:
        with _lock:
            _inflight.discard(key)


def browser_native(photo: Path) -> bool:
    return _ext(photo) in BROWSER_NATIVE


PREFETCH_PAUSE_S = 0.05          # between two generated thumbnails
YIELD_S = 1.5                    # no generating while a page asked for thumbnails this recently


def prefetch(cfg: Config, photos: list[Path]) -> threading.Thread | None:
    """Generate missing thumbnails in the background, one at a time (the T430 has two cores).
    Finding out which are missing means one stat per photo on the share, so that happens in
    the worker too, not in the pipeline; and the worker pauses whenever a page is loading
    thumbnails, so the review pages stay quick while it works through a big library."""
    if not cfg.generate_thumbnails or not photos:
        return None

    def work():
        n, todo = 0, 0
        for p in photos:
            if nas_thumb(cfg, p) or cache_path(p).exists() or known_failure(p, cache_path(p)):
                continue
            todo += 1
            while time.time() - _last_request < YIELD_S:
                time.sleep(0.25)
            if get(cfg, p):
                n += 1
            time.sleep(PREFETCH_PAUSE_S)
        log.info("thumbnails: %d generated, %d skipped", n, todo - n)

    global _worker
    if _worker is not None and _worker.is_alive():
        return _worker                                        # one at a time; the next run picks up the rest
    _worker = threading.Thread(target=work, name="thumbs", daemon=True)
    _worker.start()
    return _worker
