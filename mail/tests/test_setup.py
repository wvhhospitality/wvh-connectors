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


# ─── Identities, switches, and the older format ──────────────────────────────

from wvh_mail_mcp import config as _config  # noqa: E402


def test_identity_add_copies_the_signature_in(home, tmp_path):
    write_settings("owner_name: Robin\naccounts:\n  - email: robin@example.com\n    label: Studio\n")
    src = tmp_path / "robin sig.html"
    src.write_text("<p>Robin Vale</p>")
    src.with_suffix(".txt").write_text("Robin Vale")
    assert setup_cli.main(["--install", "robin-mail", "identity", "add", "Robin", "--signature", str(src), "--default"]) == 0
    assert setup_cli.main(["--install", "robin-mail", "identity", "add", "Helper", "--drafts-only", "--account", "Studio"]) == 0
    cfg = _config.load_config()
    assert cfg.default_identity == "Robin" and cfg.identity("Helper").drafts_only
    assert cfg.accounts[0].identity == "Helper"
    assert (settings_folder() / "signatures" / "robin sig.html").read_text() == "<p>Robin Vale</p>"
    assert (settings_folder() / "signatures" / "robin sig.txt").exists()


def test_identity_per_domain_signature(home, tmp_path):
    write_settings("accounts:\n  - email: robin@example.com\n  - email: robin@example.net\n")
    a, b = tmp_path / "a.html", tmp_path / "b.html"
    a.write_text("A")
    b.write_text("B")
    setup_cli.main(["--install", "robin-mail", "identity", "add", "Robin", "--signature", str(a), "--domain", f"example.net={b}"])
    cfg = _config.load_config()
    ident = cfg.identity("Robin")
    assert ident.signature_for(cfg.accounts[0]) == "a.html" and ident.signature_for(cfg.accounts[1]) == "b.html"


@pytest.mark.parametrize("which", ["sending", "drafts"])
def test_switches_go_off_and_on(home, which):
    write_settings("accounts:\n  - email: robin@example.com\n")
    setup_cli.main(["--install", "robin-mail", which, "off"])
    assert getattr(_config.load_config(), which) is False
    setup_cli.main(["--install", "robin-mail", which, "on"])
    assert getattr(_config.load_config(), which) is True


OLDER = """\
imap: {host: imap.gmail.com, port: 993, ssl: true}
defaults:
  auth_mode: app_password
  send: {max_per_window: 5}
accounts:
  - client_code: NB
    email: Ops@Example.com
    secret_ref: OldMail/ops@example.com
    signature: robin-nb.html
  - client_code: KL
    email: hello@example.net
    secret_ref: OldMail/hello@example.net
"""


def test_import_brings_in_mailboxes_without_moving_a_password(home, tmp_path, isolated):
    old = tmp_path / "accounts.yaml"
    old.write_text(OLDER)
    sigs = tmp_path / "signatures"
    sigs.mkdir()
    (sigs / "robin-nb.html").write_text("<p>Robin</p>")
    isolated.set_password("OldMail", "ops@example.com", "already-there")
    assert setup_cli.main(["--install", "robin-mail", "import", str(old), "--signatures", str(sigs)]) == 0
    cfg = _config.load_config()
    assert cfg.keychain_service == "OldMail"
    assert [(a.email, a.label, a.signature) for a in cfg.accounts] == [
        ("ops@example.com", "NB", "robin-nb.html"), ("hello@example.net", "KL", None)]
    assert cfg.send_limits.max_per_window == 5
    assert _config.get_password(cfg.accounts[0]) == "already-there"
    assert (settings_folder() / "signatures" / "robin-nb.html").exists()
    assert isolated.items == {("OldMail", "ops@example.com"): "already-there"}, "no password was copied or moved"


def test_import_never_overwrites_mailboxes_already_set_up(home, tmp_path):
    write_settings("accounts:\n  - email: robin@example.com\n")
    old = tmp_path / "accounts.yaml"
    old.write_text(OLDER)
    assert setup_cli.main(["--install", "robin-mail", "import", str(old)]) == 1
    assert [a.email for a in _config.load_config().accounts] == ["robin@example.com"]


def test_the_older_format_is_named_plainly_if_used_directly(home):
    write_settings(OLDER)
    with pytest.raises(_config.ConfigError, match="older WvH Mail format. Convert it once"):
        _config.load_config()
