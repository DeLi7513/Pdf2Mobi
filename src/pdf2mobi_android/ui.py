"""Kivy user interface for the Android build.

Design notes
------------

* **Phone first.** Large touch targets, a scrollable file list, and no hover-only
  interactions. The layout is a single column so it works in portrait.
* **Nothing blocks the UI.** Conversion runs on a worker thread and communicates
  through a queue polled by a Kivy ``Clock`` schedule; the UI thread never waits
  on I/O.
* **Files come from Android's picker**, with a manual path box as a fallback for
  desktop testing and for content the picker cannot expose.
* **Everything is reported.** Each file shows its detected kind, per-file status
  and the final summary; a log pane carries details.
"""

from __future__ import annotations

import os
import queue
import threading
import traceback
from pathlib import Path
from typing import Dict, List, Optional

from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.properties import BooleanProperty, NumericProperty, StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput

from .pdftext import PdfKind, analyse_pdf
from .builder import BuildOptions, build_book
from .writers import write_epub, write_mobi

APP_TITLE = "PDF 转 MOBI"
FORMATS = ["mobi", "epub", "txt"]

KIND_LABEL = {
    PdfKind.TEXT: "文字版",
    PdfKind.SCANNED: "扫描版",
    PdfKind.HYBRID: "混合版",
    PdfKind.EMPTY: "空白",
    PdfKind.ERROR: "无法读取",
    PdfKind.ENCRYPTED: "已加密",
}


def default_output_dir() -> Path:
    """A sensible writable output folder on Android and on a desktop."""
    for candidate in (
        os.environ.get("EXTERNAL_STORAGE"),
        os.environ.get("ANDROID_PRIVATE"),
        str(Path.home()),
    ):
        if candidate:
            p = Path(candidate)
            if p.exists() and os.access(p, os.W_OK):
                out = p / "pdf2mobi"
                try:
                    out.mkdir(parents=True, exist_ok=True)
                    return out
                except Exception:
                    continue
    return Path.cwd()


class FileRow(BoxLayout):
    """One line in the file list."""

    def __init__(self, path: Path, on_remove=None, **kw):
        super().__init__(size_hint_y=None, height=dp(56), spacing=dp(4), **kw)
        self.path = path
        self.kind: Optional[PdfKind] = None
        self.status = "待处理"

        self.name_label = Label(
            text=path.name, halign="left", valign="middle",
            size_hint_x=0.46, shorten=True, shorten_from="middle",
            font_size=dp(15),
        )
        self.name_label.bind(size=lambda *_: setattr(
            self.name_label, "text_size", (self.name_label.width, None)))

        self.kind_label = Label(
            text="", halign="left", valign="middle",
            size_hint_x=0.20, font_size=dp(13), color=(0.55, 0.55, 0.55, 1),
        )
        self.status_label = Label(
            text=self.status, halign="left", valign="middle",
            size_hint_x=0.24, font_size=dp(13),
        )
        self.add_widget(self.name_label)
        self.add_widget(self.kind_label)
        self.add_widget(self.status_label)

        if on_remove:
            btn = Button(text="✕", size_hint_x=None, width=dp(40),
                         background_normal="", background_color=(0.5, 0.2, 0.2, 1))
            btn.bind(on_release=lambda *_: on_remove(self))
            self.add_widget(btn)

    def set_kind(self, kind: Optional[PdfKind]) -> None:
        self.kind = kind
        self.kind_label.text = KIND_LABEL.get(kind, "") if kind else ""

    def set_status(self, status: str, colour=(1, 1, 1, 1)) -> None:
        self.status = status
        self.status_label.text = status
        self.status_label.color = colour


