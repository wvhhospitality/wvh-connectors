"""Setup: adding and removing a mailbox without a password ever reaching a chat, a command or a file."""

import subprocess

import pytest
import yaml

from wvh_mail_mcp import imap_client as mail
from wvh_mail_mcp import setup_cli

from conftest import settings_folder, write_settings

SECRET = "abcd efgh ijkl mnop"  # how Google shows an app password
STORED = "abcdefghijklmnop"


class FakeDialog:
    """Stands in for the Mac window that asks for the password when there is no terminal."""

    def __init__(self, answer: str | None = SECRET) -> None:
        self.answer = answer
        self.calls: list[dict] = []

    def __call__(self, argv, input=None, **kwargs):
        self.calls.append({"argv": list(argv), "input": input, **kwargs})
        if self.answer is None:
            return subprocess.CompletedProcess(argv, 1, "", "execution error: User canceled. (-128)")
        return subprocess.CompletedProcess(argv, 0, self.answer + "\n", "")


@pytest.fixture
def no_terminal(monkeypatch):
    """As when Claude runs setup: stdin is not a terminal."""
    monkeypatch.setattr(setup_cli.sys.stdin, "isatty", lambda: False, raising=False)
    dialog = FakeDialog()
    monkeypatch.setattr(setup_cli.subprocess, "run", dialog)
    return dialog


@pytest.fixture
def server_says(monkeypatch):
    seen: list[str] = []

    def answer(status="ok"):
        def check(account, password):
            seen.append(password)
            return mail.AccountStatus(account.email, account.label, status, None if status == "ok" else "refused")

        monkeypatch.setattr(setup_cli.mail, "check_login", check)
        return seen

    return answer


def settings():
    return yaml.safe_load((settings_folder() / "accounts.yaml").read_text())


def every_file_text(home):
    return "".join(p.read_text(errors="ignore") for p in home.rglob("*") if p.is_file())


def test_add_without_a_terminal_asks_in_a_mac_window(isolated, home, no_terminal, server_says, capsys):
    seen = server_says("ok")
    assert setup_cli.main(["--install", "robin-mail", "add", "Robin@Example.com", "--label", "Studio"]) == 0

    (call,) = no_terminal.calls
    assert call["argv"] == ["/usr/bin/osascript", "-"], "the script goes in on stdin, not the command line"
    assert "with hidden answer" in call["input"] and "robin@example.com" in call["input"]
    assert seen == [STORED], "the server is asked with the spaces taken out"
    assert isolated.items == {("robin-mail", "robin@example.com"): STORED}
    assert settings()["accounts"] == [{"email": "robin@example.com", "label": "Studio"}]

    out = capsys.readouterr()
    assert STORED not in out.out + out.err and "abcd" not in out.out + out.err
    assert STORED not in every_file_text(home) and "abcd" not in every_file_text(home)


def test_a_refused_password_is_not_kept_and_nothing_is_written(isolated, home, no_terminal, server_says):
    server_says("auth_error")
    assert setup_cli.main(["--install", "robin-mail", "add", "robin@example.com"]) == 1
    assert isolated.items == {}
    assert not settings_folder().exists()


def test_a_cancelled_window_stores_nothing(isolated, no_terminal, server_says):
    no_terminal.answer = None
    seen = server_says("ok")
    assert setup_cli.main(["--install", "robin-mail", "add", "robin@example.com"]) == 1
    assert seen == [] and isolated.items == {}
    assert not settings_folder().exists()


