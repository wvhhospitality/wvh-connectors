"""The setup command: the only thing that changes the allow-list."""

import yaml

from wvh_texts_mcp import setup_cli
from wvh_texts_mcp.config import load_config

from conftest import ALLOWED


def run(*argv):
    return setup_cli.main(["--install", "robin-texts", *argv])


def _settings(world):
    return yaml.safe_load(world.settings.read_text())


def _backups(world):
    return sorted(world.settings.parent.glob("people.yaml.bak-*"))


def test_status_on_a_brand_new_install_creates_nothing(world, capsys):
    assert run("status") == 0
    out = capsys.readouterr().out
    assert "not created yet" in out and "0 people, 0 group chats" in out
    assert "Messages:         ok" in out and "Contacts:         ok" in out
    assert "Nothing has been created on this Mac." in out
    assert not (world.home / "Library").exists()


def test_owner_and_time_zone(world, capsys):
    assert run("owner", "Robin", "--timezone", "America/Chicago") == 0
    cfg = load_config()
    assert cfg.owner_name == "Robin" and cfg.timezone == "America/Chicago"
    assert oct(world.settings.stat().st_mode & 0o777) == "0o600"
    assert oct(world.settings.parent.stat().st_mode & 0o777) == "0o700"
    assert run("owner", "Robin", "--timezone", "Mars/Olympus") == 2


def test_allow_by_name_adds_every_number_on_their_card(world, capsys):
    assert run("allow", "Dana Northwind") == 0
    entry = _settings(world)["allow"][0]
    assert entry == {"name": "Dana Northwind", "handles": ["3125550101", "dana@example.com"]}
    out = capsys.readouterr().out
    assert "Group chats they are in stay closed" in out and "3125550101" not in out


def test_allow_only_one_number(world, capsys):
    assert run("allow", "Dana Northwind", "--only", "+1 312 555 0101") == 0
    assert _settings(world)["allow"][0]["handles"] == ["3125550101"]
    assert run("allow", "Nico Reyes", "--only", "312-555-0199") == 1  # not on Nico's card


def test_allow_an_ambiguous_name_adds_nothing(world, capsys):
    assert run("allow", "Nico") == 1
    out = capsys.readouterr().out
    assert "Nico Alvarez" in out and "Nico Reyes" in out and "Nothing was added" in out
    assert not world.settings.exists()


def test_allow_someone_not_in_contacts(world, capsys):
    assert run("allow", "Zed Nobody") == 1
    assert "No one called 'Zed Nobody' is in this Mac's Contacts" in capsys.readouterr().out
    assert run("allow", "+1 312 555 0199", "--name", "Front desk") == 0
    assert _settings(world)["allow"][0] == {"name": "Front desk", "handles": ["3125550199"]}


def test_allow_keeps_other_entries_and_backs_up(allowed, capsys):
    assert run("allow", "Sam Hollis") == 0
    data = _settings(allowed)
    assert [e["name"] for e in data["allow"]] == ["Dana Northwind", "Nico Reyes", "Sam Hollis"]
    assert data["groups"] == ALLOWED["groups"] and data["owner_name"] == "Robin"
    backups = _backups(allowed)
    assert len(backups) == 1 and "Sam Hollis" not in backups[0].read_text()


def test_allow_twice_merges(allowed):
    run("allow", "Dana Northwind", "--only", "dana@example.com")
    entries = [e for e in _settings(allowed)["allow"] if e["name"] == "Dana Northwind"]
    assert len(entries) == 1 and entries[0]["handles"] == ["3125550101", "dana@example.com"]


def test_groups_lists_chats_for_a_person(world, capsys):
    assert run("groups", "Dana Northwind") == 0
    out = capsys.readouterr().out
    assert "Harbor crew" in out and "Family" in out and "chat600000000000000001" in out


def test_allow_group_by_name_and_by_id(world, capsys):
    assert run("allow-group", "Harbor crew") == 0
    assert _settings(world)["groups"] == [{"name": "Harbor crew", "chat": "chat600000000000000001"}]
    assert run("allow-group", "--id", "chat700000000000000002", "--name", "Home") == 0
    assert _settings(world)["groups"][1] == {"name": "Home", "chat": "chat700000000000000002"}
    assert run("allow-group", "No Such Group") == 1
    assert run("allow-group", "--id", "chat999") == 1


def test_remove_a_person_a_number_and_a_group(allowed, capsys):
    assert run("remove", "+1 312 555 0101") == 0
    dana = next(e for e in _settings(allowed)["allow"] if e["name"] == "Dana Northwind")
    assert dana["handles"] == ["dana@example.com"]
    assert run("remove", "Nico Reyes") == 0
    assert run("remove", "Harbor crew") == 0
    data = _settings(allowed)
    assert [e["name"] for e in data["allow"]] == ["Dana Northwind"] and data["groups"] == []
    assert run("remove", "Sam Hollis") == 1


def test_nicknames(allowed, capsys):
    assert run("nickname", "Nic", "Nico Reyes") == 0
    assert load_config().nicknames["nic"] == "Nico Reyes"
    assert "never adds anyone to the allow-list" in capsys.readouterr().out
    assert run("nickname", "dee", "--remove") == 0
    assert "dee" not in load_config().nicknames
    assert run("nickname", "zzz", "--remove") == 1


def test_a_nickname_never_opens_anyone(allowed):
    from wvh_texts_mcp.tools import call

    run("nickname", "Pal", "Sam Hollis")
    assert call("read_texts", {"person": "Pal"})["status"] == "not_on_allow_list"


def test_list(allowed, capsys):
    assert run("list") == 0
    out = capsys.readouterr().out
    assert "person  Dana Northwind" in out and "group   Harbor crew" in out and "nickname  dee = Dana Northwind" in out
    assert "3125550101" not in out


def test_no_contacts_access_is_said_plainly(world, monkeypatch, tmp_path, capsys):
    blocked = tmp_path / "blocked-contacts"
    blocked.mkdir()
    blocked.chmod(0o000)
    monkeypatch.setenv("WVH_TEXTS_CONTACTS", str(blocked))
    try:
        assert run("allow", "Dana Northwind") == 1
        out = capsys.readouterr().out
        assert run("status") == 0
        status = capsys.readouterr().out
    finally:
        blocked.chmod(0o755)
    assert "Full Disk Access" in out
    assert "Contacts:         blocked" in status and "Privacy & Security" in status


def test_a_broken_settings_file_is_left_alone(world, capsys):
    world.settings.parent.mkdir(parents=True)
    world.settings.write_text("allow: [{name: Dana}]")
    assert run("allow", "Nico Reyes") == 2
    assert world.settings.read_text() == "allow: [{name: Dana}]"
