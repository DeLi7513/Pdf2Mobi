"""Build a reflowable book from a PDF, using text only.

This is the Android counterpart of the desktop ``extract`` module. It is
deliberately text-first, per the requirement that images need not be preserved:

* every page's text is recovered and reflowed into clean HTML;
* headings are inferred from font size relative to the document body size;
* the PDF outline becomes the table of contents, with a generated one as
  fallback;
* **images are dropped by default**. A page that is a pure scan therefore yields
  nothing on its own, which is where OCR comes in: the caller may supply an OCR
  callback that turns a rendered page into text.

Keeping the image path optional means the phone never builds a multi hundred
megabyte book of page bitmaps, which is exactly the behaviour asked for.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from pypdf import PdfReader

from .pdftext import PdfAnalysis, PdfKind, TextLine, extract_page_lines

# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #


@dataclass
class Section:
    id: str
    title: str
    html_body: str
    text: str = ""
    from_ocr: bool = False


@dataclass
class Book:
    title: str
    author: str
    sections: List[Section] = field(default_factory=list)
    toc: List[Tuple[int, str, str]] = field(default_factory=list)
    css: str = ""
    language: str = "zh"

    def plain_text(self) -> str:
        out = [self.title, ""]
        for s in self.sections:
            if s.title:
                out += [s.title, ""]
            out += [s.text, ""]
        return "\n".join(out).strip() + "\n"


@dataclass
class BuildOptions:
    keep_toc: bool = True
    #: Skip pages that produced no text at all.
    skip_empty: bool = True
    #: Optional OCR hook: receives (page_number_1_based, pypdf_page) and returns
    #: extracted text, or "" when unavailable.
    ocr_page: Optional[Callable[[int, object], str]] = None
    #: Extra CSS appended to the built in stylesheet.
    extra_css: str = ""


# --------------------------------------------------------------------------- #
# HTML helpers
# --------------------------------------------------------------------------- #

DEFAULT_CSS = """
body { line-height: 1.65; margin: 0 5%; text-align: justify; word-wrap: break-word; }
h1, h2, h3, h4 { line-height: 1.3; margin: 1.3em 0 0.55em; text-align: left; }
h1 { font-size: 1.7em; } h2 { font-size: 1.45em; }
h3 { font-size: 1.22em; } h4 { font-size: 1.08em; }
p { margin: 0 0 0.72em; text-indent: 2em; }
p.flat { text-indent: 0; }
.ocr { color: #000; }
.note { color: #777; font-size: 0.85em; text-indent: 0; }
"""


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _is_cjk_char(ch: str) -> bool:
    cp = ord(ch)
    return (0x3040 <= cp <= 0x30FF or 0x3400 <= cp <= 0x4DBF
            or 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF
            or 0xFF00 <= cp <= 0xFFEF)


def _cjk_heavy(text: str) -> bool:
    sample = text[:200]
    if not sample:
        return False
    return sum(1 for c in sample if _is_cjk_char(c)) / len(sample) > 0.3


def _join_lines(parts: Sequence[str]) -> str:
    """Join wrapped lines, inserting a space only where the script needs one."""
    out: List[str] = []
    for raw in parts:
        if not raw:
            continue
        if not out:
            out.append(raw)
            continue
        prev, cur = out[-1][-1], raw[0]
        need = (
            not prev.isspace() and not cur.isspace()
            and not _is_cjk_char(prev) and not _is_cjk_char(cur)
            and prev not in "-/\u2014"
            and cur not in ".,;:!?)]}\u2019\u201d"
        )
        # A hyphen at a line end in Latin text is a soft break.
        if prev == "-" and not _is_cjk_char(cur):
            out[-1] = out[-1][:-1]
            need = False
        out.append((" " if need else "") + raw)
    return "".join(out)


_PARAGRAPH_END = re.compile(r"[.!?\u3002\uff01\uff1f\u201d\u2019\"']\s*$")


def _body_font_size(all_sizes: Dict[float, int]) -> float:
    if not all_sizes:
        return 11.0
    return max(all_sizes.items(), key=lambda kv: kv[1])[0]


def heading_level(size: float, body: float) -> int:
    """Map a line's font size to an h1..h4 level, or 0 for body text."""
    if body <= 0 or size <= 0:
        return 0
    ratio = size / body
    if ratio >= 1.75:
        return 1
    if ratio >= 1.45:
        return 2
    if ratio >= 1.22:
        return 3
    if ratio >= 1.10:
        return 4
    return 0


def _lines_to_html(lines: Sequence[TextLine], body: float) -> Tuple[List[str], str]:
    """Turn reconstructed lines into HTML blocks plus the plain text."""
    blocks: List[str] = []
    plain: List[str] = []
    if not lines:
        return blocks, ""

    # Group consecutive lines into paragraphs. A new paragraph starts when the
    # previous line ended a sentence, when there is a clear vertical gap, or
    # when the font size changes (i.e. a heading).
    paragraphs: List[List[TextLine]] = []
    current: List[TextLine] = []
    prev: Optional[TextLine] = None

    for line in lines:
        text = line.text.strip()
        if not text:
            if current:
                paragraphs.append(current)
                current = []
            prev = None
            continue

        level = heading_level(line.size, body)
        prev_level = heading_level(prev.size, body) if prev else 0

        start_new = False
        if current and prev is not None:
            gap = prev.y - line.y           # y grows upward in PDF space
            ref = max(line.size, prev.size, body, 1.0)
            if gap > ref * 1.6:
                start_new = True
            elif level != prev_level:
                start_new = True
            elif _PARAGRAPH_END.search(prev.text.strip()) and gap > ref * 1.15:
                start_new = True
            elif level == 0 and prev_level == 0 and gap > ref * 1.35:
                start_new = True
        if start_new and current:
            paragraphs.append(current)
            current = []
        current.append(line)
        prev = line

    if current:
        paragraphs.append(current)

    for para in paragraphs:
        texts = [l.text.strip() for l in para if l.text.strip()]
        if not texts:
            continue
        joined = re.sub(r"[ \t]+", " ", _join_lines(texts)).strip()
        if not joined:
            continue
        size = max((l.size for l in para), default=body)
        level = heading_level(size, body)

        # Headings are short; a long "heading" is really a styled paragraph.
        if level and len(joined) <= 80:
            blocks.append(f"<h{level}>{_esc(joined)}</h{level}>")
        elif re.match(r"^[\u2022\-\*\u2013]\s", joined) and len(joined) < 200:
            blocks.append(f'<p class="flat">{_esc(joined)}</p>')
        else:
            blocks.append(f"<p>{_esc(joined)}</p>")
        plain.append(joined)

    return blocks, "\n\n".join(plain)


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #

def _sanitise_title(name: str) -> str:
    stem = Path(name).stem
    return re.sub(r"\s+", " ", re.sub(r"[_]+", " ", stem)).strip() or "未命名"


def build_book(
    source: Path,
    analysis: PdfAnalysis,
    options: BuildOptions,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> Book:
    """Extract ``source`` into a :class:`Book`.

    ``progress(current, total, message)`` is called for each page so a UI can
    show movement on long documents.
    """
    source = Path(source)
    reader = PdfReader(str(source))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            pass

    total = len(reader.pages)

    # ---- pass 1: font size histogram, so body text can be identified --------
    sizes: Dict[float, int] = {}
    sample = min(total, 40)
    for i in range(sample):
        try:
            lines = extract_page_lines(reader.pages[i])
        except Exception:
            continue
        for line in lines:
            for span in line.spans:
                if span.text.strip() and span.size > 0:
                    key = round(span.size, 1)
                    sizes[key] = sizes.get(key, 0) + len(span.text.strip())
    body_size = _body_font_size(sizes)

    # ---- pass 2: build sections -------------------------------------------
    sections: List[Section] = []
    for i in range(total):
        sec_id = f"page{i + 1:05d}"
        try:
            lines = extract_page_lines(reader.pages[i])
        except Exception:
            lines = []

        blocks, plain = _lines_to_html(lines, body_size)
        from_ocr = False

        # A page with no usable text is a scan (or a pure image page). If the
        # caller supplied an OCR hook, try it before giving up.
        if not plain.strip() and options.ocr_page is not None:
            try:
                ocr_text = options.ocr_page(i + 1, reader.pages[i]) or ""
            except Exception:
                ocr_text = ""
            ocr_text = ocr_text.strip()
            if ocr_text:
                paras = [p.strip() for p in re.split(r"\n\s*\n|\n", ocr_text) if p.strip()]
                blocks = [f'<p class="ocr">{_esc(p)}</p>' for p in paras]
                plain = "\n\n".join(paras)
                from_ocr = True

        if progress:
            progress(i + 1, total, sec_id)

        if not plain.strip():
            if options.skip_empty:
                continue
            blocks = ['<p class="note">（本页没有可提取的文字）</p>']

        sections.append(Section(id=sec_id, title="", html_body="\n".join(blocks),
                                text=plain, from_ocr=from_ocr))

    # ---- table of contents -------------------------------------------------
    toc: List[Tuple[int, str, str]] = []
    if options.keep_toc:
        toc = _map_toc(analysis, sections)
        if not toc:
            toc = _generated_toc(sections)

    by_id = {s.id: s for s in sections}
    for _lvl, title, sid in toc:
        if sid in by_id and not by_id[sid].title:
            by_id[sid].title = title

    title = analysis.title or _sanitise_title(source.name)
    author = analysis.author or "未知作者"
    css = DEFAULT_CSS + ("\n" + options.extra_css if options.extra_css else "")

    return Book(title=title, author=author, sections=sections, toc=toc, css=css)


def _map_toc(analysis: PdfAnalysis, sections: Sequence[Section]) -> List[Tuple[int, str, str]]:
    valid = {s.id for s in sections}
    if not valid:
        return []

    def resolve(page_no: int) -> Optional[str]:
        for delta in (0, 1, -1, 2, -2):
            cand = f"page{page_no + delta:05d}"
            if cand in valid:
                return cand
        return None

    out: List[Tuple[int, str, str]] = []
    seen = set()
    for entry in analysis.toc:
        if not entry.page:
            continue
        target = resolve(entry.page)
        if not target:
            continue
        key = (entry.level, target)
        if key in seen:
            continue
        seen.add(key)
        out.append((entry.level, entry.title, target))
    return out


_HEADING_RE = re.compile(r"<h([1-4])>(.*?)</h\1>", re.DOTALL | re.IGNORECASE)


def _generated_toc(sections: Sequence[Section]) -> List[Tuple[int, str, str]]:
    """Fallback TOC mined from the headings we detected."""
    toc: List[Tuple[int, str, str]] = []
    for sec in sections:
        for m in _HEADING_RE.finditer(sec.html_body):
            level = int(m.group(1))
            text = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
            if text:
                toc.append((level, text, sec.id))
    return toc
