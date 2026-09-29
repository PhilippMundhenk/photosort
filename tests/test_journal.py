"""The transaction journal: every action that moves files is one batch of one line per file
operation; any batch, or one file of it, can be reverted as far as the files still are where the
batch left them; the History page shows it all and traces a single file."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cluster, config, journal, main, mover


@pytest.fixture
def client(library):
    main._state.update({"last_run": None, "last_stats": {}, "running": False, "error": None, "progress": None,
                        "runs": 0})
    with TestClient(main.app, follow_redirects=False) as c:
        yield c


def _kind(kind: str) -> dict:
    return next(p for p in cluster.load_proposals().values() if p["kind"] == kind)


def _apply(cfg: config.Config, pr: dict) -> None:
    """What the worker does: the files, then the status."""
    mover.apply(cfg, pr, reviewed=True)
    props = cluster.load_proposals()
    props[pr["id"]]["status"] = "applied"
    cluster.save_proposals(props)


def _live() -> config.Config:
    cfg = config.load()
    cfg.dry_run = False
    config.save(cfg)
    cluster.run(cfg)
    return cfg


# --- what gets written -----------------------------------------------------------------------------

def test_every_action_is_one_batch_with_one_line_per_file(cfg, library):
    _live()
    local = _kind("local")
    assert journal.batches() == []
    mover.apply(cfg, local, reviewed=True)
    b = journal.batches()
    assert len(b) == 1 and b[0]["action"] == "apply" and b[0]["n"] == local["n"]
    assert b[0]["meta"] == {"proposal": local["id"], "name": local["name"], "kind": "local",
                            "folder": str(mover.target_folder(cfg, local))}
    assert all(op["op"] == "move" and Path(op["dst"]).exists() and not Path(op["src"]).exists() for op in b[0]["ops"])
    folder = mover.target_folder(cfg, local)
    dst = Path(b[0]["ops"][0]["dst"])
    mover.move_out(cfg, folder, dst)
    mover.put_back(cfg, folder, Path(b[0]["ops"][0]["src"]))
    new = mover.rename(cfg, folder, "2026-06-27 Barock")
    mover.apply_everyday(cfg, 0)
    mover.undo(cfg, new)
    actions = [x["action"] for x in journal.batches()]
    assert actions == ["undo", "move_everyday", "rename", "put_back", "move_out", "apply"]
    rename_ops = next(x for x in journal.batches() if x["action"] == "rename")["ops"]
    assert [o["op"] for o in rename_ops] == ["rename_dir"] and rename_ops[0]["dst"] == str(new)
    lines = journal.JOURNAL_PATH.read_text(encoding="utf-8").splitlines()
    assert all(json.loads(line) for line in lines)                        # one JSON object per line


def test_a_file_operation_outside_any_action_is_still_journaled(cfg, library):
    src = library.paths[0]
    dst = mover._transfer(cfg, src, Path(cfg.root) / "loose")
    b = journal.batches()
    assert len(b) == 1 and b[0]["action"] == "file" and b[0]["ops"][0]["dst"] == str(dst)


def test_trace_tells_where_a_file_is_now(cfg, library):
    _live()
    local = _kind("local")
    mover.apply(cfg, local, reviewed=True)
    name = Path(local["photos"][0]["path"]).name
    t = journal.trace(name)
    assert len(t) == 1 and t[0]["action"] == "apply" and t[0]["now"] == t[0]["dst"]
    mover.undo(cfg, mover.target_folder(cfg, local))
    t = journal.trace(name)
    assert [e["action"] for e in t] == ["undo", "apply"] and t[0]["now"] == local["photos"][0]["path"]
    assert journal.trace("") == [] and journal.trace("no-such-file") == []


def test_a_batch_opened_inside_another_joins_it_and_a_damaged_journal_still_reads(data_dir):
    with journal.batch("nothing-happened"):
        pass
    assert journal.read_all() == []                                          # an action that moved nothing: no trace
    with journal.batch("outer") as a, journal.batch("inner") as b:
        assert a == b
        journal.record("move", "/x/a.jpg", "/y/a.jpg")
    assert [x["action"] for x in journal.batches()] == ["outer"] and journal.batches()[0]["n"] == 1
    with journal.JOURNAL_PATH.open("a", encoding="utf-8") as f:
        stray = json.dumps({"ts": "2026-09-29T00:00:00+00:00", "op": "note"})
        f.write("\n{not json\n" + stray + "\n")
    assert len(journal.read_all()) == 4 and len(journal.batches()) == 1      # a blank, a broken and a stray line
    assert journal.trace("a.jpg")[0]["now"] is None                          # nowhere on disk


# --- revert ------------------------------------------------------------------------------------------

def test_revert_of_an_apply_puts_everything_back_and_marks_the_proposal_rejected(cfg, library):
    _live()
    local = _kind("local")
    _apply(cfg, local)
    folder = mover.target_folder(cfg, local)
    batch = journal.batches()[0]["batch"]
    result = journal.revert(cfg, batch)
    assert result["reverted"] == local["n"] and result["skipped"] == []
    assert all(Path(p["path"]).exists() for p in local["photos"]) and not folder.exists()
    assert cluster.load_proposals()[local["id"]]["status"] == "rejected"
    b = journal.batches()
    assert b[0]["action"] == "revert" and b[0]["meta"]["of"] == batch and b[1]["reverted_by"] == b[0]["batch"]
    props = cluster.load_proposals()                                        # the proposal is pruned meanwhile
    del props[local["id"]]
    cluster.save_proposals(props)
    again = journal.revert(cfg, b[0]["batch"])                              # a revert can be reverted
    assert again["reverted"] == local["n"] and folder.is_dir()
    assert len(mover.read_manifest(folder)["photos"]) == local["n"]        # the folder has its manifest back
    assert local["id"] not in cluster.load_proposals()                     # and no status is invented
    result = journal.revert(cfg, batch)                                    # the apply reverted a second time:
    assert result["reverted"] == local["n"] and not folder.exists()        # same result, no status invented
    mover.wait_for_clusters()


def test_revert_skips_files_that_moved_on_and_puts_back_one_file_only(cfg, library):
    _live()
    local = _kind("local")
    _apply(cfg, local)
    folder = mover.target_folder(cfg, local)
    batch = journal.batches()[0]["batch"]
    gone = Path(local["photos"][0]["path"])
    first_dst = Path(next(op["dst"] for op in journal.get(batch)["ops"] if op["src"] == str(gone)))
    first_dst.unlink()                                                     # deleted by hand meanwhile
    one = journal.revert(cfg, batch, only=local["photos"][1]["path"])       # one file back
    assert one["reverted"] == 1 and Path(local["photos"][1]["path"]).exists()
    assert len(mover.read_manifest(folder)["photos"]) == local["n"] - 1     # its manifest entry went too
    assert cluster.load_proposals()[local["id"]]["status"] == "applied"     # one file: the cluster stays
    rest = journal.revert(cfg, batch)
    assert rest["reverted"] == local["n"] - 2                              # the deleted one and the done one
    assert len(rest["skipped"]) == 2 and any("not there any more" in s["why"] for s in rest["skipped"])
    assert cluster.load_proposals()[local["id"]]["status"] == "applied"     # not everything came back: stays
    assert [Path(p["dst"]) for p in mover.read_manifest(folder)["photos"]] == [first_dst]   # the manifest still
    assert not any(p.suffix == ".jpg" for p in folder.iterdir())           # lists the hand-deleted file: it stays


def test_revert_of_an_everyday_move_and_of_a_rename(cfg, library):
    cfg = _live()
    cfg.everyday_keep_days = 0
    n = mover.apply_everyday(cfg, 0)
    batch = journal.batches()[0]["batch"]
    everyday = [Path(op["src"]) for op in journal.get(batch)["ops"]]
    assert n == len(everyday) == 10 and not any(p.exists() for p in everyday)
    result = journal.revert(cfg, batch)
    assert result["reverted"] == 10 and all(p.exists() for p in everyday)
    assert not (Path(cfg.root) / "2026").exists()
    home = _kind("home")
    mover.apply(cfg, home, reviewed=True)
    folder = mover.target_folder(cfg, home)                                 # _unnamed/<name>
    new = mover.rename(cfg, folder, "2026-06-30 Hannas Geburtstag")
    assert new.parent == Path(cfg.root)
    result = journal.revert(cfg, journal.batches()[0]["batch"])
    assert result["reverted"] == 1 and folder.is_dir() and not new.exists()   # back into _unnamed
    assert mover.read_manifest(folder)["name"] == folder.name
    again = journal.revert(cfg, journal.batches()[0]["batch"])              # and forward again
    assert again["reverted"] == 1 and new.is_dir() and not folder.exists()
    mover.wait_for_clusters()
    folder.mkdir(parents=True)                                               # something else at the old place
    blocked = journal.revert(cfg, again["batch"])
    assert blocked["reverted"] == 0 and "something else" in blocked["skipped"][0]["why"]
    folder.rmdir()
    shutil.rmtree(new)                                                       # the folder itself is gone
    gone = journal.revert(cfg, again["batch"])
    assert gone["reverted"] == 0 and "folder is not there" in gone["skipped"][0]["why"]


def test_reverting_a_reverted_everyday_move_moves_it_forward_again(cfg, library):
    cfg = _live()
    cfg.everyday_keep_days = 0
    mover.apply_everyday(cfg, 0)
    first = journal.revert(cfg, journal.batches()[0]["batch"])
    forward = journal.revert(cfg, journal.batches()[0]["batch"])            # the revert of the revert
    assert first["reverted"] == forward["reverted"] == 10
    assert all(Path(op["src"]).exists() for op in forward["done"]) and not journal.batches()[0]["meta"].get("folder")
    one = journal.revert(cfg, journal.batches()[0]["batch"], only=forward["done"][0]["src"])
    assert one["reverted"] == 1


def test_a_file_that_cannot_be_moved_back_is_reported_not_guessed(cfg, library, monkeypatch):
    _live()
    local = _kind("local")
    _apply(cfg, local)
    batch = journal.batches()[0]["batch"]
    monkeypatch.setattr(mover, "_transfer", lambda *a, **k: (_ for _ in ()).throw(PermissionError("read-only")))
    result = journal.revert(cfg, batch)
    assert result["reverted"] == 0 and all("PermissionError" in s["why"] for s in result["skipped"])
    assert cluster.load_proposals()[local["id"]]["status"] == "applied"     # nothing came back: unchanged


def test_revert_respects_dry_run_and_refuses_unknown_batches(cfg, library):
    _live()
    local = _kind("local")
    mover.apply(cfg, local, reviewed=True)
    batch = journal.batches()[0]["batch"]
    cfg.dry_run = True
    with pytest.raises(mover.DryRun):
        journal.revert(cfg, batch)
    assert all(not Path(p["path"]).exists() for p in local["photos"])       # nothing moved
    cfg.dry_run = False
    with pytest.raises(KeyError):
        journal.revert(cfg, "nope")


def test_revert_of_a_copy_deletes_the_copy_and_frees_the_original(cfg, library):
    cfg = _live()
    cfg.copy_instead_of_move = True
    local = _kind("local")
    mover.apply(cfg, local, reviewed=True)
    batch = journal.batches()[0]
    assert all(op["op"] == "copy" for op in batch["ops"])
    result = journal.revert(cfg, batch["batch"])
    assert result["reverted"] == local["n"] and not mover.target_folder(cfg, local).exists()
    assert all(Path(p["path"]).exists() for p in local["photos"])
    from app import ingest
    assert all("copied_to" not in (ingest.read_sidecar(Path(p["path"]), cfg) or {}) for p in local["photos"])
    deletion = journal.batches()[0]
    assert all(op["op"] == "delete" for op in deletion["ops"]) and deletion["n"] == local["n"]
    partial = journal.revert(cfg, batch["batch"])                           # the copies are gone already
    assert partial["reverted"] == 0 and len(partial["skipped"]) == local["n"]
    assert all("copy is not there" in s["why"] for s in partial["skipped"])
    result = journal.revert(cfg, deletion["batch"])                         # a deletion cannot be undone
    assert result["reverted"] == 0 and len(result["skipped"]) == local["n"]
    assert all("cannot be restored" in s["why"] for s in result["skipped"])


def test_revert_of_an_undo_puts_the_cluster_back_with_manifest_and_status(cfg, library):
    _live()
    local = _kind("local")
    _apply(cfg, local)
    folder = mover.target_folder(cfg, local)
    mover.undo(cfg, folder)
    props = cluster.load_proposals()
    props[local["id"]]["status"] = "rejected"                                # what the page does after an undo
    cluster.save_proposals(props)
    assert not folder.exists()
    result = journal.revert(cfg, journal.batches()[0]["batch"])
    assert result["reverted"] == local["n"] and result["skipped"] == []
    m = mover.read_manifest(folder)
    assert len(m["photos"]) == local["n"] and m["proposal_id"] == local["id"] and m["name"] == local["name"]
    assert cluster.load_proposals()[local["id"]]["status"] == "applied"
    mover.wait_for_clusters()
    assert folder in [Path(c["folder"]) for c in mover.list_clusters(cfg)]   # and it is a cluster again


# --- the page -----------------------------------------------------------------------------------------

def test_history_page_lists_actions_reverts_and_traces(client, library):
    cfg = _live()
    local = _kind("local")
    client.post(f"/proposal/{local['id']}/approve")
    main.wait_for_apply()
    html = client.get("/history").text
    assert "apply" in html and local["name"] in html and ">Revert</button>" in html
    name = Path(local["photos"][0]["path"]).name
    html = client.get("/history", params={"file": name}).text
    assert "now at" in html and "put back where it was" in html and "1 operation touched" in html
    batch = journal.batches()[0]["batch"]
    r = client.post(f"/history/{batch}/revert",
                    data={"only": local["photos"][0]["path"], "back": f"/history?file={name}"})
    assert r.status_code == 303 and "1 file put back" in r.headers["location"].replace("%20", " ")
    assert Path(local["photos"][0]["path"]).exists()
    r = client.post(f"/history/{batch}/revert")
    assert r.status_code == 303 and f"{local['n'] - 1} files put back" in r.headers["location"].replace("%20", " ")
    assert cluster.load_proposals()[local["id"]]["status"] == "applied"     # one had gone before: stays
    html = client.get("/history").text
    assert html.count('<span class="badge warn">revert</span>') == 2
    assert client.post("/history/nope/revert").status_code == 404
    assert client.post(f"/history/{batch}/revert", data={"back": "//evil"}).headers["location"].startswith("/history")
    assert "Nothing has moved yet" not in html
    cfg.dry_run = True
    config.save(cfg)
    assert client.post(f"/history/{batch}/revert").status_code == 409     # dry-run: refused, like every move


def test_deleting_a_missing_file_leaves_no_trace_and_a_strangers_file_is_never_removed(cfg, library):
    """Only what really happened is on record; a revert empties a cluster folder of what the
    action moved in, but a file somebody else put there stays, and so does the folder."""
    mover._delete_with_sidecars(cfg, Path(cfg.root) / "never-there.jpg")
    assert journal.batches() == []
    _live()
    local = _kind("local")
    _apply(cfg, local)
    folder = mover.target_folder(cfg, local)
    (folder / "notes.txt").write_text("mine", encoding="utf-8")
    result = journal.revert(cfg, journal.batches()[0]["batch"])
    assert result["reverted"] == local["n"] and result["skipped"] == []
    assert [p.name for p in folder.iterdir()] == ["notes.txt"]              # no manifest, no photos: not a cluster
    assert cluster.load_proposals()[local["id"]]["status"] == "rejected"
