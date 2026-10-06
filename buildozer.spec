[app]

# ---------------------------------------------------------------------------
# Basic application metadata
# ---------------------------------------------------------------------------
title = PDF 转 MOBI
package.name = pdf2mobi
package.domain = org.pdf2mobi

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,ttf
source.include_patterns = src/pdf2mobi_android/*.py

version = 1.0.0

# ---------------------------------------------------------------------------
# Requirements
#
# Everything here must be either pure Python or have a python-for-android
# recipe. Notably ABSENT:
#   * pymupdf / fitz     - no Android build exists; text extraction uses pypdf
#   * pdfminer.six       - depends on `cryptography`, which has no p4a recipe
#   * charset-normalizer - a leftover pdfminer dependency that nothing imports;
#                          it publishes no Android wheel, so shipping it makes
#                          pip fail with "not a supported wheel on this platform"
#   * tesseract          - no p4a recipe; OCR goes through Android's ML Kit
# ---------------------------------------------------------------------------
requirements = python3,kivy,pypdf,android

# Use the official Kivy bootstrap.
orientation = portrait
fullscreen = 0

# ---------------------------------------------------------------------------
# Android specific
# ---------------------------------------------------------------------------
android.api = 34
android.minapi = 24
# 28c is the version python-for-android currently recommends
# (see RECOMMENDED_NDK_VERSION in pythonforandroid/recommendations.py).
# Buildozer downloads it automatically on the first build; the CI cache keeps
# it available afterwards.
android.ndk = 28c
android.archs = arm64-v8a
android.allow_backup = True

# ML Kit text recognition (Chinese) plus the base recogniser. These are fetched
# by Gradle as AARs and add only a few hundred KB to the APK, because the actual
# language models are downloaded on demand by Google Play Services.
android.gradle_dependencies = com.google.mlkit:text-recognition-chinese:16.0.1,com.google.mlkit:text-recognition:16.0.1

# The Java source for the OCR bridge lives in android-src/ (see OcrBridge.java).
android.add_src = android-src

# REQUIRED. Without this buildozer refuses to answer sdkmanager's
# "Accept? (y/N):" prompt, so platform-tools and build-tools are skipped and
# the build then dies with "Aidl not found, please install it."
# When true, buildozer watches for the prompt and sends "y".
android.accept_sdk_license = True

android.permissions = READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,READ_MEDIA_IMAGES
android.enable_androidx = True

[buildozer]
log_level = 2
warn_on_root = 1
