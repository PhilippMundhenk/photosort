import json

from app import events


def test_log_appends_json_lines(data_dir):
    ev = events.log("run", n=1)
    assert ev["kind"] == "run" and ev["n"] == 1 and ev["ts"].endswith("+00:00")
    events.log("review", id="d1", action="approve")
    lines = events.EVENTS_PATH.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[1])["id"] == "d1"


def test_read_newest_first_with_limit_and_filter(data_dir):
    for i in range(5):
        events.log("a" if i % 2 else "b", i=i)
    rows = events.read()
    assert [r["i"] for r in rows] == [4, 3, 2, 1, 0]
    assert [r["i"] for r in events.read(limit=2)] == [4, 3]
    assert [r["i"] for r in events.read(kind="a")] == [3, 1]
    assert events.read(kind="zzz") == []


def test_read_skips_blank_and_broken_lines(data_dir):
    events.log("ok", i=1)
    with events.EVENTS_PATH.open("a", encoding="utf-8") as f:
        f.write("\n{not json}\n")
    events.log("ok", i=2)
    assert [r["i"] for r in events.read()] == [2, 1]


def test_read_without_file(data_dir):
    assert events.read() == []

