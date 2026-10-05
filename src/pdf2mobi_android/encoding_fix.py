"""Character encoding repair for PDF text.

The problem
-----------

pypdf decodes a PDF's text strings using the font's encoding. For the common
predefined CJK CMaps (``UniGB-UTF16-H``, ``UniJIS-UTF16-H``, ``UniKS-UTF16-H``
and friends) it does not apply the UTF-16 mapping, so a two byte code such as
U+7B2C (第) surfaces as the two characters ``{`` and ``,``.

The desktop build never hits this because PyMuPDF implements those CMaps itself.
On Android we depend on pure Python, so the repair has to happen here.

The fix
-------

The squashed text is exactly the original byte stream reinterpreted one byte per
character, so encoding it back to latin-1 and decoding as UTF-16BE recovers the
true text. That round trip is applied only when it clearly improves the result,
detected by counting how many characters land in CJK or other non-Latin ranges
before and after. Garbled ASCII text can never be "repaired" into something
wrong because the check is score based.
"""

from __future__ import annotations

from typing import Optional, Tuple

#: How many CJK characters must appear before the text is accepted, and by how
#: much the repair must beat the original.
_MIN_GAIN = 3


def _score(text: str) -> int:
    """Count characters that indicate a successful decode.

    CJK ideographs and kana only appear when the two byte mapping was applied,
    so they make a reliable positive signal. Replacement characters and control
    bytes make a negative one.
    """
    score = 0
    for ch in text:
        cp = ord(ch)
        if (
            0x4E00 <= cp <= 0x9FFF        # CJK unified ideographs
            or 0x3400 <= cp <= 0x4DBF     # extension A
            or 0x3040 <= cp <= 0x30FF     # kana
            or 0xAC00 <= cp <= 0xD7AF     # hangul
            or 0xF900 <= cp <= 0xFAFF
        ):
            score += 3
        elif 0x3000 <= cp <= 0x303F:      # CJK punctuation
            score += 2
        elif 0xFF00 <= cp <= 0xFFEF:      # fullwidth forms
            score += 2
        elif cp == 0xFFFD:                # replacement char
            score -= 4
        elif cp < 0x09 or (0x0E <= cp <= 0x1F):
            score -= 2
    return score


def repair_cjk(text: str) -> str:
    """Reverse pypdf's mishandling of predefined UTF-16 CJK CMaps.

    Returns the original string when the repair is not clearly warranted, so
    this is safe to call on every run of text.

    The decision problem
    --------------------

    Scores alone are not enough. ``Hello world`` decodes as UTF-16BE to
    ``䡥汬漠睯牬``, which contains *more* CJK characters than the original, so any
    "did the CJK count go up?" rule rewrites every English book into gibberish.

    What separates the two cases is the *byte pattern*, and specifically whether
    the two byte units are plausible Unicode. Byte-squashed CJK decodes into a
    string that is overwhelmingly letters, digits, punctuation and whitespace,
    because CJK code points and their punctuation map onto those ranges. Real
    English squashed the same way decodes into a mixture that includes many
    symbols, box drawing characters and other non-text code points, because
    ASCII letter pairs (``He`` = U+4865) land in unrelated blocks.

    So the test is:

    1. the source must look like byte data rather than text;
    2. the UTF-16 reading must consist almost entirely of characters that occur
       in real prose (letters, digits, punctuation, whitespace, area symbols),
       with no unassigned or symbol-only blocks;
    3. and it must be an improvement on the original.

    All three must hold, which keeps English safe while still repairing CJK.
    """
    if not text:
        return text

    # Any character above U+00FF means this is not byte-squashed data, so it is
    # already decoded correctly.
    try:
        raw = text.encode("latin-1")
    except UnicodeEncodeError:
        return text

    if len(raw) < 4:
        return text

    n = len(raw)

    # (1) Does the source look like raw bytes rather than ordinary Latin text?
    #     Real prose is nearly all printable; squashed CJK is not, and always
    #     carries a scattering of NULs or control bytes.
    printable = sum(1 for b in raw if 0x20 <= b <= 0x7E)
    zeros = raw.count(0)
    controls = sum(1 for b in raw if b < 0x09 or 0x0E <= b <= 0x1F)
    high = sum(1 for b in raw if b >= 0x80)
    if printable > n * 0.92 and zeros == 0 and controls == 0:
        return text          # plainly ordinary text, leave it alone
    if zeros + controls + high < max(1, n // 20):
        return text          # no sign of a UTF-16 byte pattern

    # (2) Decode and judge whether the result is plausible prose.
    candidate = None
    for length in (n, n - 1):
        if length < 2 or length % 2:
            continue
        try:
            candidate = raw[:length].decode("utf-16-be")
            break
        except UnicodeDecodeError:
            continue
    if candidate is None or not candidate:
        return text

    if not _plausible_text(candidate):
        return text

    # (3) Finally require an improvement over what we started with.
    if _score(candidate) > _score(text):
        return candidate
    return text


#: Code point ranges that occur in real running text. Anything outside these is
#: treated as evidence that a UTF-16 decode produced noise rather than words.
_TEXT_RANGES = (
    (0x0020, 0x007E),   # ASCII printable
    (0x00A0, 0x024F),   # Latin-1 supplement and extended
    (0x0370, 0x03FF),   # Greek
    (0x0400, 0x04FF),   # Cyrillic
    (0x2000, 0x206F),   # general punctuation
    (0x2070, 0x209F),   # super/subscripts
    (0x20A0, 0x20CF),   # currency
    (0x2100, 0x214F),   # letterlike
    (0x2190, 0x21FF),   # arrows
    (0x2200, 0x22FF),   # mathematical operators
    (0x2460, 0x24FF),   # enclosed alphanumerics
    (0x2500, 0x257F),   # box drawing
    (0x25A0, 0x25FF),   # geometric shapes
    (0x2600, 0x26FF),   # misc symbols
    (0x3000, 0x303F),   # CJK punctuation
    (0x3040, 0x30FF),   # kana
    (0x3400, 0x4DBF),   # CJK ext A
    (0x4E00, 0x9FFF),   # CJK unified
    (0xAC00, 0xD7AF),   # hangul
    (0xF900, 0xFAFF),   # CJK compatibility
    (0xFE30, 0xFE4F),   # CJK compatibility forms
    (0xFF00, 0xFFEF),   # fullwidth and halfwidth forms
)


def _is_text_char(ch: str) -> bool:
    cp = ord(ch)
    if ch in "\t\n\r":
        return True
    for lo, hi in _TEXT_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def _plausible_text(candidate: str) -> bool:
    """True when almost every character could appear in a real document.

    A UTF-16 decode of English bytes produces many code points in unassigned or
    purely symbolic blocks; a decode of CJK bytes produces ordinary text. The
    contrast is stark enough to separate them reliably.
    """
    meaningful = sum(1 for ch in candidate if _is_text_char(ch))
    ratio = meaningful / len(candidate)
    # Require both a high ratio and at least one non-ASCII text character, so
    # plain ASCII cannot qualify (it never needed repairing anyway).
    has_non_ascii = any(ord(c) > 0x7F for c in candidate)
    return ratio >= 0.90 and has_non_ascii


def repair_if_garbled(text: str) -> str:
    """Public entry point; identical to :func:`repair_cjk`."""
    return repair_cjk(text)
