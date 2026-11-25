"""Turn a text message picture into JPEG bytes, with the sips tool built into macOS."""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

from .config import attachments_root

log = logging.getLogger("wvh_texts_mcp")

SIPS = "/usr/bin/sips"
MAX_EDGE = 1600
QUALITY = 80


class PictureError(Exception):
    """The picture can't be shown. The message is safe to pass to Claude."""


def locate(filename: str | None, root: Path | None = None) -> Path:
    """Find the file on disk, and refuse anything outside the Messages attachments folder."""
    if not filename:
        raise PictureError("This picture has no file on this Mac.")
    root = (root or attachments_root()).expanduser().resolve()
    path = Path(filename).expanduser().resolve()
    if not path.is_relative_to(root):
        raise PictureError("This picture is stored outside the Messages attachments folder, so it wasn't opened.")
    if not path.is_file():
        raise PictureError("This picture isn't on this Mac. It may be held only in iCloud.")
    return path


def to_jpeg(path: Path) -> bytes:
    with tempfile.TemporaryDirectory(prefix="wvh-texts-") as folder:
        out = Path(folder) / "picture.jpg"
        # sips talks on stdout, which belongs to the connection with Claude, so capture it.
        result = subprocess.run(
            [SIPS, "-s", "format", "jpeg", "-s", "formatOptions", str(QUALITY), "-Z", str(MAX_EDGE), str(path), "--out", str(out)],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0 or not out.exists():
            log.error("sips failed with exit code %s", result.returncode)
            raise PictureError("This picture couldn't be converted.")
        return out.read_bytes()
