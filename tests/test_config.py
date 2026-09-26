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


def test_kev_url_seeded_from_environment(monkeypatch):
    monkeypatch.setenv("PHOTOSORT_KEV_URL", "http://laya:8000")
    assert config.Config().kev_url == "http://laya:8000"
    monkeypatch.delenv("PHOTOSORT_KEV_URL")
    assert config.Config().kev_url == ""


def test_saved_value_wins_over_environment(data_dir, monkeypatch):
    monkeypatch.setenv("PHOTOSORT_KEV_URL", "http://laya:8000")
    config.save(config.Config(kev_url=""))
    assert config.load().kev_url == ""


def test_update_from_form_coercion():
    cfg = config.Config()
    form = {
        "home_lat": "48.944", "home_radius_km": "1", "scan_interval_min": "15",
        "dry_run": "on", "subfolder_by_source": "on",
        "photo_extensions": ".JPG, heic , mp4",
        "inboxes": "phone-a=/photos/a\n\n/photos/b\ncam = /photos/c \n",
        "kev_url": "  http://laya:8000/ ", "root": "/photos/sorted",
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
    assert out.kev_url == "http://laya:8000/"
    assert not hasattr(out, "unknown_field")


def test_inbox_helpers():
    cfg = config.Config(inboxes=[{"path": "/p/a", "name": "A"}, {"path": "/p/b"}])
    dirs = config.inbox_dirs(cfg)
    assert [n for n, _ in dirs] == ["A", "b"]
    assert config.inboxes_text(cfg) == "A=/p/a\nb=/p/b"
    assert os.path.basename(str(dirs[1][1])) == "b"
