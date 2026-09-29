"""Shared fixtures. PHOTOSORT_DATA must be set before `app` is imported (module-level paths),
so the whole session uses one scratch data dir that `data_dir` wipes before each test."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

_SESSION_TMP = Path(tempfile.mkdtemp(prefix="photosort-pytest-"))
os.environ["PHOTOSORT_DATA"] = str(_SESSION_TMP / "data")

from app import config, thumbs  # noqa: E402

thumbs.YIELD_S = thumbs.PREFETCH_PAUSE_S = 0.0     # the prefetch's politeness only slows the suite down
thumbs.IN_PROCESS = True                           # decode in-process so coverage sees the generators;
                                                   # tests of the helper process switch it off themselves
from tests import synth  # noqa: E402


@pytest.fixture
def data_dir() -> Path:
    import threading

    from app import thumbs
    thumbs.wait()                          # a prefetch from the previous test must not write into the wiped dir
    for t in threading.enumerate():        # nor may the startup cache warm-up still be reading it
        if t.name == "warm":
            t.join(60)
    if any(t.name == "apply" for t in threading.enumerate()):
        from app import main
        main.wait_for_apply(60)            # nor a background move started by a web test
    import sys
    if "app.main" in sys.modules:
        from app import main
        with main._lock:                   # nor a pipeline run a previous test's scheduler kicked off:
            pass                           # it would write its proposals into the wiped dir
    d = config.DATA_DIR
    for attempt in range(3):               # a thumbnail written by a worker finishing this instant
        try:
            if d.exists():
                shutil.rmtree(d)
            break
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.5)
    d.mkdir(parents=True)
    return d


@pytest.fixture
def photos(tmp_path: Path, data_dir: Path) -> Path:
    """Empty inbox/sorted layout under tmp_path with a saved config; no photos yet."""
    base = tmp_path / "photos"
    for sub in ("phone-a", "phone-b", "sorted"):
        (base / sub).mkdir(parents=True)
    config.save(synth.make_config(base))
    return base


@pytest.fixture
def cfg(photos: Path) -> config.Config:
    return config.load()


@pytest.fixture
def library(cfg: config.Config) -> synth.Library:
    return synth.populate(cfg)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_SESSION_TMP, ignore_errors=True)
