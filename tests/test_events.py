import json

from app import events


def test_log_appends_json_lines(data_dir):
    ev = events.log("run", n=1)
    assert ev["kind"] == "run" and ev["n"] == 1 and ev["ts"].endswith("+00:00")
    events.log("decision", id="d1", by="kev", conf=0.9)
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
    assert all(b["n"] == 0 and b["acc"] is None for b in events.calibration())


def test_calibration_buckets_kev_decisions_against_corrections(data_dir):
    events.log("decision", id="a", by="kev", conf=0.95)   # bucket 4, correct
    events.log("decision", id="b", by="kev", conf=0.9)    # bucket 4, corrected -> wrong
    events.log("decision", id="c", by="kev", conf=0.55)   # bucket 2, correct
    events.log("decision", id="d", by="rule", conf=0.99)  # rule decisions are not calibrated
    events.log("decision", id="e", by="kev", conf=1.0)    # clamps into the last bucket
    events.log("correction", decision_id="b")
    events.log("correction", decision_id="b")             # duplicate correction counts once
    b = events.calibration(bins=5)
    assert [x["n"] for x in b] == [0, 0, 1, 0, 3]
    assert b[4]["wrong"] == 1 and b[4]["acc"] == 0.67 and b[2]["acc"] == 1.0
    assert (b[0]["lo"], b[0]["hi"], b[4]["hi"]) == (0.0, 0.2, 1.0)
