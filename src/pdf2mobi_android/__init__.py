"""pdf2mobi for Android — text-first PDF to MOBI/EPUB conversion.

The Android build differs from the desktop one in three deliberate ways:

1. **Pure Python only.** PyMuPDF has no Android build, so text extraction uses
   ``pypdf``. That also means no native code to cross-compile.
2. **Text first, images dropped.** Scanned pages are not embedded as bitmaps,
   which keeps the output small — the requested behaviour for a phone.
3. **OCR via Android's ML Kit** rather than Tesseract, which has no
   python-for-android recipe.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
