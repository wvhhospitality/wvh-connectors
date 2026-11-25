"""Get the words out of a text message row.

Some messages leave `text` empty and keep the words only in `attributedBody`, an
NSArchiver "typedstream" blob. The string follows the NSString class marker and a
`+` type tag, with its length as a typedstream integer: one byte for short strings,
0x81 plus two bytes, or 0x82 plus four bytes.
"""

from __future__ import annotations

_MARKER = b"NSString"
_WINDOW = 16  # the type tag sits a few bytes past the marker
_ATTACHMENT_PLACEHOLDER = "￼"


def _clean(text: str) -> str:
    return text.replace(_ATTACHMENT_PLACEHOLDER, "").strip()


def decode_attributed_body(blob: bytes | None) -> str:
    """Return the text inside `blob`, or "" if there is none. Never raises."""
    if not blob:
        return ""
    marker = blob.find(_MARKER)
    if marker < 0:
        return ""
    after = marker + len(_MARKER)
    tag = blob.find(b"+", after, after + _WINDOW)
    if tag < 0 or tag + 1 >= len(blob):
        return ""

    pos = tag + 1
    first = blob[pos]
    if first < 0x80:
        length, pos = first, pos + 1
    elif first == 0x81:
        length, pos = int.from_bytes(blob[pos + 1 : pos + 3], "little"), pos + 3
    elif first == 0x82:
        length, pos = int.from_bytes(blob[pos + 1 : pos + 5], "little"), pos + 5
    else:
        return ""

    raw = blob[pos : pos + length]
    if len(raw) != length:
        return ""
    return _clean(raw.decode("utf-8", errors="replace"))


def message_text(text: str | None, attributed_body: bytes | None) -> str:
    """The message's words, from whichever column holds them."""
    cleaned = _clean(text) if text else ""
    return cleaned or decode_attributed_body(attributed_body)
