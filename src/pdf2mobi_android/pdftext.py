"""PDF text extraction for Android — pure Python only.

Why this module exists separately from :mod:`pdf2mobi.analysis`
-------------------------------------------------------------

The desktop build uses PyMuPDF, which is a fast native library with an excellent
layout model. PyMuPDF has **no Android build** (python-for-android ships no
recipe for it, and there is no aarch64 Android wheel), so the Android app cannot
use it.

``pypdf``, by contrast, is **pure Python** and therefore runs unmodified on
Android. It exposes everything we actually need:

* per-character text with the transformation matrix and font size, from which
  headings, paragraphs and reading order can be reconstructed;
* document metadata (title, author);
* the outline, used for the table of contents;
* embedded images and page geometry, used to detect scanned pages.

The trade-off is speed: pypdf is slower than PyMuPDF. That is acceptable on a
phone because the extraction happens once per book, and the UI reports progress.

Everything here avoids ``pdfminer.six`` deliberately: it hard-depends on
``cryptography`` (a native package with no Android recipe), which would make the
app unbuildable.
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .encoding_fix import repair_cjk

# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #


class PdfKind(str, Enum):
    TEXT = "text"
    SCANNED = "scanned"
    HYBRID = "hybrid"
    EMPTY = "empty"
    ERROR = "error"
    ENCRYPTED = "encrypted"


@dataclass
class TextSpan:
    """A run of characters sharing one font size."""

    text: str
    size: float
    x: float
    y: float


@dataclass
class TextLine:
    spans: List[TextSpan] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def size(self) -> float:
        sizes = [s.size for s in self.spans if s.text.strip()]
        return max(sizes) if sizes else 0.0

    @property
    def y(self) -> float:
        return self.spans[0].y if self.spans else 0.0

    @property
    def x(self) -> float:
        return self.spans[0].x if self.spans else 0.0


@dataclass
class PageInfo:
    index: int
    char_count: int
    image_count: int
    width: float
    height: float
    has_text: bool = False
    is_scan_like: bool = False


@dataclass
class TocEntry:
    level: int
    title: str
    page: int


@dataclass
class PdfAnalysis:
    path: Path
    kind: PdfKind
    page_count: int
    title: Optional[str] = None
    author: Optional[str] = None
    toc: List[TocEntry] = field(default_factory=list)
    pages: List[PageInfo] = field(default_factory=list)
    error: Optional[str] = None
    encrypted: bool = False

    @property
    def has_toc(self) -> bool:
        return bool(self.toc)

    @property
    def needs_ocr(self) -> bool:
        return self.kind in (PdfKind.SCANNED, PdfKind.HYBRID)

    @property
    def total_chars(self) -> int:
        return sum(p.char_count for p in self.pages)

    def summary(self) -> str:
        if self.kind is PdfKind.ERROR:
            return f"无法读取（{self.error}）"
        if self.kind is PdfKind.ENCRYPTED:
            return "已加密，需要密码"
        labels = {
            PdfKind.TEXT: "文字版",
            PdfKind.SCANNED: "扫描版",
            PdfKind.HYBRID: "混合版",
            PdfKind.EMPTY: "空白",
        }
        label = labels.get(self.kind, self.kind.value)
        return (f"{label}：{self.page_count} 页，{self.total_chars:,} 字"
                f"{'，有目录' if self.has_toc else ''}")


# --------------------------------------------------------------------------- #
# Thresholds (kept identical in spirit to the desktop build)
# --------------------------------------------------------------------------- #

MIN_CHARS_PER_TEXT_PAGE = 12
SCAN_MAX_CHARS = 40
TEXT_DOMINANT_RATIO = 0.60
SCANNED_DOMINANT_RATIO = 0.25


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x3040 <= cp <= 0x30FF
        or 0x3400 <= cp <= 0x4DBF
        or 0x4E00 <= cp <= 0x9FFF
        or 0xF900 <= cp <= 0xFAFF
        or 0xFF00 <= cp <= 0xFFEF
    )


_NORMALISE = {
    "\u00ad": "",       # soft hyphen
    "\ufb01": "fi", "\ufb02": "fl", "\ufb00": "ff",
    "\ufb03": "ffi", "\ufb04": "ffl",
}


def _clean(text: str) -> str:
    for bad, good in _NORMALISE.items():
        if bad in text:
            text = text.replace(bad, good)
    return unicodedata.normalize("NFC", text)


# --------------------------------------------------------------------------- #
# Line reconstruction
# --------------------------------------------------------------------------- #

def _join_spans(parts: Sequence[str]) -> str:
    """Join text runs, adding a space only where the script needs one.

    CJK has no inter-word spaces and PDF runs frequently split mid-word in
    Latin text, so neither ``""`` nor ``" "`` is universally correct.
    """
    out: List[str] = []
    for raw in parts:
        if not raw:
            continue
        if not out:
            out.append(raw)
            continue
        prev, cur = out[-1][-1], raw[0]
        if (
            prev and cur
            and not prev.isspace() and not cur.isspace()
            and not _is_cjk(prev) and not _is_cjk(cur)
            and prev not in "-/\u2014"
            and cur not in ".,;:!?)]}\u2019\u201d"
        ):
            out.append(" ")
        out.append(raw)
    return "".join(out)


def extract_page_lines(page) -> List[TextLine]:
    """Group a pypdf page's text runs into visual lines.

    ``visitor_text`` reports runs in content-stream order, which is usually but
    not always reading order. Rows are therefore bucketed by their y coordinate
    so that a line assembled from several runs stays together, and the buckets
    are then sorted top-to-bottom, left-to-right.
    """
    runs: List[TextSpan] = []

    def visitor(text, cm, tm, font_dict, font_size):
        if not text or not text.strip():
            # Preserve meaningful whitespace between runs.
            if text and runs and runs[-1].text and not runs[-1].text.endswith(" "):
                runs.append(TextSpan(" ", float(font_size or 0),
                                     float(tm[4]), float(tm[5])))
            return
        runs.append(TextSpan(
            repair_cjk(text),
            float(font_size or 0),
            float(tm[4]),
            float(tm[5]),
        ))

    try:
        page.extract_text(visitor_text=visitor)
    except Exception:
        # A malformed page must not abort the whole book.
        try:
            raw = repair_cjk(page.extract_text() or "")
        except Exception:
            raw = ""
        return [TextLine([TextSpan(l, 0.0, 0.0, 0.0)]) for l in raw.splitlines()]

    if not runs:
        return []

    # Bucket runs into lines by vertical position. The tolerance scales with the
    # font size so large headings are not split.
    runs.sort(key=lambda r: (-r.y, r.x))
    lines: List[TextLine] = []
    for run in runs:
        placed = False
        for line in lines:
            ref_size = max(line.size, run.size, 1.0)
            if abs(line.y - run.y) <= ref_size * 0.5:
                line.spans.append(run)
                placed = True
                break
        if not placed:
            lines.append(TextLine([run]))

    for line in lines:
        line.spans.sort(key=lambda s: s.x)
    lines.sort(key=lambda l: (round(-l.y, 1), l.x))
    return [l for l in lines if l.text.strip()]


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #

def _page_image_count(page) -> int:
    try:
        return len(page.images)
    except Exception:
        return 0


def _page_size(page) -> Tuple[float, float]:
    try:
        box = page.mediabox
        return float(box.width), float(box.height)
    except Exception:
        return 612.0, 792.0


def _extract_toc(reader: PdfReader) -> List[TocEntry]:
    """Flatten pypdf's nested outline into (level, title, page) entries.

    Two destination styles exist in the wild and both must be handled:

    * ``/Dest`` with an explicit destination array or name, which
      ``get_destination_page_number`` resolves;
    * a direct ``/Page`` reference, which that helper returns 0 for. Matching the
      referenced page object against the page list recovers the number.
    """
    entries: List[TocEntry] = []
    try:
        outline = reader.outline
    except Exception:
        return entries
    if not outline:
        return entries

    # Build a lookup from page object identity to 1-based page number once.
    page_numbers: Dict[int, int] = {}
    page_refs: Dict[int, int] = {}
    try:
        for i, page in enumerate(reader.pages):
            page_numbers[id(page.get_object())] = i + 1
            ref = getattr(page, "indirect_reference", None)
            if ref is not None:
                page_refs[id(ref)] = i + 1
    except Exception:
        pass

    def page_number_of(item, dest) -> int:
        # 1) Direct /Page reference (the style that defeats the pypdf helper).
        ref = item.get("/Page") if hasattr(item, "get") else None
        if ref is not None:
            try:
                obj_id = id(ref.get_object())
                if obj_id in page_numbers:
                    return page_numbers[obj_id]
            except Exception:
                pass
            try:
                if id(ref) in page_refs:
                    return page_refs[id(ref)]
            except Exception:
                pass
        # 2) Standard destination resolution.
        try:
            number = reader.get_destination_page_number(item)
            if number is not None and int(number) >= 0:
                return int(number) + 1
        except Exception:
            pass
        # 3) A raw array whose first element is a page reference.
        if isinstance(dest, list) and dest:
            try:
                obj_id = id(dest[0].get_object())
                if obj_id in page_numbers:
                    return page_numbers[obj_id]
            except Exception:
                pass
        if isinstance(dest, int):
            return dest + 1
        return 0

    def walk(items, level: int) -> None:
        for item in items:
            if isinstance(item, list):
                walk(item, level + 1)
                continue
            try:
                title = (item.title or "").strip()
            except Exception:
                continue
            if not title:
                continue
            try:
                dest = item.get("/Dest")
            except Exception:
                dest = None
            entries.append(TocEntry(level=max(1, level), title=title,
                                    page=page_number_of(item, dest)))

    try:
        walk(outline, 1)
    except Exception:
        return []
    return entries


def analyse_pdf(path: Path) -> PdfAnalysis:
    """Classify a PDF and collect metadata, without rendering anything."""
    path = Path(path)
    if not path.exists():
        return PdfAnalysis(path, PdfKind.ERROR, 0, error="文件不存在")
    if path.stat().st_size == 0:
        return PdfAnalysis(path, PdfKind.ERROR, 0, error="文件为空")

    try:
        reader = PdfReader(str(path))
    except PdfReadError as exc:
        return PdfAnalysis(path, PdfKind.ERROR, 0, error=str(exc))
    except Exception as exc:
        return PdfAnalysis(path, PdfKind.ERROR, 0, error=f"{type(exc).__name__}: {exc}")

    if reader.is_encrypted:
        # Try the common empty password before giving up.
        try:
            if reader.decrypt("") == 0:
                return PdfAnalysis(path, PdfKind.ENCRYPTED, len(reader.pages),
                                   encrypted=True, error="需要密码")
        except Exception:
            return PdfAnalysis(path, PdfKind.ENCRYPTED, len(reader.pages),
                               encrypted=True, error="需要密码")

    try:
        page_count = len(reader.pages)
    except Exception as exc:
        return PdfAnalysis(path, PdfKind.ERROR, 0, error=str(exc))

    meta = {}
    try:
        meta = dict(reader.metadata or {})
    except Exception:
        pass
    title = str(meta.get("/Title") or "").strip() or None
    author = str(meta.get("/Author") or "").strip() or None
    toc = _extract_toc(reader)

    pages: List[PageInfo] = []
    for i in range(page_count):
        try:
            page = reader.pages[i]
            text = repair_cjk(page.extract_text() or "")
            stripped = text.strip()
            n_chars = len(stripped)
            n_images = _page_image_count(page)
            w, h = _page_size(page)
        except Exception:
            pages.append(PageInfo(i, 0, 0, 612.0, 792.0))
            continue

        has_text = n_chars >= MIN_CHARS_PER_TEXT_PAGE
        # Without a layout engine we cannot measure image coverage, so a page is
        # treated as a scan when it carries images and effectively no text.
        is_scan = n_images >= 1 and n_chars < MIN_CHARS_PER_TEXT_PAGE and not has_text
        pages.append(PageInfo(i, n_chars, n_images, w, h, has_text, is_scan))

    return PdfAnalysis(
        path=path,
        kind=classify(pages),
        page_count=page_count,
        title=title,
        author=author,
        toc=toc,
        pages=pages,
    )


def classify(pages: Sequence[PageInfo]) -> PdfKind:
    """Document level verdict from per page measurements."""
    considered = [p for p in pages if not (p.char_count == 0 and p.image_count == 0)]
    if not considered:
        return PdfKind.EMPTY
    n = len(considered)
    text_pages = sum(1 for p in considered if p.has_text)
    scan_pages = sum(1 for p in considered if p.is_scan_like)
    ratio = text_pages / n
    if ratio >= TEXT_DOMINANT_RATIO:
        return PdfKind.HYBRID if scan_pages / n > 0.15 else PdfKind.TEXT
    if ratio <= SCANNED_DOMINANT_RATIO:
        return PdfKind.SCANNED
    return PdfKind.HYBRID
