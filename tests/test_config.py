import os

import yaml

from app import config


def test_defaults_and_roundtrip(data_dir):
    cfg = config.Config(home_lat=48.9, inboxes=[{"path": "/x", "name": "x"}])
    config.save(cfg)
    assert config.CONFIG_PATH.exists() and not config.CONFIG_PATH.with_suffix(".tmp").exists()
    loaded = config.load()
    assert loaded == cfg
    assert yaml.safe_load(config.CONFIG_PATH.read_text())["home_lat"] == 48.9


def test_load_ignores_unknown_keys(data_dir):
    config.CONFIG_PATH.write_text("home_lat: 1.5\nbogus: 3\n")
    cfg = config.load()
    assert cfg.home_lat == 1.5 and not hasattr(cfg, "bogus")


def test_load_without_file_gives_defaults(data_dir):
    assert config.load() == config.Config()


def test_update_from_form_coercion():
    cfg = config.Config()
    form = {
        "home_lat": "48.944", "home_radius_km": "1", "scan_interval_min": "15",
        "dry_run": "on", "subfolder_by_source": "on",
        "photo_extensions": ".JPG, heic , mp4",
        "inboxes": "phone-a=/photos/a\n\n/photos/b\ncam = /photos/c \n",
        "root": "/photos/sorted", "unnamed_dir": "  _todo ",
        "unknown_field": "ignored",
    }
    out = config.update_from_form(cfg, form)
    assert out.home_lat == 48.944 and isinstance(out.home_lat, float)
    assert out.home_radius_km == 1.0 and out.scan_interval_min == 15
    assert out.dry_run is True and out.subfolder_by_source is True
    assert out.write_xmp_sidecar is False           # checkbox absent from the form -> unticked
    assert out.auto_apply_trips is False
    assert out.photo_extensions == ["jpg", "heic", "mp4"]
    assert out.inboxes == [{"path": "/photos/a", "name": "phone-a"},
                           {"path": "/photos/b", "name": "b"},
                           {"path": "/photos/c", "name": "cam"}]
    assert out.unnamed_dir == "_todo"
    assert not hasattr(out, "unknown_field")


def test_named_places_parsing_and_text():
    txt = "Black Forest = 48.0, 8.2, 40\n\nbad line\nAllotment=52.52;13.40\n = 1,2\nX = 1, 2, 0"
    places = config.parse_named_places(txt)
    assert places == [{"name": "Black Forest", "lat": 48.0, "lon": 8.2, "radius_km": 40.0},
                      {"name": "Allotment", "lat": 52.52, "lon": 13.4, "radius_km": 2.0},
                      {"name": "X", "lat": 1.0, "lon": 2.0, "radius_km": 0.05}]
    cfg = config.update_from_form(config.Config(), {"named_places": txt})
    assert cfg.named_places == places
    assert config.named_places_text(cfg).splitlines()[0] == "Black Forest = 48.00000, 8.20000, 40"
    assert config.parse_named_places(config.named_places_text(cfg)) == places      # round trip


def test_inputs_are_discovered_from_the_inbox_root(tmp_path):
    root = tmp_path / "inbox"
    root.mkdir()
    cfg = config.Config(inbox_root=str(root), root=str(tmp_path / "sorted"))
    assert config.inbox_dirs(cfg) == []                                   # empty root: nothing yet
    for name in ("hans", "phone-b", ".hidden", "@eaDir"):
        (root / name).mkdir()
    (tmp_path / "sorted").mkdir()
    assert config.inbox_dirs(cfg) == [("hans", root / "hans"), ("phone-b", root / "phone-b")]
    assert config.inboxes_text(cfg) == ""
    cfg.inboxes = [{"path": str(root / "hans"), "name": "Hans"}]        # explicit lines win
    assert config.inbox_dirs(cfg) == [("Hans", root / "hans")]
    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "a.jpg").write_bytes(b"x")
    assert config.inbox_dirs(config.Config(inbox_root=str(flat))) == [("flat", flat)]   # files, no subfolders
    assert config.inbox_dirs(config.Config(inbox_root=str(tmp_path / "nope"))) == []


def test_inbox_helpers():
    cfg = config.Config(inboxes=[{"path": "/p/a", "name": "A"}, {"path": "/p/b"}])
    dirs = config.inbox_dirs(cfg)
    assert [n for n, _ in dirs] == ["A", "b"]
    assert config.inboxes_text(cfg) == "A=/p/a\nb=/p/b"
    assert os.path.basename(str(dirs[1][1])) == "b"


def test_corrupt_or_odd_config_files_fall_back_to_defaults(data_dir):
    """A hand-edited config.yaml with broken YAML, a list instead of a mapping, or a field of the
    wrong type must not take the service down; the defaults apply and the file is left alone."""
    for text in ("{not yaml: [", "- a\n- b\n", "dry_run: maybe\nhome_lat: north\n", "", "42"):
        config.CONFIG_PATH.write_text(text, encoding="utf-8")
        config._cache["key"] = None
        cfg = config.load()
        assert isinstance(cfg.dry_run, bool) and isinstance(cfg.home_lat, float), text
        assert config.CONFIG_PATH.read_text(encoding="utf-8") == text                 # not rewritten


def test_yaml_values_are_coerced_to_the_field_types(data_dir):
    """Every field type, right and wrong: booleans from words, numbers from strings, lists and
    dicts only as such, anything else keeps the default and is reported, never a crash."""
    c = config._coerce
    assert c(True, False, "x") is False and c(True, "no", "x") is False and c(False, "yes", "x") is True
    assert c(True, "maybe", "x") is True and c(False, 3, "x") is False           # wrong kind: default kept
    assert c(10, "12", "x") == 12 and c(10, 7.0, "x") == 7 and c(10, "many", "x") == 10
    assert c(10, True, "x") == 10
    assert c(0.5, "1.25", "x") == 1.25 and c(0.5, 2, "x") == 2.0 and c(0.5, "north", "x") == 0.5
    assert c("a", 5, "x") == "5" and c("a", 1.5, "x") == "1.5" and c("a", True, "x") == "a"
    assert c("a", ["l"], "x") == "a"
    assert c([1], [2, 3], "x") == [2, 3] and c([1], "2,3", "x") == [1]
    assert c({}, {"k": 1}, "x") == {"k": 1} and c({}, [1], "x") == {}
    assert c(None, "anything", "x") == "anything"
    text = "dry_run: 'off'\nscan_interval_min: '3'\nhome_lat: '48.9'\ninboxes: nope\n"
    text += "photo_extensions: [jpg]\ntimezone: 7\n"
    config.CONFIG_PATH.write_text(text, encoding="utf-8")
    config._cache["key"] = None
    cfg = config.load()
    assert cfg.dry_run is False and cfg.scan_interval_min == 3 and cfg.home_lat == 48.9
    assert cfg.inboxes == [] and cfg.photo_extensions == ["jpg"] and cfg.timezone == "7"