class Pdf2MobiApp(BoxLayout):
    """Root widget; also holds the conversion worker plumbing."""

    log_text = StringProperty("")
    status_text = StringProperty("就绪")
    progress = NumericProperty(0.0)
    busy = BooleanProperty(False)

    def __init__(self, **kw):
        super().__init__(orientation="vertical", padding=dp(8), spacing=dp(6), **kw)
        self.rows: List[FileRow] = []
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.cancel_flag = threading.Event()
        self.out_dir = default_output_dir()
        self._job: dict = {}

        self._build_ui()
        Clock.schedule_interval(self._drain_queue, 0.1)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        # Title
        self.add_widget(Label(
            text=APP_TITLE, size_hint_y=None, height=dp(34),
            bold=True, font_size=dp(19),
        ))

        # Action buttons
        actions = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
        for text, cb, colour in (
            ("添加 PDF", self.pick_files, (0.15, 0.45, 0.75, 1)),
            ("选择文件夹", self.pick_folder, (0.15, 0.45, 0.75, 1)),
            ("分析类型", self.analyse_all, (0.35, 0.35, 0.4, 1)),
            ("清空", self.clear_all, (0.45, 0.25, 0.25, 1)),
        ):
            b = Button(text=text, background_normal="",
                       background_color=colour, font_size=dp(14))
            b.bind(on_release=lambda _b, f=cb: f())
            actions.add_widget(b)
        self.add_widget(actions)

        # Options row
        opts = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
        opts.add_widget(Label(text="格式", size_hint_x=0.2, font_size=dp(14)))
        self.fmt = Spinner(text="mobi", values=FORMATS, size_hint_x=0.3,
                           font_size=dp(14))
        opts.add_widget(self.fmt)
        self.ocr_btn = Button(text="OCR: 自动", size_hint_x=0.5,
                              background_normal="",
                              background_color=(0.3, 0.4, 0.3, 1), font_size=dp(13))
        self.ocr_btn.bind(on_release=self.toggle_ocr)
        opts.add_widget(self.ocr_btn)
        self.add_widget(opts)
        self.use_ocr = True

        # Output folder
        out = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(6))
        out.add_widget(Label(text="输出到", size_hint_x=0.2, font_size=dp(13)))
        self.out_label = Label(text=str(self.out_dir), size_hint_x=0.6,
                               font_size=dp(12), shorten=True,
                               shorten_from="middle", halign="left")
        self.out_label.bind(size=lambda *_: setattr(
            self.out_label, "text_size", (self.out_label.width, None)))
        out.add_widget(self.out_label)
        b = Button(text="更改", size_hint_x=0.2, font_size=dp(13),
                   background_normal="", background_color=(0.35, 0.35, 0.4, 1))
        b.bind(on_release=self.pick_outdir)
        out.add_widget(b)
        self.add_widget(out)

        # File list
        self.list_holder = BoxLayout(orientation="vertical", size_hint_y=0.42)
        self.scroll = ScrollView()
        self.list_box = BoxLayout(orientation="vertical", size_hint_y=None,
                                  spacing=dp(2))
        self.list_box.bind(minimum_height=self.list_box.setter("height"))
        self.scroll.add_widget(self.list_box)
        self.list_holder.add_widget(self.scroll)
        self.add_widget(self.list_holder)

        # Log
        log_scroll = ScrollView(size_hint_y=0.26)
        self.log_label = Label(text="", size_hint_y=None, halign="left",
                               valign="top", font_size=dp(12),
                               color=(0.85, 0.85, 0.85, 1))
        self.log_label.bind(
            width=lambda *_: setattr(self.log_label, "text_size",
                                     (self.log_label.width, None)),
            texture_size=lambda *_: setattr(self.log_label, "height",
                                            self.log_label.texture_size[1]),
        )
        log_scroll.add_widget(self.log_label)
        self.add_widget(log_scroll)

        # Progress + start
        bottom = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(6))
        self.status_lbl = Label(text=self.status_text, size_hint_x=0.4,
                                font_size=dp(13), halign="left")
        self.status_lbl.bind(size=lambda *_: setattr(
            self.status_lbl, "text_size", (self.status_lbl.width, None)))
        bottom.add_widget(self.status_lbl)
        self.start_btn = Button(text="开始转换", size_hint_x=0.6,
                                background_normal="",
                                background_color=(0.13, 0.55, 0.25, 1),
                                font_size=dp(16), bold=True)
        self.start_btn.bind(on_release=self.start)
        bottom.add_widget(self.start_btn)
        self.add_widget(bottom)

        # Thin progress bar showing overall completion.
        try:
            from kivy.uix.progressbar import ProgressBar

            self.progress_bar = ProgressBar(max=1.0, value=0.0,
                                            size_hint_y=None, height=dp(6))
            self.add_widget(self.progress_bar)
            self.bind(progress=lambda *_: setattr(
                self.progress_bar, "value", self.progress))
        except Exception:
            self.progress_bar = None

    # ------------------------------------------------------------- logging

    def log(self, message: str) -> None:
        self.queue.put(("log", message))

    def _append_log(self, message: str) -> None:
        lines = (self.log_text + "\n" + message).strip().splitlines()
        # Keep the buffer bounded so a long batch cannot exhaust memory.
        if len(lines) > 300:
            lines = lines[-300:]
        self.log_text = "\n".join(lines)
        self.log_label.text = self.log_text

    # --------------------------------------------------------------- files

    def pick_files(self) -> None:
        """Open Android's document picker when available, else a file browser."""
        paths = self._android_pick(multiple=True)
        if paths:
            self._add_paths([Path(p) for p in paths])
            return
        if paths is not None:
            return  # user cancelled
        self._open_file_chooser(multiple=True)

    def pick_folder(self) -> None:
        self._open_file_chooser(multiple=False, directory=True)

    def _android_pick(self, multiple: bool):
        """Use the native picker on Android; return None when unavailable."""
        if not (os.environ.get("ANDROID_ARGUMENT") or os.environ.get("ANDROID_PRIVATE")):
            return None
        try:
            from jnius import autoclass  # type: ignore

            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            activity = PythonActivity.mActivity
            Intent = autoclass("android.content.Intent")
            intent = Intent(Intent.ACTION_OPEN_DOCUMENT)
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            intent.setType("application/pdf")
            if multiple:
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, True)
            activity.startActivityForResult(intent, 0x5010)
            self.log("已打开系统文件选择器，请选择 PDF")
        except Exception as exc:
            self.log(f"系统选择器不可用（{exc}），改用内置浏览")
            return None
        return []

    def _open_file_chooser(self, multiple: bool, directory: bool = False) -> None:
        """Fallback picker built from Kivy widgets."""
        chooser = FileChooserListView(
            path=str(Path.home()),
            filters=["*.pdf"] if not directory else [],
            dirselect=directory,
        )
        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(6))
        box.add_widget(chooser)
        buttons = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
        popup = Popup(title="选择" + ("文件夹" if directory else "PDF 文件"),
                      content=box, size_hint=(0.95, 0.95))

        def confirm(*_):
            if directory:
                chosen = [chooser.path]
            else:
                chosen = list(chooser.selection)
                if multiple and not chosen and chooser.path:
                    chosen = []
            popup.dismiss()
            if directory:
                self._add_folder(Path(chosen[0]))
            else:
                self._add_paths([Path(p) for p in chosen])

        ok = Button(text="确定", background_normal="",
                    background_color=(0.13, 0.55, 0.25, 1))
        ok.bind(on_release=confirm)
        cancel = Button(text="取消", background_normal="",
                        background_color=(0.4, 0.4, 0.4, 1))
        cancel.bind(on_release=lambda *_: popup.dismiss())
        buttons.add_widget(ok)
        buttons.add_widget(cancel)
        box.add_widget(buttons)
        popup.open()

    def _add_folder(self, folder: Path) -> None:
        found = sorted(
            (p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"),
            key=lambda p: str(p).lower(),
        )
        if not found:
            self.log(f"{folder} 里没有找到 PDF")
            return
        self._add_paths(found)

    def _add_paths(self, paths) -> None:
        existing = {r.path.resolve() for r in self.rows if r.path.exists()}
        added = 0
        for p in paths:
            try:
                key = p.resolve()
            except Exception:
                key = p
            if key in existing:
                continue
            row = FileRow(p, on_remove=self._remove_row)
            self.list_box.add_widget(row)
            self.rows.append(row)
            existing.add(key)
            added += 1
        if added:
            self.log(f"已添加 {added} 个文件（共 {len(self.rows)} 个）")

    def _remove_row(self, row: FileRow) -> None:
        if row in self.rows:
            self.rows.remove(row)
            self.list_box.remove_widget(row)

    def clear_all(self) -> None:
        for row in list(self.rows):
            self._remove_row(row)
        self.log_text = ""
        self.log_label.text = ""
        self.log("列表已清空")

    # -------------------------------------------------------------- options

    def toggle_ocr(self) -> None:
        self.use_ocr = not self.use_ocr
        if self.use_ocr:
            self.ocr_btn.text = "OCR: 自动"
            self.ocr_btn.background_color = (0.3, 0.4, 0.3, 1)
        else:
            self.ocr_btn.text = "OCR: 关闭"
            self.ocr_btn.background_color = (0.4, 0.3, 0.3, 1)
        self.log("OCR 已" + ("开启" if self.use_ocr else "关闭"))

    def pick_outdir(self) -> None:
        chooser = FileChooserListView(path=str(self.out_dir), dirselect=True)
        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(6))
        box.add_widget(chooser)
        buttons = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
        popup = Popup(title="选择输出文件夹", content=box, size_hint=(0.95, 0.9))

        def confirm(*_):
            self.out_dir = Path(chooser.path)
            self.out_label.text = str(self.out_dir)
            popup.dismiss()
            self.log(f"输出目录：{self.out_dir}")

        ok = Button(text="确定", background_normal="",
                    background_color=(0.13, 0.55, 0.25, 1))
        ok.bind(on_release=confirm)
        cancel = Button(text="取消", background_normal="",
                        background_color=(0.4, 0.4, 0.4, 1))
        cancel.bind(on_release=lambda *_: popup.dismiss())
        buttons.add_widget(ok)
        buttons.add_widget(cancel)
        box.add_widget(buttons)
        popup.open()

    # ------------------------------------------------------------ analysis

    def analyse_all(self) -> None:
        if self.busy or not self.rows:
            if not self.rows:
                self.log("请先添加 PDF 文件")
            return
        self._begin()
        self.worker = threading.Thread(target=self._run_analysis, daemon=True)
        self.worker.start()

    def _run_analysis(self) -> None:
        try:
            total = len(self.rows)
            for i, row in enumerate(list(self.rows), 1):
                if self.cancel_flag.is_set():
                    break
                self.queue.put(("status", f"分析中 {i}/{total}"))
                a = analyse_pdf(row.path)
                note = ""
                if a.kind is PdfKind.SCANNED:
                    note = "（需要 OCR 才有文字）"
                elif a.kind is PdfKind.HYBRID:
                    note = "（部分页面需要 OCR）"
                self.queue.put(("row", row, "已分析", a.kind, ""))
                self.queue.put(("log", f"{row.path.name}：{a.summary()}{note}"))
                self.queue.put(("progress", i / total))
        except Exception as exc:
            self.queue.put(("log", f"分析出错：{exc}"))
        finally:
            self.queue.put(("done", "分析完成"))

    # ---------------------------------------------------------- conversion

    def start(self) -> None:
        if self.busy:
            self.cancel_flag.set()
            self.status_text = "正在停止…"
            self.status_lbl.text = self.status_text
            return
        if not self.rows:
            self.log("请先添加 PDF 文件")
            return

        # Freeze the job so the worker never touches widgets.
        self._job = {
            "format": self.fmt.text,
            "outdir": self.out_dir,
            "use_ocr": self.use_ocr,
            "rows": list(self.rows),
        }
        self._begin()
        self.worker = threading.Thread(target=self._run_batch, daemon=True)
        self.worker.start()

    def _begin(self) -> None:
        self.busy = True
        self.cancel_flag.clear()
        self.progress = 0.0
        self.start_btn.text = "停止"
        self.start_btn.background_color = (0.6, 0.25, 0.2, 1)
        for row in self.rows:
            row.set_status("排队中")

    def _finish(self, summary: str) -> None:
        self.busy = False
        self.start_btn.text = "开始转换"
        self.start_btn.background_color = (0.13, 0.55, 0.25, 1)
        self.status_text = summary
        self.status_lbl.text = summary

    def _run_batch(self) -> None:
        job = self._job
        fmt = job["format"]
        outdir: Path = job["outdir"]
        rows: List[FileRow] = job["rows"]
        use_ocr = job["use_ocr"]
        total = len(rows)
        ok = failed = skipped = 0

        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.queue.put(("log", f"无法创建输出目录 {outdir}：{exc}"))
            self.queue.put(("done", "失败"))
            return

        for i, row in enumerate(rows, 1):
            if self.cancel_flag.is_set():
                self.queue.put(("log", "已停止"))
                break
            self.queue.put(("status", f"转换中 {i}/{total}"))
            self.queue.put(("row", row, "分析中", None, ""))

            try:
                analysis = analyse_pdf(row.path)
                self.queue.put(("row", row, "转换中", analysis.kind, ""))
                self.queue.put(("log", f"[{i}/{total}] {row.path.name} — {analysis.summary()}"))

                if analysis.kind is PdfKind.ERROR:
                    raise RuntimeError(analysis.error or "无法读取")
                if analysis.kind is PdfKind.ENCRYPTED:
                    raise RuntimeError("文件已加密，需要密码")

                # Build the OCR hook only when it could be needed.
                ocr_hook = None
                if use_ocr and analysis.needs_ocr:
                    from .ocr import make_ocr_hook

                    ocr_hook = make_ocr_hook(
                        row.path, dpi=200, log=lambda m: self.queue.put(("log", m))
                    )
                    if ocr_hook is None:
                        self.queue.put((
                            "log",
                            "  OCR 不可用，本文件的图片页将被跳过"
                            if analysis.kind is PdfKind.SCANNED else
                            "  OCR 不可用，扫描页将被跳过",
                        ))

                book = build_book(
                    row.path, analysis,
                    BuildOptions(ocr_page=ocr_hook, keep_toc=True),
                    progress=lambda c, t, _sid: self.queue.put(("progress", i - 1 + c / max(t, 1))),
                )

                if not book.sections:
                    skipped += 1
                    self.queue.put(("row", row, "无文字可提取", analysis.kind, ""))
                    self.queue.put(("log", "  没有可提取的文字，已跳过"))
                    self.queue.put(("progress", i / total))
                    continue

                dest = outdir / f"{row.path.stem}.{fmt}"
                if fmt == "epub":
                    write_epub(book, dest)
                elif fmt == "mobi":
                    write_mobi(book, dest)
                else:
                    dest.write_text(book.plain_text(), encoding="utf-8")

                size_kb = dest.stat().st_size / 1024
                ok += 1
                self.queue.put(("row", row, "完成", analysis.kind, str(dest)))
                self.queue.put(("log", f"  完成 -> {dest.name}（{size_kb:,.0f} KB，"
                                       f"{len(book.sections)} 节）"))
            except Exception as exc:
                failed += 1
                self.queue.put(("row", row, "失败", None, ""))
                self.queue.put(("log", f"  失败：{exc}"))
                if os.environ.get("PDF2MOBI_DEBUG"):
                    self.queue.put(("log", traceback.format_exc()))
            finally:
                self.queue.put(("progress", i / total))

        parts = [f"成功 {ok}"]
        if failed:
            parts.append(f"失败 {failed}")
        if skipped:
            parts.append(f"跳过 {skipped}")
        self.queue.put(("log", "转换结束：" + "，".join(parts)))
        self.queue.put(("done", "，".join(parts)))

    # ---------------------------------------------------------- queue pump

    def _drain_queue(self, _dt) -> None:
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1])
                elif kind == "status":
                    self.status_text = item[1]
                    self.status_lbl.text = item[1]
                elif kind == "progress":
                    self.progress = max(0.0, min(1.0, float(item[1])))
                elif kind == "row":
                    row, status, pdf_kind, _out = item[1], item[2], item[3], item[4]
                    colour = (1, 1, 1, 1)
                    if status == "完成":
                        colour = (0.4, 0.9, 0.5, 1)
                    elif status in ("失败", "无文字可提取"):
                        colour = (0.95, 0.5, 0.45, 1)
                    row.set_status(status, colour)
                    if pdf_kind is not None:
                        row.set_kind(pdf_kind)
                elif kind == "done":
                    self._finish(item[1])
        except queue.Empty:
            pass
        return True


def run() -> None:
    """Entry point used by main.py."""
    from kivy.app import App

    class _App(App):
        title = APP_TITLE

        def build(self):
            Window.softinput_mode = "below_target"
            return Pdf2MobiApp()

    _App().run()
