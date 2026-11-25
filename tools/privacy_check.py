#!/usr/bin/env python3
"""Stops private details from being committed to this public repository.

It reads the real settings on this Mac (which never live in the repository),
collects every name, address, phone number and workspace id in them, and fails
if any of those, or anything shaped like a password or key, appears in a file
that git would publish.

    python3 tools/privacy_check.py            check everything git would publish
    python3 tools/privacy_check.py --install  run it automatically before every commit and push

Where it looks for your private words:
  * each connector's settings folder (~/Library/Application Support/wvh-slack, -mail, -texts)
  * an optional list of your own: ~/Library/Application Support/wvh-connectors/privacy.yaml
        settings_folders: [~/some/older/config/folder]   # more places to read
        extra_words: [A Client Name, a-person@example.com]
        allow: [Words That Are Fine To Publish]

It needs nothing installed beyond Python. With PyYAML present it reads settings
files exactly; without it, it falls back to pulling the words out by pattern.
It prints where a problem is and what kind, never a password or a phone number.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUPPORT = Path.home() / "Library" / "Application Support"
OWN_LIST = SUPPORT / "wvh-connectors" / "privacy.yaml"
SETTINGS_FOLDERS = [SUPPORT / name for name in ("wvh-slack", "wvh-mail", "wvh-texts")]

# Addresses and numbers that exist only as examples.
SAFE_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "users.noreply.github.com", "anthropic.com"}
SAFE_HOME_NAMES = {"robin", "you", "yourname", "me", "user", "username", "shared"}
COMMON_WORDS = {"general", "random", "team", "ops", "all", "the", "and", "llc", "inc", "true", "false", "none"}

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
PHONE = re.compile(r"(?<![\d.])(?:\+?1[\s.-]?)?\(?[2-9]\d{2}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\d.])")
SLACK_ID = re.compile(r"(?<![A-Za-z0-9_])[TUCWDG](?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{8,11}(?![A-Za-z0-9_])")
SHAPES = [
    ("a Slack token", re.compile(r"xox[abeoprs]-(?=[0-9A-Za-z-]*\d)[0-9A-Za-z-]{20,}")),
    ("an API key", re.compile(r"sk-(?:ant-)?[0-9A-Za-z_-]{24,}|\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{30,}")),
    ("an access token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{30,}\b|\bglpat-[0-9A-Za-z_-]{16,}")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("a password in plain text", re.compile(r"(?i)\b(?:password|passwd|app_password|secret)\b\s*[:=]\s*[\"']?[^\s\"'<{$][^\s\"']{7,}")),
    ("a private network address", re.compile(r"\b[a-z0-9-]+\.tail[0-9a-f]{4,}\.ts\.net\b|\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b|\b192\.168\.\d{1,3}\.\d{1,3}\b")),
]
HOME_PATH = re.compile(r"/Users/([A-Za-z0-9._-]+)")


# ─── Gathering the private words ──────────────────────────────────────────


def _strings(node) -> list[str]:
    if isinstance(node, dict):
        # Values are the private part. A key counts only when it is itself an address or an id.
        keys = [k for k in node if isinstance(k, str) and (EMAIL.fullmatch(k) or SLACK_ID.fullmatch(k))]
        return keys + [s for value in node.values() for s in _strings(value)]
    if isinstance(node, (list, tuple)):
        return [s for item in node for s in _strings(item)]
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        return [str(node)]
    return [node] if isinstance(node, str) else []


def _load(path: Path) -> list[str]:
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return []
    if path.suffix == ".json":
        try:
            return _strings(json.loads(text))
        except ValueError:
            return []
    try:
        import yaml  # optional

        return _strings(yaml.safe_load(text))
    except Exception:
        # No PyYAML, or a file it can't read: take quoted text and the value after each colon.
        found = re.findall(r"[\"']([^\"'\n]{2,})[\"']", text)
        found += [m.strip() for m in re.findall(r":[ \t]+([^#\n\[\]{}]+)", text)]
        return found + EMAIL.findall(text) + SLACK_ID.findall(text)


def _own_list() -> dict:
    if not OWN_LIST.exists():
        return {}
    # Fail closed: without this list the check would quietly miss the extra words in it.
    try:
        import yaml
    except ImportError:
        raise SystemExit(
            f"STOP: {OWN_LIST} needs PyYAML to read, and this Python has none.\n"
            "Run the check with a connector's own Python instead, for example:\n"
            "    slack/.venv/bin/python tools/privacy_check.py"
        )
    try:
        data = yaml.safe_load(OWN_LIST.read_text()) or {}
    except Exception as exc:
        raise SystemExit(f"STOP: could not read {OWN_LIST} ({exc}). Fix it, then try again.")
    if not isinstance(data, dict):
        raise SystemExit(f"STOP: {OWN_LIST} should be a list of settings_folders, extra_words and allow.")
    return data


def private_words() -> tuple[dict[str, set[str]], set[str]]:
    own = _own_list()
    folders = SETTINGS_FOLDERS + [Path(str(p)).expanduser() for p in own.get("settings_folders") or []]
    words: dict[str, set[str]] = {"email": set(), "domain": set(), "phone": set(), "id": set(), "name": set(), "code": set()}

    raw: list[str] = [str(w) for w in own.get("extra_words") or []]
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if path.suffix in (".yaml", ".yml", ".json") and path.is_file():
                raw += _load(path)

    for item in raw:
        item = item.strip()
        if not item:
            continue
        for address in EMAIL.findall(item):
            words["email"].add(address.lower())
            domain = address.split("@", 1)[1].lower()
            if domain not in SAFE_EMAIL_DOMAINS and not domain.endswith(("gmail.com", "icloud.com", "me.com", "yahoo.com", "outlook.com")):
                words["domain"].add(domain)
        digits = re.sub(r"\D", "", item)
        if PHONE.fullmatch(item) or (re.fullmatch(r"[+\d\s().-]+", item) and len(digits) in (10, 11)):
            words["phone"].add(digits[-10:])
        elif SLACK_ID.fullmatch(item):
            words["id"].add(item)
        elif re.fullmatch(r"[A-Z][A-Z0-9]{1,5}", item):
            words["code"].add(item)
        elif "@" not in item and not item.startswith(("/", "~", "http")) and 4 <= len(item) <= 60:
            if re.search(r"[A-Za-z]{3}", item) and item.lower() not in COMMON_WORDS and len(item.split()) <= 6:
                # A name, a label, a nickname, or a workspace address.
                if " " in item or item[:1].isupper() or re.fullmatch(r"[a-z0-9-]{5,}", item):
                    words["name"].add(item)

    allow = {str(a).strip().lower() for a in own.get("allow") or []}
    for kind in words:
        words[kind] = {w for w in words[kind] if w.lower() not in allow}
    return words, allow


# ─── Checking what git would publish ──────────────────────────────────────


def published_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-co", "--exclude-standard", "-z"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [REPO / name for name in out.split("\0") if name]


def _mask(value: str) -> str:
    return value[:2] + "…" + value[-2:] if len(value) > 6 else "…"


def check(paths: list[Path]) -> list[str]:
    words, allow = private_words()
    by_kind = [
        ("a real mailbox address", words["email"], re.I, False),
        ("a real mail domain", words["domain"], re.I, False),
        ("a real workspace or account id", words["id"], 0, False),
        ("a real name or label from your settings", words["name"], re.I, False),
        ("a real workspace code from your settings", words["code"], 0, False),
    ]
    patterns = []
    for label, values, flags, _ in by_kind:
        for value in sorted(values, key=len, reverse=True):
            patterns.append((label, value, re.compile(rf"(?<![A-Za-z0-9_]){re.escape(value)}(?![A-Za-z0-9_])", flags)))

    problems: list[str] = []
    this_file = Path(__file__).resolve()
    for path in paths:
        if path.resolve() == this_file:
            continue  # its own patterns look like the things it hunts
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable: nothing here reads those
        rel = path.relative_to(REPO)
        for number, line in enumerate(text.splitlines(), 1):
            where = f"{rel}:{number}"
            for label, value, pattern in patterns:
                if pattern.search(line):
                    problems.append(f"{where}  {label}: {value}")
            digits_in_line = re.sub(r"\D", "", line)
            if any(phone in digits_in_line for phone in words["phone"]):
                problems.append(f"{where}  a real phone number from your settings")
            for label, pattern in SHAPES:
                hit = pattern.search(line)
                if hit and hit.group(0).lower() not in allow:
                    problems.append(f"{where}  looks like {label}: {_mask(hit.group(0))}")
            for address in EMAIL.findall(line):
                domain = address.split("@", 1)[1].lower()
                if domain not in SAFE_EMAIL_DOMAINS and address.lower() not in allow and address.lower() not in words["email"]:
                    problems.append(f"{where}  an email address that isn't an example: {address}")
            for name in HOME_PATH.findall(line):
                if name.lower() not in SAFE_HOME_NAMES and f"/users/{name.lower()}" not in allow:
                    problems.append(f"{where}  a real home folder path: /Users/{name}")
            for number in PHONE.findall(line):
                digits = re.sub(r"\D", "", number)[-10:]
                written_as_phone = re.search(r"[\s().+-]", number.strip()) is not None
                if written_as_phone and digits[3:6] != "555" and number.strip().lower() not in allow:
                    problems.append(f"{where}  a phone number that isn't a 555 example")
    return problems


HOOK = """#!/bin/sh
# Installed by tools/privacy_check.py --install. Blocks a commit or push that carries private details.
top="$(git rev-parse --show-toplevel)"
for py in "$top/slack/.venv/bin/python" "$top/mail/.venv/bin/python" "$top/texts/.venv/bin/python"; do
  [ -x "$py" ] && exec "$py" "$top/tools/privacy_check.py"
done
exec python3 "$top/tools/privacy_check.py"
"""


def install() -> int:
    hooks = REPO / ".git" / "hooks"
    if not hooks.is_dir():
        print("This folder isn't a git repository yet.")
        return 1
    for name in ("pre-commit", "pre-push"):
        target = hooks / name
        target.write_text(HOOK)
        target.chmod(0o755)
    print("Installed. The check now runs before every commit and every push from this Mac.")
    return 0


def main() -> int:
    if sys.argv[1:] == ["--install"]:
        return install()
    words, _ = private_words()
    known = sum(len(v) for v in words.values())
    paths = published_files()
    problems = check(paths)
    if problems:
        print(f"STOP: {len(problems)} private detail(s) in files git would publish:\n")
        for line in problems:
            print("  " + line)
        print("\nNothing was committed. Replace each with a made-up example, then try again.")
        return 1
    print(f"Clear: {len(paths)} files checked against {known} private words from this Mac's settings.")
    if not known:
        print("note: no settings were found on this Mac, so only passwords, keys and addresses were checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
