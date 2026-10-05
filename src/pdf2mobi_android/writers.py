"""EPUB and MOBI writers for the Android build.

Both writers are pure Python and therefore run on Android unchanged. They are
adapted from the desktop project; the differences are that the book model comes
from the text-only builder and no image records are emitted, which keeps the
output small — the stated requirement for the phone build.
"""

from __future__ import annotations

import html
import re
import struct
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .builder import Book

# --------------------------------------------------------------------------- #
# EPUB 3
# --------------------------------------------------------------------------- #

CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

XHTML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}" lang="{lang}">
<head><meta charset="utf-8"/><title>{title}</title>
<link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
{body}
</body>
</html>
"""


def _xe(text: str) -> str:
    return html.escape(text or "", quote=True)


def _safe(raw: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in raw) or "id"


def write_epub(book: Book, dest: Path) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    lang = book.language or "zh"
    uid = f"urn:uuid:{uuid.uuid4()}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Stable, filesystem-safe ids.
    sections = []
    used = set()
    id_map: dict[str, str] = {}
    for i, sec in enumerate(book.sections):
        sid = _safe(sec.id) or f"sec{i:04d}"
        while sid in used:
            sid = f"{sid}_{i}"
        used.add(sid)
        sections.append((sid, sec))
        id_map.setdefault(sec.id, sid)

    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
    ]
    spine = []
    for sid, _sec in sections:
        manifest.append(f'<item id="{sid}" href="{sid}.xhtml" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{sid}"/>')

    meta = [
        f'<dc:identifier id="bookid">{_xe(uid)}</dc:identifier>',
        f"<dc:title>{_xe(book.title)}</dc:title>",
        f"<dc:language>{_xe(lang)}</dc:language>",
        f"<dc:creator>{_xe(book.author)}</dc:creator>",
        f'<meta property="dcterms:modified">{now}</meta>',
        '<meta name="generator" content="pdf2mobi-android"/>',
    ]

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    {chr(10).join('    ' + m for m in meta)}
  </metadata>
  <manifest>
    {chr(10).join('    ' + m for m in manifest)}
  </manifest>
  <spine toc="ncx">
    {chr(10).join('    ' + s for s in spine)}
  </spine>
</package>
"""

    nav_inner = _nav_tree(book, id_map)
    nav = XHTML.format(
        lang=_xe(lang), title=_xe(book.title),
        body='<nav epub:type="toc" id="toc" xmlns:epub="http://www.idpf.org/2007/ops">'
             f"<h1>目录</h1>{nav_inner}</nav>",
    )
    ncx = _ncx(book, id_map, uid)

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER_XML)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/nav.xhtml", nav)
        z.writestr("OEBPS/toc.ncx", ncx)
        z.writestr("OEBPS/style.css", book.css or "")
        for sid, sec in sections:
            z.writestr(f"OEBPS/{sid}.xhtml",
                       XHTML.format(lang=_xe(lang),
                                    title=_xe(sec.title or book.title),
                                    body=sec.html_body))
    return dest


def _tree(entries):
    """Build a nested tree from (level, title, target) tuples."""
    root: list = []
    stack: list = []
    for level, title, target in entries:
        level = max(1, level)
        node = [level, title, target, []]
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            stack[-1][3].append(node)
        else:
            root.append(node)
        stack.append(node)
    return root


def _nav_tree(book: Book, id_map) -> str:
    if not book.toc:
        items = "".join(
            f'<li><a href="{id_map.get(s.id, s.id)}.xhtml">'
            f"{_xe(s.title or f'第 {i + 1} 页')}</a></li>"
            for i, s in enumerate(book.sections)
        )
        return f"<ol>{items}</ol>"

    nodes = _tree([(lvl, t, id_map.get(sid, sid)) for lvl, t, sid in book.toc])

    def render(items) -> str:
        out = ["<ol>"]
        for _lvl, title, target, kids in items:
            out.append("<li>")
            out.append(f'<a href="{target}.xhtml">{_xe(title)}</a>')
            if kids:
                out.append(render(kids))
            out.append("</li>")
        out.append("</ol>")
        return "".join(out)

    return render(nodes)


def _ncx(book: Book, id_map, uid: str) -> str:
    if not book.toc:
        entries = [(1, s.title or f"第 {i + 1} 页", id_map.get(s.id, s.id))
                   for i, s in enumerate(book.sections)]
    else:
        entries = [(lvl, t, id_map.get(sid, sid)) for lvl, t, sid in book.toc]

    nodes = _tree(entries)
    counter = [0]

    def render(items) -> str:
        out = []
        for _lvl, title, target, kids in items:
            counter[0] += 1
            n = counter[0]
            inner = (f"<navLabel><text>{_xe(title)}</text></navLabel>"
                     f'<content src="{target}.xhtml"/>')
            if kids:
                inner += render(kids)
            out.append(f'<navPoint id="np{n}" playOrder="{n}">{inner}</navPoint>')
        return "".join(out)

    depth = max((e[0] for e in entries), default=1)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{_xe(uid)}"/>
    <meta name="dtb:depth" content="{depth}"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{_xe(book.title)}</text></docTitle>
  <navMap>{render(nodes)}</navMap>