def test_in_a_terminal_it_uses_a_hidden_prompt(isolated, monkeypatch, server_says):
    monkeypatch.setattr(setup_cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(setup_cli.getpass, "getpass", lambda prompt: SECRET)
    monkeypatch.setattr(setup_cli.subprocess, "run", lambda *a, **k: pytest.fail("no window when there is a terminal"))
    server_says("ok")
    assert setup_cli.main(["--install", "robin-mail", "add", "robin@example.com"]) == 0
    assert isolated.items[("robin-mail", "robin@example.com")] == STORED


def test_adding_keeps_everything_else_and_backs_up_first(isolated, no_terminal, server_says):
    server_says("ok")
    write_settings("owner_name: Robin\ntimezone: America/Chicago\nsignature_note: keep me\naccounts:\n  - email: robin@example.com\n    label: Studio\n")
    assert setup_cli.main(["--install", "robin-mail", "add", "robin.jones@example.net", "--label", "Personal"]) == 0
    data = settings()
    assert data["owner_name"] == "Robin" and data["timezone"] == "America/Chicago" and data["signature_note"] == "keep me"
    assert [a["email"] for a in data["accounts"]] == ["robin@example.com", "robin.jones@example.net"]
    backups = list(settings_folder().glob("accounts.yaml.bak-*"))
    assert len(backups) == 1 and "signature_note: keep me" in backups[0].read_text()
    assert (settings_folder().stat().st_mode & 0o777) == 0o700
    assert ((settings_folder() / "accounts.yaml").stat().st_mode & 0o777) == 0o600


def test_adding_an_existing_mailbox_updates_it_in_place(isolated, no_terminal, server_says):
    server_says("ok")
    write_settings("accounts:\n  - email: robin@example.com\n    label: Studio\n")
    assert setup_cli.main(["--install", "robin-mail", "add", "robin@example.com"]) == 0
    assert settings()["accounts"] == [{"email": "robin@example.com", "label": "Studio"}]


def test_two_mailboxes_cannot_share_a_label(isolated, no_terminal, server_says):
    server_says("ok")
    write_settings("accounts:\n  - email: robin@example.com\n    label: Studio\n")
    assert setup_cli.main(["--install", "robin-mail", "add", "robin.jones@example.net", "--label", "studio"]) == 2
    assert no_terminal.calls == [], "nothing is asked for when the answer would be refused anyway"


def test_remove_takes_the_mailbox_and_its_login_away(isolated, no_terminal, server_says):
    server_says("ok")
    setup_cli.main(["--install", "robin-mail", "add", "robin@example.com", "--label", "Studio"])
    setup_cli.main(["--install", "robin-mail", "add", "robin.jones@example.net"])
    assert setup_cli.main(["--install", "robin-mail", "remove", "ROBIN@example.com"]) == 0
    assert [a["email"] for a in settings()["accounts"]] == ["robin.jones@example.net"]
    assert ("robin-mail", "robin@example.com") not in isolated.items
    assert ("robin-mail", "robin.jones@example.net") in isolated.items
    assert setup_cli.main(["--install", "robin-mail", "remove", "nobody@example.com"]) == 1


def test_installs_never_share_logins(isolated, no_terminal, server_says):
    server_says("ok")
    setup_cli.main(["--install", "robin-mail", "add", "robin@example.com"])
    assert setup_cli.main(["--install", "kite-mail", "remove", "robin@example.com"]) == 1
    assert ("robin-mail", "robin@example.com") in isolated.items
    assert not settings_folder("kite-mail").exists()


def test_owner_and_time_zone(isolated):
    assert setup_cli.main(["--install", "robin-mail", "owner", "Robin", "--timezone", "America/Chicago"]) == 0
    assert settings() == {"owner_name": "Robin", "timezone": "America/Chicago"}
    assert setup_cli.main(["--install", "robin-mail", "owner", "Robin", "--timezone", "Mars/Olympus"]) == 2


def test_status_on_a_new_install_creates_nothing(home, capsys):
    assert setup_cli.main(["--install", "robin-mail", "status"]) == 0
    out = capsys.readouterr().out
    assert "No mailboxes yet. Nothing has been created" in out
    assert list(home.rglob("*")) == []


def test_status_shows_which_logins_are_stored(isolated, capsys):
    write_settings("owner_name: Robin\naccounts:\n  - email: robin@example.com\n    label: Studio\n  - email: robin.jones@example.net\n")
    isolated.set_password("robin-mail", "robin@example.com", STORED)
    assert setup_cli.main(["--install", "robin-mail", "status", "--no-probe"]) == 0
    out = capsys.readouterr().out
    assert "robin@example.com (Studio)  login stored" in out
    assert "robin.jones@example.net  login MISSING" in out
    assert STORED not in out


def test_a_bad_install_name_is_refused(capsys):
    assert setup_cli.main(["--install", "Robin Mail", "status"]) == 2
    assert "lowercase letters" in capsys.readouterr().out


def test_the_window_script_escapes_the_address():
    assert setup_cli._applescript_text('a"b\\c') == '"a\\"b\\\\c"'
