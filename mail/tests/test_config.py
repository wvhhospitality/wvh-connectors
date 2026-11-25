"""Settings: the install name, where settings live, and reading the mailbox list."""

from pathlib import Path

import pytest

from wvh_mail_mcp import config
from wvh_mail_mcp.config import ConfigError, load_config

from conftest import TWO_MAILBOXES, settings_folder, write_settings

PROJECT = Path(__file__).resolve().parents[1]


def test_install_name_decides_folder_and_keychain_service(monkeypatch):
    monkeypatch.setenv("WVH_MAIL_INSTALL", "robin-mail")
    assert config.home_dir() == Path.home() / "Library/Application Support/robin-mail"
    assert config.settings_path() == Path.home() / "Library/Application Support/robin-mail/accounts.yaml"
    assert config.keychain_service() == "robin-mail"
    assert config.keychain_service("kite-loop-mail") == "kite-loop-mail"
    assert config.home_dir("kite-loop-mail").name == "kite-loop-mail"


def test_default_install_name(monkeypatch):
    monkeypatch.delenv("WVH_MAIL_INSTALL", raising=False)
    assert config.install_name() == "wvh-mail"
    assert config.home_dir().name == "wvh-mail"


@pytest.mark.parametrize("bad", ["Robin Mail", "robin_mail", "../robin", "-robin", "", "robin/mail", "ROBIN"])
def test_bad_install_names_are_refused(bad):
    with pytest.raises(ConfigError):
        config.install_name(bad or " ")


def test_settings_live_outside_the_app():
    assert PROJECT not in config.settings_path().parents, "settings must never sit beside the code"


def test_wvh_mail_home_moves_the_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("WVH_MAIL_HOME", str(tmp_path / "elsewhere"))
    assert config.settings_path() == tmp_path / "elsewhere" / "accounts.yaml"


def test_a_new_install_has_no_mailboxes_and_writes_nothing():
    cfg = load_config()
    assert cfg.accounts == ()
    assert cfg.owner_name == "the owner"
    assert cfg.install == "robin-mail"
    assert not settings_folder().exists()


def test_settings_parse():
    write_settings(TWO_MAILBOXES)
    cfg = load_config()
    assert cfg.owner_name == "Robin"
    assert cfg.timezone == "America/Chicago" and cfg.zone() is not None
    studio, personal = cfg.accounts
    assert studio.email == "robin@example.com" and studio.label == "Studio"
    assert studio.imap_host == "imap.gmail.com" and studio.imap_port == 993 and studio.ssl
    assert personal.email == "robin.personal@example.net", "addresses are kept in lowercase"


def test_find_by_address_or_label_ignoring_case():
    write_settings(TWO_MAILBOXES)
    cfg = load_config()
    assert cfg.find("STUDIO").email == "robin@example.com"
    assert cfg.find("robin.personal@EXAMPLE.net").label == "Personal"
    with pytest.raises(ConfigError, match="Studio, Personal"):
        cfg.find("Kiteloop")


def test_other_mail_servers_can_be_named():
    write_settings("accounts:\n  - email: robin@example.org\n    imap_host: imap.example.org\n    imap_port: 1993\n")
    (acct,) = load_config().accounts
    assert (acct.imap_host, acct.imap_port) == ("imap.example.org", 1993)


def test_duplicate_mailboxes_are_refused():
    write_settings("accounts:\n  - email: robin@example.com\n  - email: ROBIN@example.com\n")
    with pytest.raises(ConfigError, match="both called"):
        load_config()


def test_a_mailbox_without_an_address_is_refused():
    write_settings("accounts:\n  - label: Studio\n")
    with pytest.raises(ConfigError, match="no usable email"):
        load_config()


def test_example_settings_parse():
    path = PROJECT / "accounts.example.yaml"
    cfg = load_config(path)
    assert cfg.owner_name == "Robin"
    assert len(cfg.accounts) == 2
    assert all(a.email.endswith(("example.com", "example.net", "example.org")) for a in cfg.accounts)


def test_missing_login_says_how_to_fix_it():
    write_settings(TWO_MAILBOXES)
    acct = load_config().accounts[0]
    with pytest.raises(config.CredentialError, match="wvh-mail-setup --install robin-mail add robin@example.com"):
        config.get_password(acct)
    assert config.has_password(acct) is False


def test_logins_are_read_from_the_install_s_own_service(isolated):
    write_settings(TWO_MAILBOXES)
    acct = load_config().accounts[0]
    isolated.set_password("wvh-mail", "robin@example.com", "someone-elses")
    assert config.has_password(acct) is False, "another install's login must never be used"
    isolated.set_password("robin-mail", "robin@example.com", "abcdabcdabcdabcd")
    assert config.get_password(acct) == "abcdabcdabcdabcd"