</ncx>
"""


# --------------------------------------------------------------------------- #
# MOBI (PalmDOC) — text only, no image records
# --------------------------------------------------------------------------- #

MOBI_HEADER_LENGTH = 232
RECORD_SIZE = 4096

EXTH_TITLE = 503
EXTH_AUTHOR = 100
EXTH_LANGUAGE = 524
EXTH_CREATOR = 204


def _escape_record(data: bytes) -> bytes:
    """Map bytes into an ASCII-safe domain before compression.

    PalmDOC treats any byte >= 0x80 as the start of a two byte back reference, so
    raw UTF-8 would corrupt the record. Each byte outside printable ASCII becomes
    a three byte group introduced by 0x0A, which is never a back reference lead.
    """
    out = bytearray()
    for b in data:
        if 0x20 <= b <= 0x7E:
            out.append(b)
        else:
            out.append(0x0A)
            out.append(0x30 + (b >> 4))
            out.append(0x30 + (b & 0x0F))
    return _palmdoc_compress(bytes(out))


def _palmdoc_compress(data: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        best_len = best_dist = 0
        start = max(0, i - 2047)
        max_len = min(10, n - i)
        if max_len >= 3 and i - start >= 3:
            key = data[i:i + 3]
            j, scanned = i - 1, 0
            while j >= start and scanned < 64 and best_len < max_len:
                if data[j:j + 3] == key:
                    length = 3
                    while length < max_len and data[j + length] == data[i + length]:
                        length += 1
                    if length > best_len:
                        best_len, best_dist = length, i - j
                        if length == max_len:
                            break
                j -= 1
                scanned += 1
        if best_len >= 3 and 1 <= best_dist <= 2047:
            out.extend(struct.pack(">H", 0x8000 | ((best_dist & 0x7FF) << 3) | (best_len - 3)))
            i += best_len
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def decode_record(data: bytes) -> bytes:
    """Inverse of :func:`_escape_record`; used by the self test."""
    raw = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b < 0x80:
            raw.append(b)
            i += 1
        else:
            if i + 1 >= n:
                break
            v = (b << 8) | data[i + 1]
            dist, ln = (v >> 3) & 0x7FF, (v & 0x07) + 3
            if dist == 0 or dist > len(raw):
                break
            for k in range(ln):
                raw.append(raw[len(raw) - dist])
            i += 2
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == 0x0A and i + 2 < len(raw):
            out.append(((raw[i + 1] - 0x30) & 0x0F) << 4 | ((raw[i + 2] - 0x30) & 0x0F))
            i += 3
        else:
            out.append(raw[i])
            i += 1
    return bytes(out)


def _exth(title: str, author: str, language: str) -> bytes:
    def rec(t, data: bytes) -> bytes:
        return struct.pack(">II", t, len(data) + 8) + data

    body = (rec(EXTH_TITLE, title.encode())
            + rec(EXTH_AUTHOR, author.encode())
            + rec(EXTH_LANGUAGE, language.encode())
            + rec(EXTH_CREATOR, b"pdf2mobi-android"))
    return b"EXTH" + struct.pack(">II", 12 + len(body), 4) + body


def _record0(text_len: int, text_recs: int, exth: bytes, title: str) -> bytes:
    full_name = title.encode("utf-8")
    palmdoc = struct.pack(">HHIHHHH", 2, 0, text_len, text_recs, RECORD_SIZE, 0, 0)

    h = bytearray(MOBI_HEADER_LENGTH)
    h[0x00:0x04] = b"MOBI"
    struct.pack_into(">I", h, 0x04, MOBI_HEADER_LENGTH)
    struct.pack_into(">I", h, 0x08, 2)            # type: book
    struct.pack_into(">I", h, 0x0C, 65001)        # UTF-8
    struct.pack_into(">I", h, 0x10, 0x12345678)
    struct.pack_into(">I", h, 0x14, 6)
    for off in (0x18, 0x1C, 0x20, 0x24, 0x38):
        struct.pack_into(">I", h, off, 0xFFFFFFFF)
    name_off = 16 + MOBI_HEADER_LENGTH + len(exth) + 4
    struct.pack_into(">II", h, 0x3C, name_off, len(full_name))
    struct.pack_into(">I", h, 0x44, 0x00000804)   # locale zh-CN
    struct.pack_into(">I", h, 0x48, 0)
    struct.pack_into(">I", h, 0x4C, 0)
    struct.pack_into(">I", h, 0x50, 6)
    struct.pack_into(">I", h, 0x54, 0xFFFFFFFF)   # no images
    struct.pack_into(">II", h, 0x58, 0, 0)
    struct.pack_into(">II", h, 0x60, 0, 0)
    struct.pack_into(">I", h, 0x68, 0x40)         # EXTH present
    struct.pack_into(">I", h, 0x8C, 0xFFFFFFFF)
    struct.pack_into(">I", h, 0x90, 0xFFFFFFFF)
    struct.pack_into(">I", h, 0x94, 0)
    struct.pack_into(">I", h, 0x98, 0)
    struct.pack_into(">I", h, 0x9C, 0)
    struct.pack_into(">II", h, 0xA0, 0, 0)
    struct.pack_into(">HH", h, 0xA8, 1, text_recs)
    struct.pack_into(">I", h, 0xAC, 1)
    struct.pack_into(">II", h, 0xB0, 0xFFFFFFFF, 1)
    struct.pack_into(">II", h, 0xB8, 0xFFFFFFFF, 1)
    struct.pack_into(">II", h, 0xC0, 0, 0)
    struct.pack_into(">II", h, 0xC8, 0xFFFFFFFF, 0)
    struct.pack_into(">I", h, 0xD0, 0xFFFFFFFF)
    struct.pack_into(">I", h, 0xD4, 0)
    struct.pack_into(">I", h, 0xD8, 0xFFFFFFFF)

    return palmdoc + bytes(h) + exth + struct.pack(">I", 0) + full_name


_TAG = re.compile(r"<[^>]+>")


def _basic_html(fragment: str) -> str:
    """Reduce our XHTML to the small HTML subset MOBI readers understand."""
    s = fragment
    for a, b in (
        ("<p class=\"ocr\">", "<p>"),
        ("<p class=\"note\">", "<p>"),
        ('<p class="flat">', "<p>"),
    ):
        s = s.replace(a, b)
    return re.sub(r"</?(?:div|span|nav|section)\b[^>]*>", "", s)


def write_mobi(book: Book, dest: Path) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    parts = ["<html><head><guide></guide></head><body>"]

    # Table of contents as a page at the front.
    entries = book.toc or [(1, s.title or f"第 {i + 1} 部分", s.id)
                           for i, s in enumerate(book.sections)]
    if entries:
        parts.append('<div id="toc"><h1>目录</h1>')
        for level, title, sid in entries:
            pad = "" if level <= 1 else "&nbsp;" * (4 * (level - 1))
            parts.append(f'<p>{pad}<a href="#{html.escape(sid)}">{html.escape(title)}</a></p>')
        parts.append("</div><mbp:pagebreak/>")

    for sec in book.sections:
        parts.append(f'<div id="{html.escape(sec.id)}">{_basic_html(sec.html_body)}</div>')
        parts.append("<mbp:pagebreak/>")
    parts.append("</body></html>")

    text = "".join(parts).encode("utf-8")
    text_recs = max(1, (len(text) + RECORD_SIZE - 1) // RECORD_SIZE)

    records = [_record0(len(text), text_recs, _exth(book.title, book.author, book.language), book.title)]
    for i in range(text_recs):
        records.append(_escape_record(text[i * RECORD_SIZE:(i + 1) * RECORD_SIZE]))
    # FCIS / FLIS trailers that MOBI 6 readers expect.
    records.append(b"FCIS" + struct.pack(">I", 0x14) + struct.pack(">I", 0x10) + b"\x00" * 32)
    records.append(b"FLIS" + struct.pack(">I", 8) + struct.pack(">I", 0x41) + b"\x00" * 32)

    nrec = len(records)
    header = bytearray(78)
    name = (book.title or "book").encode("utf-8", "ignore")[:31]
    header[0:len(name)] = name
    struct.pack_into(">I", header, 60, 0x424F4F4B)   # 'BOOK'
    struct.pack_into(">I", header, 64, 0x4D4F4249)   # 'MOBI'
    struct.pack_into(">H", header, 76, nrec)

    offset = 78 + nrec * 8 + 2
    info = bytearray()
    payload = bytearray()
    for rec in records:
        info += struct.pack(">I", offset) + b"\x00\x00\x00\x00"
        payload += rec
        offset += len(rec)

    with open(dest, "wb") as fh:
        fh.write(bytes(header) + bytes(info) + b"\x00\x00" + bytes(payload))
    return dest
