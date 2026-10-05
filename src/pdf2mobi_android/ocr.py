"""OCR for scanned PDF pages, using Android's native ML Kit.

Why ML Kit rather than Tesseract
--------------------------------

Tesseract has no python-for-android recipe. Bundling it would mean writing a
custom recipe, compiling Leptonica and Tesseract for each ABI, and shipping
roughly 30 MB of extra libraries plus language data. Google's ML Kit is already
present on Android through Play Services, recognises Chinese well, adds almost
nothing to the APK, and is reached with a few lines of Java. That makes it the
far more robust choice for a phone build.

How this module behaves
-----------------------

* **On Android** with ML Kit available, it renders the page to a bitmap with
  PyMuPDF-free means (the Android canvas via `android.graphics`), hands it to the
  text recogniser and returns the recognised string.
* **Anywhere else**, or if ML Kit is missing, every function degrades to a no-op
  that reports "unavailable". The conversion then simply skips image-only pages
  instead of failing, which is the correct behaviour for a text-focused build.

Rendering note: pdf rendering on Android without PyMuPDF is done through
Android's own `PdfRenderer`, which turns a PDF page straight into a Bitmap. That
is part of the platform, so no extra dependency is needed.
"""

from __future__ import annotations

import io
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional


@dataclass
class OcrStatus:
    available: bool
    reason: str = ""
    engine: str = "none"


_JPYNIUS_ERROR = ""


def _load_java() -> Optional[object]:
    """Import pyjnius if this really is an Android build."""
    global _JPYNIUS_ERROR
    try:
        from jnius import autoclass  # type: ignore

        return autoclass
    except Exception as exc:  # desktop, or pyjnius missing
        _JPYNIUS_ERROR = str(exc)
        return None


def is_android() -> bool:
    """True when running inside the Android app rather than on a desktop."""
    if os.environ.get("ANDROID_ARGUMENT") or os.environ.get("ANDROID_PRIVATE"):
        return True
    return "android" in sys.platform.lower()


def ocr_status() -> OcrStatus:
    """Report whether OCR can run, for display in the UI."""
    if not is_android():
        return OcrStatus(False, "当前不在安卓环境，OCR 不可用（桌面版请用带 OCR 的版本）")
    autoclass = _load_java()
    if autoclass is None:
        return OcrStatus(False, f"pyjnius 不可用：{_JPYNIUS_ERROR}")
    try:
        autoclass("com.google.mlkit.vision.text.TextRecognition")
    except Exception:
        # The class is loaded through our Java shim instead of directly.
        try:
            autoclass("org.pdf2mobi.OcrBridge")
        except Exception as exc:
            return OcrStatus(False, f"未集成 ML Kit OCR：{exc}")
    return OcrStatus(True, "", "ML Kit")


class MlKitOcr:
    """Render PDF pages with Android's PdfRenderer and recognise their text."""

    def __init__(self) -> None:
        self._autoclass = _load_java()
        self._bridge = None
        self._renderer = None
        self._parcel = None
        self.status = ocr_status()

    def open(self, pdf_path: Path) -> None:
        """Prepare Android's PdfRenderer for ``pdf_path``."""
        if not self.status.available or self._autoclass is None:
            raise RuntimeError(self.status.reason or "OCR 不可用")
        autoclass = self._autoclass
        ParcelFileDescriptor = autoclass("android.os.ParcelFileDescriptor")
        PdfRenderer = autoclass("android.graphics.pdf.PdfRenderer")
        self._parcel = ParcelFileDescriptor.open(
            autoclass("java.io.File")(str(pdf_path)),
            ParcelFileDescriptor.MODE_READ_ONLY,
        )
        self._renderer = PdfRenderer(self._parcel)
        self._bridge = autoclass("org.pdf2mobi.OcrBridge")
        self._Bitmap = autoclass("android.graphics.Bitmap")

    @property
    def page_count(self) -> int:
        return int(self._renderer.getPageCount()) if self._renderer else 0

    def page_text(self, index: int, dpi: int = 200) -> str:
        """Render page ``index`` (0 based) and return its recognised text."""
        if self._renderer is None or self._bridge is None:
            return ""
        page = None
        try:
            page = self._renderer.openPage(index)
            # Scale so the bitmap is roughly `dpi` dots per inch.
            scale = max(1.0, dpi / 72.0)
            width = int(page.getWidth() * scale)
            height = int(page.getHeight() * scale)
            bitmap = self._Bitmap.createBitmap(
                width, height, self._Bitmap.Config.ARGB_8888
            )
            # White background: ML Kit reads dark text on light far better.
            bitmap.eraseColor(-1)
            page.render(bitmap, None, None, 1)  # 1 = RENDER_MODE_FOR_DISPLAY
            return str(self._bridge.recognize(bitmap) or "")
        except Exception:
            return ""
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def close(self) -> None:
        for attr in ("_renderer", "_parcel"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
            setattr(self, attr, None)


def make_ocr_hook(pdf_path: Path, dpi: int = 200, log: Optional[Callable[[str], None]] = None):
    """Return a ``build_book`` compatible OCR callback, or None.

    The callback signature is ``(page_number_1_based, pypdf_page) -> str``; the
    pypdf page is ignored because rendering goes through Android directly.
    """
    status = ocr_status()
    if not status.available:
        if log:
            log(f"OCR 不可用：{status.reason}")
        return None

    engine = MlKitOcr()
    try:
        engine.open(pdf_path)
    except Exception as exc:
        if log:
            log(f"OCR 初始化失败：{exc}")
        return None

    if log:
        log(f"OCR 已就绪（{status.engine}），共 {engine.page_count} 页")

    def hook(page_no: int, _page) -> str:
        try:
            text = engine.page_text(page_no - 1, dpi=dpi)
        except Exception:
            return ""
        if log and text:
            log(f"  第 {page_no} 页 OCR 得到 {len(text)} 字")
        return text

    return hook
