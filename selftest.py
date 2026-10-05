"""Self test for the Android conversion core.

Runs on a desktop too, which is the point: the conversion logic is pure Python,
so it can be verified here before it ever reaches a phone. Kivy and ML Kit are
not needed — only the text pipeline is exercised.

    python selftest.py
"""

from __future__ import annotations

import struct
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "src"))

from pdf2mobi_android.builder import BuildOptions, build_book  # noqa: E402
from pdf2mobi_android.encoding_fix import repair_cjk  # noqa: E402
from pdf2mobi_android.pdftext import PdfKind, analyse_pdf  # noqa: E402
from pdf2mobi_android.writers import decode_record, write_epub, write_mobi  # noqa: E402

_fail = []
_pass = 0


def check(cond, label):
    global _pass
    if cond:
        _pass += 1
        print(f"  ok   {label}")
    else:
        _fail.append(label)
        print(f"  FAIL {label}")


# --------------------------------------------------------------------------- #
# Encoding repair
# --------------------------------------------------------------------------- #

def test_encoding():
    print("encoding repair")
    # Text already correct must be left alone.
    check(repair_cjk("Hello world") == "Hello world", "ascii untouched")
    check(repair_cjk("第一章 绪论") == "第一章 绪论", "proper chinese untouched")
    check(repair_cjk("") == "", "empty safe")

    # Simulate pypdf's squashing of a UTF-16BE CJK string, then repair it.
    original = "第一章 绪论 第1节"
    squashed = original.encode("utf-16-be").decode("latin-1")
    check(repair_cjk(squashed) == original, "utf-16 squashing repaired")

    # Japanese kana should also be recovered.
    jp = "こんにちは世界"
    squashed_jp = jp.encode("utf-16-be").decode("latin-1")
    check(repair_cjk(squashed_jp) == jp, "japanese kana repaired")


# --------------------------------------------------------------------------- #
# Round trip helpers
# --------------------------------------------------------------------------- #

def read_mobi_text(path: Path) -> tuple[str, dict]:
    data = path.read_bytes()
    nrec = struct.unpack(">H", data[76:78])[0]
    offs = [struct.unpack(">I", data[78 + i * 8: 82 + i * 8])[0] for i in range(nrec)]
    rec0 = data[offs[0]:offs[1]]
    tlen = struct.unpack(">I", rec0[4:8])[0]
    trecs = struct.unpack(">H", rec0[8:10])[0]
    decoded = b"".join(decode_record(data[offs[1 + k]:offs[2 + k]]) for k in range(trecs))
    return decoded.decode("utf-8", "replace"), {
        "records": nrec, "offsets": offs, "rec0": rec0,
        "claimed_len": tlen, "text_recs": trecs, "decoded_len": len(decoded),
    }


# --------------------------------------------------------------------------- #
# Round trip of the MOBI writer
# --------------------------------------------------------------------------- #

def test_codec():
    print("mobi byte codec")
    cases = {
        "ascii": b"Hello world, repeated text. " * 40,
        "cjk": "中文测试内容重复 abc ".encode() * 30,
        "all_bytes": bytes(range(256)) * 2,
        "emoji": "\U0001F600\U0001F4DA test".encode() * 20,
        "empty": b"",
    }
    for name, data in cases.items():
        from pdf2mobi_android.writers import _escape_record
        check(decode_record(_escape_record(data)) == data, f"roundtrip {name}")


# --------------------------------------------------------------------------- #
# Full pipeline against real PDFs
# --------------------------------------------------------------------------- #

