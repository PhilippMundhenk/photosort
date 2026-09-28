"""Many runs, approvals, undos and page loads in a row: nothing accumulates (queues, progress
dicts, caches, threads) and memory does not creep. A leak that costs a few hundred kilobytes per
run would take the NAS down after a month of ten-minute scans."""
from __future__ import annotations

import gc
import threading
import tracemalloc

import pytest
from fastapi.testclient import TestClient

from app import cluster, config, main, mover, thumbs


@pytest.fixture
def client(library):
    main._state.update({"last_run": None, "last_stats": {}, "running": False, "error": None, "progress": None,
                        "runs": 0})
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def _cycle(client, cfg) -> None:
    main.run_pipeline("schedule")
    main.wait_for_apply()
    for path in ("/", "/review", "/everyday", "/clusters", "/log", "/settings", "/api/status"):
        assert client.get(path).status_code == 200
    props = cluster.load_proposals()
    local = next((p for p in props.values() if p["kind"] == "local" and p["status"] == "pending"), None)
    if local:
        client.post(f"/proposal/{local['id']}/toggle", data={"path": local["photos"][0]["path"]},
                    headers={"X-Requested-With": "fetch"})
        client.post(f"/proposal/{local['id']}/approve")
        main.wait_for_apply()
        client.post("/cluster/undo", data={"folder": str(mover.target_folder(cfg, local))})
        props = cluster.load_proposals()
        props[local["id"]]["status"] = "pending"                          # undo marks it rejected: reopen
        cluster.save_proposals(props)


def test_thirty_cycles_leave_nothing_behind(client, library):
    cfg = config.load()
    cfg.dry_run = False
    config.save(cfg)
    threads_before = {t.name for t in threading.enumerate()}
    for _ in range(5):
        _cycle(client, cfg)
    gc.collect()
    tracemalloc.start()
    base = tracemalloc.take_snapshot()
    for _ in range(25):
        _cycle(client, cfg)
    gc.collect()
    now = tracemalloc.take_snapshot()
    tracemalloc.stop()
    growth = sum(st.size_diff for st in now.compare_to(base, "filename") if st.size_diff > 0)
    print(f"\n  soak  memory growth over 25 cycles: {growth / 1024:.0f} kB")
    assert growth < 8 * 1024 * 1024, f"{growth / 1024:.0f} kB grew over 25 cycles"
    assert main._apply_queue.empty() and not main._applying and not main._state["running"]
    assert main._state["runs"] == 30
    extra = {t.name for t in threading.enumerate()} - threads_before - {"apply", "thumbs", "warm"}
    assert not [t for t in extra if not t.startswith(("Thread-", "asyncio", "AnyIO", "ThreadPool"))], extra
    assert len(cluster.load_proposals()) < 20                                # applied/rejected history stays bounded
    thumbs.wait()
    assert all(p.exists() for p in library.paths)                            # everything undone again
