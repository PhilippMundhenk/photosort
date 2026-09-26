import json
from pathlib import Path

import pytest

from app import cluster, config, events, ingest, mover


def _props(cfg):
    cfg.sidecar_cleanup = "never"                     # these tests inspect the records of sorted photos
    cfg.dry_run = False                               # these tests move files
    config.save(cfg)
    cluster.run(cfg)
    props = cluster.load_proposals()
    return props, {p["kind"]: p for p in props.values()}


def _force_uncertain(props: dict, pr: dict, i: int = 0) -> None:
    pr["photos"][i].update(conf=0.6, uncertain=True)
    pr["n_uncertain"] = sum(p["uncertain"] for p in pr["photos"])
    cluster.save_proposals(props)


def _jpgs(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*.jpg") if p.is_file())


# --- helpers ---------------------------------------------------------------------------

def test_unique_adds_counter(tmp_path):
    p = tmp_path / "a.jpg"
    assert mover._unique(p) == p
    p.write_bytes(b"x")
    assert mover._unique(p) == tmp_path / "a_1.jpg"
    (tmp_path / "a_1.jpg").write_bytes(b"x")
    assert mover._unique(p) == tmp_path / "a_2.jpg"


def test_target_folder_home_goes_to_unnamed_until_named(cfg):
    root = Path(cfg.root)
    assert mover.target_folder(cfg, {"kind": "trip", "name": "T"}) == root / "T"
    assert mover.target_folder(cfg, {"kind": "local", "name": "L"}) == root / "L"
    assert mover.target_folder(cfg, {"kind": "home", "name": "H"}) == root / cfg.unnamed_dir / "H"
    assert mover.target_folder(cfg, {"kind": "home", "name": "H", "name_edited": True}) == root / "H"   # renamed
    assert mover.target_folder(cfg, {"kind": "home", "name": "H", "manual": True}) == root / "H"


def test_dry_run_blocks_every_file_operation(cfg, library):
    """Dry-run means nothing is moved, copied or deleted, whatever is asked."""
    import pytest as _pytest
    cfg.dry_run = True
    cluster.run(cfg)
    props = cluster.load_proposals()
    kinds = {p["kind"]: p for p in props.values()}
    before = sorted(str(p) for p in library.paths if p.exists())
    with _pytest.raises(mover.DryRun):
        mover.apply(cfg, kinds["trip"], reviewed=True)
    with _pytest.raises(mover.DryRun):
        mover.apply_everyday(cfg, min_age_days=0)
    with _pytest.raises(mover.DryRun):
        mover._transfer(cfg, library.paths[0], Path(cfg.root) / "x")
    with _pytest.raises(mover.DryRun):
        mover._delete_with_sidecars(cfg, library.paths[0])
    # a folder that exists from live times: rename/undo/move_out are blocked too
    cfg.dry_run = False
    m = mover.apply(cfg, kinds["local"])
    folder = mover.target_folder(cfg, kinds["local"])
    cfg.dry_run = True
    with _pytest.raises(mover.DryRun):
        mover.undo(cfg, folder)
    with _pytest.raises(mover.DryRun):
        mover.rename(cfg, folder, "other")
    with _pytest.raises(mover.DryRun):
        mover.move_out(cfg, folder, Path(m["photos"][0]["dst"]))
    assert folder.is_dir() and Path(m["photos"][0]["dst"]).exists()
    after = sorted(str(p) for p in library.paths if p.exists())
    assert set(before) - set(after) == {p["src"] for p in m["photos"]}     # only the deliberate live apply moved


def test_thumb_for(cfg, tmp_path):
    photo = tmp_path / "IMG.jpg"
    assert mover.thumb_for(cfg, photo) is None
    t = tmp_path / "@eaDir" / "IMG.jpg" / "SYNOPHOTO_THUMB_M.jpg"
    t.parent.mkdir(parents=True)
    t.write_bytes(b"x")
    assert mover.thumb_for(cfg, photo) == t
    from app import thumbs
    assert thumbs.nas_thumb(cfg, photo) == t


def test_manifest_roundtrip_and_corruption(tmp_path):
    assert mover.read_manifest(tmp_path) is None
    mover.write_manifest(tmp_path, {"name": "x"})
    assert mover.read_manifest(tmp_path) == {"name": "x"}
    (tmp_path / mover.MANIFEST).write_text("{")
    assert mover.read_manifest(tmp_path) is None


# --- apply -------------------------------------------------------------------------------

def test_apply_moves_by_source_and_uncertain_into_review(cfg, library):
    props, kinds = _props(cfg)
    trip = kinds["trip"]
    excluded = trip["photos"][0]["path"]
    trip["excluded"] = [excluded]
    trip["photos"][1]["uncertain"] = True
    m = mover.apply(cfg, trip)
    folder = mover.target_folder(cfg, trip)

    assert Path(excluded).exists()                                 # excluded photo stays in the inbox
    assert len(m["photos"]) == trip["n"] - 1 and m["name"] == trip["name"] and m["kind"] == "trip"
    assert m["proposal_id"] == trip["id"] and m["label"] is None and m["mode"] == "move"
    assert m["reviewed"] is False
    assert (folder / "phone-a").is_dir() and (folder / "phone-b").is_dir()
    review = folder / trip["photos"][1]["source"] / cfg.review_dir     # photos are in time order, sources mixed
    assert _jpgs(review) == [review / Path(trip["photos"][1]["path"]).name]
    dst = Path(m["photos"][0]["dst"])
    assert dst.exists() and not Path(m["photos"][0]["src"]).exists()
    rec = ingest.read_sidecar(dst)
    assert rec["cluster"] == trip["name"] and rec["decision"] == {"by": "rule", "conf": 1.0, "kind": "trip"}
    assert not ingest.sidecar_path(Path(m["photos"][0]["src"]), cfg).exists()
    ev = events.read(limit=1)[0]
    assert ev["kind"] == "apply" and ev["n"] == len(m["photos"]) and ev["proposal"] == trip["id"]


def test_manual_approval_settles_uncertain_photos(cfg, library):
    props, kinds = _props(cfg)
    local = kinds["local"]
    _force_uncertain(props, local)
    m = mover.apply(cfg, local, reviewed=True)
    folder = mover.target_folder(cfg, local)
    assert m["reviewed"] is True and not list(folder.rglob(cfg.review_dir))
    unc = next(p for p in m["photos"] if p["uncertain"])
    assert unc["in_review"] is False and Path(unc["dst"]).parent == folder / unc["source"]
    assert mover.list_clusters(cfg)[0]["n_review"] == 0
    assert events.read(limit=1)[0]["reviewed"] is True


def test_apply_flat_when_subfolders_off(cfg, library):
    cfg.subfolder_by_source = False
    props, kinds = _props(cfg)
    _force_uncertain(props, kinds["local"])
    m = mover.apply(cfg, kinds["local"])
    folder = mover.target_folder(cfg, kinds["local"])
    assert all(Path(p["dst"]).parent == folder for p in m["photos"] if not p["uncertain"])
    assert (folder / cfg.review_dir).is_dir()                       # the forced low-confidence photo


def test_apply_moves_xmp_sidecar_along(cfg, library):
    props, kinds = _props(cfg)
    local = kinds["local"]
    src = Path(local["photos"][0]["path"])
    src.with_suffix(".xmp").write_text("<x/>")
    m = mover.apply(cfg, local)
    dst = Path(m["photos"][0]["dst"])
    assert dst.with_suffix(".xmp").exists() and not src.with_suffix(".xmp").exists()


def test_apply_skips_vanished_photos(cfg, library):
    props, kinds = _props(cfg)
    local = kinds["local"]
    Path(local["photos"][0]["path"]).unlink()
    m = mover.apply(cfg, local)
    assert len(m["photos"]) == local["n"] - 1


def test_apply_everyday_moves_only_old_unclustered(cfg, library):
    props, kinds = _props(cfg)
    n = mover.apply_everyday(cfg, min_age_days=4)
    root = Path(cfg.root)
    assert n == 10 and _jpgs(root / "2026" / "06" / "phone-a")            # everyday photos, one per day
    assert len(_jpgs(root / "2026")) == 10
    # clustered (pending) photos stayed in the inbox
    assert all(Path(p["path"]).exists() for pr in props.values() for p in pr["photos"])
    ev = events.read(limit=1)[0]
    assert ev["kind"] == "apply_everyday" and ev["n"] == 10 and ev["mode"] == "move"

    cfg.everyday_layout = "leave"
    assert mover.apply_everyday(cfg, 0) == 0


def test_apply_everyday_respects_age_cutoff(cfg, library):
    _props(cfg)
    assert mover.apply_everyday(cfg, min_age_days=10_000) == 0


# --- undo / rename / move_out -------------------------------------------------------------

def test_undo_restores_everything_and_removes_folder(cfg, library):
    before = _jpgs(Path(cfg.inboxes[0]["path"])) + _jpgs(Path(cfg.inboxes[1]["path"]))
    props, kinds = _props(cfg)
    trip = kinds["trip"]
    trip["photos"][1]["uncertain"] = True
    mover.apply(cfg, trip)
    folder = mover.target_folder(cfg, trip)
    assert mover.undo(cfg, folder) == trip["n"]
    assert not folder.exists()
    after = _jpgs(Path(cfg.inboxes[0]["path"])) + _jpgs(Path(cfg.inboxes[1]["path"]))
    assert after == before and all(ingest.read_sidecar(p) for p in after)
    assert events.read(limit=1)[0]["kind"] == "undo"
    assert mover.undo(cfg, folder) == 0                              # nothing to undo twice


def test_rename_unnamed_burst_leaves_unnamed_dir(cfg, library):
    props, kinds = _props(cfg)
    home = kinds["home"]
    mover.apply(cfg, home)
    folder = mover.target_folder(cfg, home)
    assert folder.parent.name == cfg.unnamed_dir
    dst = mover.rename(cfg, folder, "2026-06-30 Hannas Geburtstag")
    assert dst == Path(cfg.root) / "2026-06-30 Hannas Geburtstag" and dst.exists() and not folder.exists()
    m = mover.read_manifest(dst)
    assert m["label"] == m["name"] == "2026-06-30 Hannas Geburtstag"
    assert all(Path(p["dst"]).exists() and Path(p["dst"]).is_relative_to(dst) for p in m["photos"])
    assert ingest.read_sidecar(Path(m["photos"][0]["dst"]))["cluster"] == "2026-06-30 Hannas Geburtstag"
    ev = events.read(limit=1)[0]
    assert ev["kind"] == "label" and ev["old"] == home["name"] and ev["decision_id"] is None


def test_rename_named_cluster_in_place_and_sanitizes(cfg, library):
    props, kinds = _props(cfg)
    mover.apply(cfg, kinds["local"])
    folder = mover.target_folder(cfg, kinds["local"])
    dst = mover.rename(cfg, folder, 'Ludwigsburg: "Barock"?')
    assert dst == Path(cfg.root) / "Ludwigsburg- -Barock-" and dst.is_dir()


def test_move_out_returns_photo_to_its_inbox_and_records_correction(cfg, library):
    props, kinds = _props(cfg)
    trip = kinds["trip"]
    m = mover.apply(cfg, trip)
    folder = mover.target_folder(cfg, trip)
    entry = next(p for p in m["photos"] if p["source"] == "phone-b")
    dst = Path(entry["dst"])
    mover.move_out(cfg, folder, dst)
    back = Path(cfg.inboxes[1]["path"]) / dst.name
    assert back.exists() and not dst.exists() and ingest.read_sidecar(back)
    m2 = mover.read_manifest(folder)
    assert len(m2["photos"]) == len(m["photos"]) - 1
    assert m2["corrections"][0]["dst"] == entry["dst"] and m2["corrections"][0]["corrected"]
    ev = events.read(limit=1)[0]
    assert ev["kind"] == "correction" and ev["photo"] == dst.name and ev["decision_id"] == trip["decision"].get("id")


def test_list_clusters(cfg, library):
    assert mover.list_clusters(cfg) == []
    props, kinds = _props(cfg)
    kinds["trip"]["photos"][0]["uncertain"] = True
    mover.apply(cfg, kinds["trip"])
    mover.apply(cfg, kinds["home"])
    out = mover.list_clusters(cfg)
    assert [c["kind"] for c in out] == ["home", "trip"]              # newest first
    home, trip = out
    assert home["unnamed"] and not trip["unnamed"]
    assert trip["n"] == kinds["trip"]["n"] and trip["n_review"] == 1
    assert trip["rel"] == kinds["trip"]["name"] and Path(trip["folder"]).is_dir()


# --- copy mode -------------------------------------------------------------------------------

@pytest.fixture
def copy_cfg(cfg):
    cfg.copy_instead_of_move = True
    return cfg


def test_copy_mode_keeps_originals_and_marks_them(copy_cfg, library):
    cfg = copy_cfg
    props, kinds = _props(cfg)
    local = kinds["local"]
    m = mover.apply(cfg, local)
    assert m["mode"] == "copy"
    for p in m["photos"]:
        src, dst = Path(p["src"]), Path(p["dst"])
        assert src.exists() and dst.exists() and src.read_bytes() == dst.read_bytes()
        assert ingest.read_sidecar(src)["copied_to"] == str(dst)
        assert ingest.read_sidecar(src)["cluster"] == local["name"]
        assert ingest.read_sidecar(dst)["cluster"] == local["name"] and "copied_to" not in ingest.read_sidecar(dst)

    # already-copied originals are not proposed or sorted again
    local["status"] = "applied"                                      # as the UI does after apply
    cluster.save_proposals(props)
    stats = cluster.run(cfg)
    assert stats["photos"] == library.n - local["n"]
    assert cluster.load_proposals()[local["id"]]["status"] == "applied"   # kept as history
    assert mover.apply_everyday(cfg, min_age_days=4) == 10
    assert len(_jpgs(Path(cfg.inboxes[0]["path"]))) + len(_jpgs(Path(cfg.inboxes[1]["path"]))) == library.n
    assert mover.apply_everyday(cfg, min_age_days=4) == 0             # marked, not copied twice


def test_copy_mode_undo_deletes_copies_and_clears_marks(copy_cfg, library):
    cfg = copy_cfg
    props, kinds = _props(cfg)
    trip = kinds["trip"]
    m = mover.apply(cfg, trip)
    folder = mover.target_folder(cfg, trip)
    assert mover.undo(cfg, folder) == trip["n"]
    assert not folder.exists()
    for p in m["photos"]:
        rec = ingest.read_sidecar(Path(p["src"]))
        assert Path(p["src"]).exists() and "copied_to" not in rec and rec["cluster"] is None
    assert cluster.run(cfg)["photos"] == library.n    # everything is sortable again


def test_copy_mode_move_out_deletes_copy_only(copy_cfg, library):
    cfg = copy_cfg
    props, kinds = _props(cfg)
    m = mover.apply(cfg, kinds["local"])
    folder = mover.target_folder(cfg, kinds["local"])
    entry = m["photos"][0]
    mover.move_out(cfg, folder, Path(entry["dst"]))
    assert not Path(entry["dst"]).exists() and not ingest.sidecar_path(Path(entry["dst"]), cfg).exists()
    assert Path(entry["src"]).exists() and "copied_to" not in ingest.read_sidecar(Path(entry["src"]))
    assert len(mover.read_manifest(folder)["photos"]) == len(m["photos"]) - 1
    assert events.read(limit=1)[0]["kind"] == "correction"


def test_copy_mode_rename_updates_source_marks(copy_cfg, library):
    cfg = copy_cfg
    props, kinds = _props(cfg)
    m = mover.apply(cfg, kinds["home"])
    dst = mover.rename(cfg, mover.target_folder(cfg, kinds["home"]), "2026-06-30 Geburtstag")
    src_rec = ingest.read_sidecar(Path(m["photos"][0]["src"]))
    assert src_rec["cluster"] == "2026-06-30 Geburtstag"
    assert Path(src_rec["copied_to"]).is_relative_to(dst) and Path(src_rec["copied_to"]).exists()


def test_manifest_json_is_valid_and_complete(cfg, library):
    props, kinds = _props(cfg)
    mover.apply(cfg, kinds["local"])
    folder = mover.target_folder(cfg, kinds["local"])
    m = json.loads((folder / mover.MANIFEST).read_text(encoding="utf-8"))
    assert set(m) >= {"name", "kind", "start", "end", "proposal_id", "decision", "applied", "label", "photos", "mode"}
    assert set(m["photos"][0]) == {"src", "dst", "conf", "zone", "media", "source", "inbox", "uncertain", "in_review"}