def test_pipeline(samples: Path, outdir: Path) -> None:
    for name in ("text.pdf", "hybrid.pdf", "scanned_distinct.pdf"):
        src = samples / name
        if not src.exists():
            continue
        print(f"pipeline: {name}")
        analysis = analyse_pdf(src)
        check(analysis.kind is not PdfKind.ERROR, f"{name} readable")

        book = build_book(src, analysis, BuildOptions())
        dest = outdir / (src.stem + ".mobi")
        write_mobi(book, dest)
        text, info = read_mobi_text(dest)

        check(info["rec0"][16:20] == b"MOBI", f"{name} MOBI magic")
        check(info["rec0"][16 + 232:16 + 232 + 4] == b"EXTH", f"{name} EXTH located")
        check(info["decoded_len"] == info["claimed_len"], f"{name} text length exact")
        check(text.startswith("<html"), f"{name} html output")
        check(info["offsets"] == sorted(info["offsets"]), f"{name} offsets ordered")
        check(info["offsets"][0] == 78 + info["records"] * 8 + 2,
              f"{name} first offset correct")

        epub = outdir / (src.stem + ".epub")
        write_epub(book, epub)
        with zipfile.ZipFile(epub) as z:
            check(z.testzip() is None, f"{name} epub intact")
            check(z.namelist()[0] == "mimetype", f"{name} mimetype first")
            bad = []
            for n in z.namelist():
                if n.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                    try:
                        ET.fromstring(z.read(n))
                    except ET.ParseError as exc:
                        bad.append(str(exc))
            check(not bad, f"{name} epub XML valid")


def test_chinese(samples: Path, outdir: Path) -> None:
    src = samples / "edge" / "chinese.pdf"
    if not src.exists():
        return
    print("chinese pdf")
    analysis = analyse_pdf(src)
    check(analysis.kind is PdfKind.TEXT, "chinese.pdf classified as text")
    book = build_book(src, analysis, BuildOptions())
    joined = "\n".join(s.text for s in book.sections)
    check("第一章" in joined, "chinese heading recovered")
    check("这是一段中文测试正文" in joined, "chinese body recovered")
    check("\ufffd" not in joined, "no replacement characters")
    check(all(t.page > 0 for t in analysis.toc), "toc pages resolved")

    dest = outdir / "chinese.mobi"
    write_mobi(book, dest)
    text, info = read_mobi_text(dest)
    check(info["decoded_len"] == info["claimed_len"], "chinese length exact")
    check("第一章" in text, "chinese survives the mobi byte round trip")


def test_ocr_hook(samples: Path, outdir: Path) -> None:
    src = samples / "scanned_distinct.pdf"
    if not src.exists():
        return
    print("ocr hook plumbing")
    analysis = analyse_pdf(src)
    check(analysis.kind is PdfKind.SCANNED, "scanned detected")

    plain = build_book(src, analysis, BuildOptions())
    check(len(plain.sections) == 0, "no text and no OCR -> nothing extracted")

    calls = []

    def fake_ocr(page_no, _page):
        calls.append(page_no)
        return f"第 {page_no} 页识别文本。\n\n第二段。"

    book = build_book(src, analysis, BuildOptions(ocr_page=fake_ocr))
    check(len(calls) == analysis.page_count, f"OCR called for every page ({len(calls)})")
    check(len(book.sections) == analysis.page_count, "OCR text became sections")
    check(all(s.from_ocr for s in book.sections), "sections flagged as OCR")
    check("第 1 页识别文本" in book.sections[0].text, "OCR text preserved")

    dest = outdir / "scanned_ocr.mobi"
    write_mobi(book, dest)
    text, info = read_mobi_text(dest)
    check(info["decoded_len"] == info["claimed_len"], "ocr mobi length exact")
    check("识别文本" in text, "ocr text survives the mo biome round trip")


def test_ocr_unavailable_gracefully(samples: Path) -> None:
    print("ocr unavailable handling")
    from pdf2mobi_android.ocr import ocr_status
    status = ocr_status()
    # On desktop this must report unavailable rather than raising.
    check(isinstance(status.available, bool), "status query does not raise")
    print(f"       -> available={status.available} reason={status.reason!r}")


def main() -> int:
    print(f"pdf2mobi android self test (python {sys.version.split()[0]})\n")
    test_encoding()
    test_codec()

    samples = _HERE.parent / "samples"
    if not samples.exists():
        samples = Path.cwd() / "samples"
    outdir = _HERE / "test-output"
    outdir.mkdir(exist_ok=True)

    if samples.exists():
        test_pipeline(samples, outdir)
        test_chinese(samples, outdir)
        test_ocr_hook(samples, outdir)
    else:
        print(f"note: samples not found at {samples}, skipping PDF pipeline tests")
    test_ocr_unavailable_gracefully(samples)

    print()
    print(f"{_pass} passed, {len(_fail)} failed")
    for f in _fail:
        print(f"  FAILED: {f}")
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
