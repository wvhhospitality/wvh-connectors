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
    assert "Group chats they're in aren't swept" in out and "3125550101" not in out


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
    assert "never puts anyone on the list" in capsys.readouterr().out
    assert run("nickname", "dee", "--remove") == 0
    assert "dee" not in load_config().nicknames
    assert run("nickname", "zzz", "--remove") == 1


def test_a_nickname_never_puts_anyone_on_the_list(allowed):
    from wvh_texts_mcp.tools import call

    run("nickname", "Pal", "Sam Hollis")
    assert call("read_texts", {"person": "Pal"})["person"] == "Sam Hollis"  # named, so readable
    assert "Sam Hollis" not in call("allow_list")["people"]
    assert "Sam Hollis" not in {t.get("person") for t in call("whats_new", {"hours": 48})["threads"]}


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
    world.settings.write_text("allow: [{handles: [3125550101]}]")
    assert run("allow", "Nico Reyes") == 2
    assert world.settings.read_text() == "allow: [{handles: [3125550101]}]"


# ─── 0.2.0: switches, holds, opt-outs, "me", and the importer ───────────────


def test_sending_and_scope_switches(allowed, capsys):
    assert run("sending", "off") == 0 and load_config().can_send is False
    assert run("sending", "on") == 0 and load_config().can_send is True
    assert run("scope", "clients") == 0 and load_config().clients_only and not load_config().can_send
    assert run("scope", "all") == 0 and load_config().scope == "all"


def test_sweep_hold_and_opt_out(allowed, capsys):
    assert run("sweep-hold", "Nico Reyes") == 0 and "nico reyes" in load_config().sweep_hold
    assert run("sweep-hold", "Nico Reyes", "--remove") == 0 and not load_config().sweep_hold
    assert run("opt-out", "Sam Hollis") == 0 and "sam hollis" in load_config().opted_out
    assert run("opt-out", "Sam Hollis", "--remove") == 0 and not load_config().opted_out
    assert run("opt-out", "Sam Hollis", "--remove") == 1


def test_owner_number_for_text_me(world, capsys):
    assert run("owner", "Robin", "--number", "+1 (312) 555-0100") == 0
    assert load_config().owner_handles == {"3125550100"}
    assert "…0100" in capsys.readouterr().out
    assert run("owner", "Robin", "--number", "not a number") == 2


def test_status_names_the_python_for_full_disk_access(world, capsys):
    from wvh_texts_mcp.config import python_app

    assert run("status") == 0
    out = capsys.readouterr().out
    assert str(python_app()) in out and "Sending:          on" in out and "Scope:            all" in out


def test_import_an_older_settings_file(world, tmp_path, capsys):
    old = tmp_path / "old-people.yaml"
    old.write_text(yaml.safe_dump({
        "approved": True,
        "nicknames": {"Dee": "Dana Northwind", "Captain": "Nico Reyes", "Bad": "3125550101"},
        "opted_out": ["Sam Hollis"],
        "allow_list": [{"name": "Dana Northwind", "client": "NB"}, {"name": "Nico Reyes", "client": "JNV"}],
        "sweep_hold": ["Nico Reyes"],
    }))
    staged = tmp_path / "staged" / "people.yaml"
    nicks = tmp_path / "staged" / "nicknames.yaml"
    assert run("import", str(old), "--to", str(staged), "--nicknames-to", str(nicks), "--owner", "Robin") == 0
    out = capsys.readouterr().out
    assert "2 people added" in out and "2 saved" in out and "1 skipped" in out
    assert "Dana" not in out and "Nico" not in out, "counts only, never names"
    data = yaml.safe_load(staged.read_text())
    assert data["allow"] == [{"name": "Dana Northwind", "client": "NB"}, {"name": "Nico Reyes", "client": "JNV"}]
    assert data["sweep_hold"] == ["Nico Reyes"] and data["opted_out"] == ["Sam Hollis"]
    assert data["sending"] is True and data["scope"] == "all" and data["owner_name"] == "Robin"
    assert yaml.safe_load(nicks.read_text())["nicknames"] == {"Dee": "Dana Northwind", "Captain": "Nico Reyes"}
    assert not (world.home / "Library" / "Application Support" / "robin-texts").exists(), "the live folder is untouched"
    from wvh_texts_mcp import nicknames

    assert nicknames.load() == {}, "the shared file is untouched when --nicknames-to is given"


def test_import_keeps_sweeps_closed_when_the_old_list_wasnt_approved(world, tmp_path, capsys):
    old = tmp_path / "old.yaml"
    old.write_text(yaml.safe_dump({"approved": False, "allow_list": [{"name": "Dana Northwind", "client": "NB"}]}))
    staged = tmp_path / "s" / "people.yaml"
    assert run("import", str(old), "--to", str(staged), "--nicknames-to", str(tmp_path / "s" / "n.yaml")) == 0
    assert yaml.safe_load(staged.read_text())["sweep_hold"] == ["Dana Northwind"]
