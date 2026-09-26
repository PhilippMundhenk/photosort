"""Shared fixtures. PHOTOSORT_DATA must be set before `app` is imported (module-level paths),
so the whole session uses one scratch data dir that `data_dir` wipes before each test."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

_SESSION_TMP = Path(tempfile.mkdtemp(prefix="photosort-pytest-"))
os.environ["PHOTOSORT_DATA"] = str(_SESSION_TMP / "data")

from app import config  # noqa: E402
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
    d = config.DATA_DIR
    if d.exists():
        shutil.rmtree(d)
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
