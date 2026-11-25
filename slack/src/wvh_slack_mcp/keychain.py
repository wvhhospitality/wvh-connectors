"""Tokens in the macOS Keychain, through Apple's own /usr/bin/security tool.

Not a Python library, on purpose. On an always-on Mac nobody sits at, a keyring read
waits forever on an approval dialog nobody is there to click, which freezes the server.
Every item here trusts /usr/bin/security, so reads return at once, and they carry
a timeout in case one ever doesn't.

Writes go over standard input, so a token never appears on a command line.
"""

from __future__ import annotations

import re
import subprocess

SECURITY = "/usr/bin/security"
TIMEOUT_SECONDS = 15
_SAFE = re.compile(r"[A-Za-z0-9._-]+")


def read(service: str, account: str) -> str | None:
    try:
        result = subprocess.run(
            [SECURITY, "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def delete(service: str, account: str) -> None:
    try:
        subprocess.run(
            [SECURITY, "delete-generic-password", "-s", service, "-a", account],
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def store(service: str, account: str, secret: str) -> None:
    """Replace the item. Callers must read it back: `security -i` always exits 0."""
    for value in (service, account, secret):
        if not _SAFE.fullmatch(value):
            raise ValueError("Keychain values must be plain tokens.")
    delete(service, account)
    command = f'add-generic-password -s "{service}" -a "{account}" -T {SECURITY} -w "{secret}"\n'
    subprocess.run(
        [SECURITY, "-i"], input=command, capture_output=True, text=True, timeout=TIMEOUT_SECONDS
    )
