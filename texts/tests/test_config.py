"""Settings: the install name decides the folder; a missing file is an empty allow-list."""

from pathlib import Path

import pytest

from wvh_texts_mcp import config
from wvh_texts_mcp.decode import decode_attributed_body, message_text

from conftest import blob_with


def test_install_name_decides_the_settings_folder(isolated, monkeypatch):
    monkeypatch.setenv("WVH_TEXTS_INSTALL", "vale-texts")
    assert config.home_dir() == isolated / "Library" / "Application Support" / "vale-texts"
    assert config.settings_path().name == "people.yaml"
    assert config.install_name("robin-texts") == "robin-texts"


def test_default_install_name(monkeypatch):
    monkeypatch.delenv("WVH_TEXTS_INSTALL")
    assert config.install_name() == "wvh-texts"


@pytest.mark.parametrize("bad", ["Robin Texts", "robin_texts", "-x", "../x", "ROBIN"])
def test_install_names_are_plain(monkeypatch, bad):
    monkeypatch.setenv("WVH_TEXTS_INSTALL", bad)
    with pytest.raises(config.ConfigError):
        config.install_name()


def test_home_can_be_moved(monkeypatch, tmp_path):
    monkeypatch.setenv("WVH_TEXTS_HOME", str(tmp_path / "elsewhere"))
    assert config.home_dir() == tmp_path / "elsewhere"


def test_no_settings_file_is_an_empty_allow_list_and_creates_nothing(isolated):
    cfg = config.load_config()
    assert cfg.empty and cfg.owner_name == "the owner" and cfg.allow == () and cfg.groups == ()
    assert not (isolated / "Library").exists()


def test_settings_are_read_and_numbers_normalized(allowed):
    cfg = config.load_config()
    assert cfg.owner_name == "Robin" and cfg.timezone == "America/Chicago"
    dana = next(p for p in cfg.allow if p.name == "Dana Northwind")
    assert dana.handles == {"3125550101", "dana@example.com"}
    assert cfg.groups[0].chat == "chat600000000000000001"
    assert cfg.nicknames == {"dee": "Dana Northwind"}


@pytest.mark.parametrize(
    "text",
    ["allow: [{handles: ['3125550101']}]", "groups: [{name: Harbor}]", "- just a list", "a: [b", "scope: everyone", "opted_out: Sam"],
)
def test_broken_settings_are_refused(world, text):
    world.settings.parent.mkdir(parents=True, exist_ok=True)
    world.settings.write_text(text)
    with pytest.raises(config.ConfigError):
        config.load_config()


def test_owner_from_environment_wins(allowed, monkeypatch):
    monkeypatch.setenv("WVH_TEXTS_OWNER", "Robin Vale")
    assert config.load_config().owner_name == "Robin Vale"


@pytest.mark.parametrize(
    "raw, expected",
    [("+1 (312) 555-0101", "3125550101"), ("312.555.0101", "3125550101"), ("Dana@Example.com", "dana@example.com"), ("12345", "12345")],
)
def test_normalize_handle(raw, expected):
    assert config.normalize_handle(raw) == expected


def test_mask_hides_most_of_a_number():
    assert config.mask("3125550101") == "…0101"
    assert config.mask("dana@example.com") == "d…@example.com"


def test_decoder_reads_the_hidden_body():
    assert decode_attributed_body(blob_with("The hidden body")) == "The hidden body"
    assert message_text(None, blob_with("x")) == "x"
    assert decode_attributed_body(b"garbage") == ""


def test_switches_default_on_and_all(isolated):
    cfg = config.load_config()
    assert cfg.sending and cfg.can_send and cfg.scope == "all" and not cfg.clients_only


@pytest.mark.parametrize("value, expected", [("off", False), (False, False), ("no", False), ("on", True), (True, True)])
def test_sending_switch(world, value, expected):
    world.write_settings({"sending": value})
    assert config.load_config().sending is expected


def test_clients_scope_turns_sending_off(world):
    world.write_settings({"scope": "clients"})
    cfg = config.load_config()
    assert cfg.clients_only and not cfg.can_send


def test_name_only_entries_holds_opt_outs_and_owner_numbers(world):
    world.write_settings({
        "allow": [{"name": "Dana Northwind", "client": "NB"}],
        "sweep_hold": ["Dana Northwind"],
        "opted_out": ["Sam Hollis", "+1 312 555 0199"],
        "owner_numbers": ["312.555.0100"],
    })
    cfg = config.load_config()
    assert cfg.allow[0].handles == frozenset() and cfg.allow[0].client == "NB"
    assert cfg.sweep_hold == {"dana northwind"} and cfg.opted_out == {"sam hollis", "3125550199"}
    assert cfg.owner_handles == {"3125550100"}


def test_python_app_is_the_venvs_base_python():
    import sys

    assert config.python_app() == Path(sys.base_prefix) / "Resources" / "Python.app"
