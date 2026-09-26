"""The System One adapter against a real Laya server (docker-compose.test.yml, profile
`integration`). Skipped unless PHOTOSORT_LAYA_URL points at one."""
from __future__ import annotations

import os
import time

import httpx
import pytest

from app import config, events
from app.cluster import HOME_BURST_CRITERIA, HOME_BURST_INSTRUCTIONS, HOME_BURST_OPTIONS
from app.kev import Decider, HttpBackend

URL = os.environ.get("PHOTOSORT_LAYA_URL", "")
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not URL, reason="PHOTOSORT_LAYA_URL not set")]

BIRTHDAY = {"photos": 25, "duration_h": 3.6, "weekday": "Saturday", "start_hour": 14, "end_hour": 17,
            "devices": 3, "burst_ratio": 2.08, "date": "2026-06-27"}
CHORES = {"photos": 13, "duration_h": 8.7, "weekday": "Tuesday", "start_hour": 8, "end_hour": 17,
          "devices": 1, "burst_ratio": 1.08, "date": "2026-06-30"}


@pytest.fixture(scope="module")
def backend():
    deadline = time.time() + 900                       # first start downloads the checkpoint
    while time.time() < deadline:
        try:
            if httpx.get(URL + "/", timeout=5).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(5)
    else:
        pytest.fail(f"Laya at {URL} did not become healthy")
    return HttpBackend(config.Config(kev_url=URL, kev_timeout_s=60))


def test_health_and_models(backend):
    assert backend.health()["ok"]
    models = httpx.get(URL + "/v1/models", timeout=10).json()["models"]
    assert any(m["name"] == "laya" for m in models)


@pytest.mark.parametrize("state", [BIRTHDAY, CHORES], ids=["birthday", "chores"])
def test_choice_returns_calibrated_distribution(backend, state):
    best, probs, conf = backend.choice(state, HOME_BURST_OPTIONS, HOME_BURST_INSTRUCTIONS, HOME_BURST_CRITERIA)
    assert best in HOME_BURST_OPTIONS and set(probs) == set(HOME_BURST_OPTIONS)
    assert abs(sum(probs.values()) - 1) < 0.01 and best == max(probs, key=probs.get)
    assert 0 <= conf <= 1
    t0 = time.time()
    backend.choice(state, HOME_BURST_OPTIONS, HOME_BURST_INSTRUCTIONS, HOME_BURST_CRITERIA)
    assert time.time() - t0 < 30                       # warm request; CPU laptops do this in < 1 s


def test_noul(backend):
    p, conf = backend.noul(BIRTHDAY, "Were several people taking photos?")
    assert 0 <= p <= 1 and 0 <= conf <= 1


def test_decider_logs_model_decisions(backend, data_dir):
    d = Decider(config.Config(kev_url=URL, kev_timeout_s=60))
    assert d.status()["backend"] == "kev" and d.status()["ok"]
    dec = d.choice("home_burst", BIRTHDAY, HOME_BURST_OPTIONS, HOME_BURST_INSTRUCTIONS, HOME_BURST_CRITERIA)
    assert dec["by"] == "kev"
    ev = events.read(limit=1, kind="decision")[0]
    assert ev["id"] == dec["id"] and "error" not in ev and ev["probs"]
