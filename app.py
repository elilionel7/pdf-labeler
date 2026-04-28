"""
PDF Labeler -- simple Tkinter desktop app.

Usage:  python3 app.py

Workflow:
  1. Open Settings (top right), pick Gemini / Claude / Tesseract, paste API key.
  2. Click "Add PDFs" or "Add Folder".
  3. Files are analysed in the background; suggestions appear in the table.
  4. Double-click any cell in Name / Type / Dates to edit.
  5. Click "Apply Rename" -- files are renamed in place.
"""

from __future__ import annotations

import os
import re
import threading
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

import ai_reader
from extractor import analyze as tesseract_analyze
from formatter import VALID_TYPES, FileLabel, build_filename, sanitize
from ocr import extract_text as tesseract_extract_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DATE_RE = re.compile(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})\b")


def dates_to_string(dates: List[date]) -> str:
    return ", ".join(f"{d.month}/{d.day}/{d.year % 100:02d}" for d in dates)


def parse_dates_field(raw: str) -> List[date]:
    out: List[date] = []
    for m in DATE_RE.finditer(raw):
        mo, dy, yr = (int(x) for x in m.groups())
        if yr < 100:
            yr += 2000
        try:
            out.append(date(yr, mo, dy))
        except ValueError:
            pass
    return out


# ---------------------------------------------------------------------------
# Per-row data
# ---------------------------------------------------------------------------

class Row:
    __slots__ = ("path", "name", "ftype", "dates", "status", "iid")

    def __init__(self, path: str):
        self.path: str = path
        self.name:  str = ""
        self.ftype: str = ""
        self.dates: List[date] = []
        self.status: str = "pending"
        self.iid:    str = ""

    def proposed_filename(self) -> Optional[str]:
        if not (self.name and self.ftype and self.dates):
            return None
        try:
            return sanitize(build_filename(self.name, self.ftype, self.dates,
                                           extension=Path(self.path).suffix or ".pdf"))
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, settings: Dict):
        super().__init__(parent)
        self.title("Settings")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result: Optional[Dict] = None
        self._settings = dict(settings)

        pad = {"padx": 10, "pady": 4}

        ttk.Label(self, text="Reader:").grid(row=0, column=0, sticky="w", **pad)
        self.provider_var = tk.StringVar(value=self._settings.get("provider", "gemini"))
        provider_frame = ttk.Frame(self)
        provider_frame.grid(row=0, column=1, sticky="w", **pad)
        for val, label in [
            ("gemini",    "Google Gemini  (free tier — recommended)"),
            ("claude",    "Anthropic Claude  (paid)"),
            ("tesseract", "Tesseract  (offline; printed text only)"),
        ]:
            ttk.Radiobutton(provider_frame, text=label, value=val,
                            variable=self.provider_var,
                            command=self._sync_enabled).pack(anchor="w")

        # Gemini fields
        ttk.Label(self, text="Gemini API key:").grid(row=1, column=0, sticky="w", **pad)
        self.gemini_key = tk.StringVar(value=self._settings.get("gemini_api_key", ""))
        self.gemini_entry = ttk.Entry(self, textvariable=self.gemini_key, width=54, show="*")
        self.gemini_entry.grid(row=1, column=1, sticky="we", **pad)

        ttk.Label(self, text="Gemini model:").grid(row=2, column=0, sticky="w", **pad)
        self.gemini_model = tk.StringVar(value=self._settings.get("gemini_model", "gemini-2.5-flash"))
        self.gemini_model_entry = ttk.Entry(self, textvariable=self.gemini_model, width=30)
        self.gemini_model_entry.grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(self, text="  (Free key: aistudio.google.com/apikey)",
                  foreground="#666").grid(row=3, column=1, sticky="w", padx=10)

        # Claude fields
        ttk.Separator(self, orient="horizontal").grid(row=4, column=0, columnspan=2,
                                                     sticky="we", pady=8)

        ttk.Label(self, text="Claude API key:").grid(row=5, column=0, sticky="w", **pad)
        self.claude_key = tk.StringVar(value=self._settings.get("claude_api_key", ""))
        self.claude_entry = ttk.Entry(self, textvariable=self.claude_key, width=54, show="*")
        self.claude_entry.grid(row=5, column=1, sticky="we", **pad)

        ttk.Label(self, text="Claude model:").grid(row=6, column=0, sticky="w", **pad)
        self.claude_model = tk.StringVar(value=self._settings.get("claude_model", "claude-haiku-4-5"))
        self.claude_model_entry = ttk.Entry(self, textvariable=self.claude_model, width=30)
        self.claude_model_entry.grid(row=6, column=1, sticky="w", **pad)

        # Buttons
        btns = ttk.Frame(self)
        btns.grid(row=7, column=0, columnspan=2, sticky="e", padx=10, pady=10)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="Save", command=self._save).pack(side="right")

        self._sync_enabled()
        self.wait_window()

    def _sync_enabled(self) -> None:
        p = self.provider_var.get()
        def state(flag): return "normal" if flag else "disabled"
        for w in (self.gemini_entry, self.gemini_model_entry):
            w.configure(state=state(p == "gemini"))
        for w in (self.claude_entry, self.claude_model_entry):
            w.configure(state=state(p == "claude"))

    def _save(self) -> None:
        self.result = {
            "provider":       self.provider_var.get(),
            "gemini_api_key": self.gemini_key.get().strip(),
            "gemini_model":   self.gemini_model.get().strip() or "gemini-2.5-flash",
            "claude_api_key": self.claude_key.get().strip(),
            "claude_model":   self.claude_model.get().strip() or "claude-haiku-4-5",
        }
        self.destroy()


# ---------------------------------------------------------------------------
# Names dialog
# ---------------------------------------------------------------------------

class NamesDialog(tk.Toplevel):
    """Edit the list of known names used to guide handwriting recognition."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Known Names")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()
        self.geometry("420x420")

        ttk.Label(self, text=(
            "One name per line.  These are sent to the AI as hints when\n"
            "reading ambiguous handwriting.  Include both clients and employees."
        ), justify="left").pack(anchor="w", padx=10, pady=(10, 4))

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        self._text = tk.Text(frame, wrap="word", font=("TkFixedFont", 12))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self._text.yview)
        self._text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._text.pack(side="left", fill="both", expand=True)

        names = ai_reader.load_known_names()
        self._text.insert("1.0", "\n".join(names))

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="Save",   command=self._save).pack(side="right")

        self.wait_window()

    def _save(self) -> None:
        raw = self._text.get("1.0", "end")
        names = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        ai_reader.save_known_names(names)
        self.destroy()


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

class LabelerApp(tk.Tk):
    COLS = ("current", "name", "type", "dates", "proposed", "status")

    def __init__(self):
        super().__init__()
        self.title("PDF Labeler")
        self.geometry("1200x640")
        self.rows: List[Row] = []
        self.settings = ai_reader.load_settings()
        self._build_ui()
        self._update_reader_label()

    # ---- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=8)

        ttk.Button(top, text="Add PDFs",         command=self.add_files).pack(side="left")
        ttk.Button(top, text="Add Folder",       command=self.add_folder).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Clear",            command=self.clear_rows).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Re-scan Selected", command=self.rescan_selected).pack(side="left", padx=(20, 0))
        ttk.Button(top, text="Names…",           command=self.open_names).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="Settings…",        command=self.open_settings).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="Apply Rename",     command=self.apply_rename).pack(side="right")

        # Table
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        self.tree = ttk.Treeview(frame, columns=self.COLS, show="headings",
                                 selectmode="extended")
        for col, label, width in [
            ("current",  "Current filename",    260),
            ("name",     "Name",                170),
            ("type",     "Type",                70),
            ("dates",    "Dates (M/D/YY, ...)", 230),
            ("proposed", "Proposed filename",   300),
            ("status",   "Status",              90),
        ]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", self._on_double_click)

        # Status bar
        bar = ttk.Frame(self)
        bar.pack(fill="x", side="bottom")
        self.reader_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.reader_var, anchor="w",
                  foreground="#333").pack(side="left", padx=10)
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(bar, textvariable=self.status_var, anchor="e",
                  relief="sunken").pack(fill="x", expand=True)

        # Tag colours for status
        self.tree.tag_configure("ok",       background="#e7f7ea")
        self.tree.tag_configure("error",    background="#fdecec")
        self.tree.tag_configure("renamed",  background="#e6eefb")

    def _update_reader_label(self) -> None:
        provider = self.settings.get("provider", "gemini")
        configured = ai_reader.is_available(self.settings)
        if provider == "tesseract":
            text = "Reader: Tesseract (offline)"
        elif provider == "gemini":
            text = "Reader: Gemini" + ("" if configured else "  ⚠ no API key set")
        elif provider == "claude":
            text = "Reader: Claude" + ("" if configured else "  ⚠ no API key set")
        else:
            text = f"Reader: {provider}"
        self.reader_var.set(text)

    # ---- Settings --------------------------------------------------------

    def open_names(self) -> None:
        NamesDialog(self)
        self.status_var.set("Known names updated.")

    def open_settings(self) -> None:
        dlg = SettingsDialog(self, self.settings)
        if dlg.result is not None:
            self.settings.update(dlg.result)
            ai_reader.save_settings(self.settings)
            self._update_reader_label()
            self.status_var.set("Settings saved.")

    # ---- File input ------------------------------------------------------

    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Select PDFs",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        self._ingest(list(paths))

    def add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select folder containing PDFs")
        if not folder:
            return
        pdfs = [str(p) for p in Path(folder).glob("*.pdf")]
        self._ingest(pdfs)

    def _ingest(self, paths: List[str]) -> None:
        new = [p for p in paths if p and os.path.isfile(p)]
        if not new:
            return
        for p in new:
            row = Row(p)
            iid = self.tree.insert("", "end", values=(
                os.path.basename(p), "", "", "", "", "queued",
            ))
            row.iid = iid
            self.rows.append(row)
        threading.Thread(target=self._process_rows, args=(new,), daemon=True).start()

    # ---- Background processing ------------------------------------------

    def _analyze(self, pdf_path: str) -> FileLabel:
        provider = self.settings.get("provider", "gemini")
        if provider in ("gemini", "claude") and ai_reader.is_available(self.settings):
            return ai_reader.analyze_pdf(pdf_path, self.settings)
        # Tesseract fallback
        text = tesseract_extract_text(pdf_path)
        return tesseract_analyze(text, source_path=pdf_path)

    def _process_rows(self, paths: List[str]) -> None:
        path_to_row = {r.path: r for r in self.rows}
        for p in paths:
            row = path_to_row.get(p)
            if row is None:
                continue
            self._set_status(row, "reading…")
            try:
                label = self._analyze(p)
            except ImportError as e:
                self._mark_error(row, f"missing dep: {e}")
                continue
            except Exception as e:
                self._mark_error(row, f"{type(e).__name__}: {e}")
                continue
            row.name  = label.full_name
            row.ftype = label.file_type
            row.dates = label.dates
            self._refresh_row(row,
                              status="ok" if row.proposed_filename() else "incomplete")
        self._set_global_status(f"{len(self.rows)} file(s) loaded.")

    # ---- Row display -----------------------------------------------------

    def _refresh_row(self, row: Row, status: Optional[str] = None) -> None:
        proposed = row.proposed_filename() or ""
        if status is not None:
            row.status = status
        tag = ""
        if   row.status == "ok":      tag = "ok"
        elif row.status == "renamed": tag = "renamed"
        elif row.status.startswith("error"): tag = "error"

        def update():
            self.tree.item(row.iid, values=(
                os.path.basename(row.path),
                row.name, row.ftype,
                dates_to_string(row.dates),
                proposed, row.status,
            ), tags=(tag,) if tag else ())
        self.after(0, update)

    def _set_status(self, row: Row, status: str) -> None:
        row.status = status
        self._refresh_row(row)

    def _mark_error(self, row: Row, msg: str) -> None:
        row.status = f"error: {msg[:90]}"
        self._refresh_row(row)

    def _set_global_status(self, msg: str) -> None:
        self.after(0, lambda: self.status_var.set(msg))

    # ---- Editing (double-click a cell) ----------------------------------

    def _on_double_click(self, event) -> None:
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        col = self.tree.identify_column(event.x)
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        col_index = int(col.replace("#", "")) - 1
        col_name = self.COLS[col_index]
        if col_name not in ("name", "type", "dates"):
            return
        row = self._row_for_iid(iid)
        if row is None:
            return
        self._open_inline_editor(row, col_name, col_index, iid)

    def _open_inline_editor(self, row: Row, col_name: str, col_index: int, iid: str) -> None:
        bbox = self.tree.bbox(iid, column=f"#{col_index+1}")
        if not bbox:
            return
        x, y, w, h = bbox

        if col_name == "type":
            combo = ttk.Combobox(self.tree, values=sorted(VALID_TYPES), state="readonly")
            combo.set(row.ftype)
            combo.place(x=x, y=y, width=w, height=h)
            combo.focus_set()
            combo.bind("<<ComboboxSelected>>",
                       lambda _e: self._commit_edit(row, col_name, combo.get(), combo))
            combo.bind("<FocusOut>",
                       lambda _e: self._commit_edit(row, col_name, combo.get(), combo))
            return

        current = row.name if col_name == "name" else dates_to_string(row.dates)
        entry = ttk.Entry(self.tree)
        entry.insert(0, current)
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        entry.select_range(0, "end")
        entry.bind("<Return>", lambda _e: self._commit_edit(row, col_name, entry.get(), entry))
        entry.bind("<Escape>", lambda _e: entry.destroy())
        entry.bind("<FocusOut>", lambda _e: self._commit_edit(row, col_name, entry.get(), entry))

    def _commit_edit(self, row: Row, col_name: str, value: str, widget) -> None:
        widget.destroy()
        if col_name == "name":
            row.name = value.strip()
        elif col_name == "type":
            row.ftype = value.strip()
        elif col_name == "dates":
            row.dates = parse_dates_field(value)
        row.status = "ok" if row.proposed_filename() else "incomplete"
        self._refresh_row(row)

    def _row_for_iid(self, iid: str) -> Optional[Row]:
        for r in self.rows:
            if r.iid == iid:
                return r
        return None

    # ---- Actions ---------------------------------------------------------

    def clear_rows(self) -> None:
        for r in self.rows:
            self.tree.delete(r.iid)
        self.rows.clear()
        self._set_global_status("Cleared.")

    def rescan_selected(self) -> None:
        iids = self.tree.selection()
        paths = []
        for iid in iids:
            r = self._row_for_iid(iid)
            if r:
                r.name = ""
                r.ftype = ""
                r.dates = []
                r.status = "queued"
                self._refresh_row(r)
                paths.append(r.path)
        if paths:
            threading.Thread(target=self._process_rows, args=(paths,), daemon=True).start()

    def apply_rename(self) -> None:
        ready = [r for r in self.rows if r.proposed_filename() and r.status != "renamed"]
        if not ready:
            messagebox.showinfo("Nothing to do",
                                "No rows have a complete, unfinished name to rename.")
            return
        if not messagebox.askyesno("Confirm rename",
                                   f"Rename {len(ready)} file(s) on disk?"):
            return
        renamed = 0
        for r in ready:
            new_name = r.proposed_filename()
            assert new_name
            folder = os.path.dirname(r.path)
            target = self._unique_path(os.path.join(folder, new_name))
            try:
                os.rename(r.path, target)
                r.path = target
                r.status = "renamed"
                renamed += 1
            except OSError as e:
                r.status = f"error: {e}"
            self._refresh_row(r)
        self._set_global_status(f"Renamed {renamed}/{len(ready)} file(s).")

    @staticmethod
    def _unique_path(path: str) -> str:
        if not os.path.exists(path):
            return path
        stem, ext = os.path.splitext(path)
        i = 2
        while True:
            candidate = f"{stem} ({i}){ext}"
            if not os.path.exists(candidate):
                return candidate
            i += 1


def main() -> None:
    LabelerApp().mainloop()


if __name__ == "__main__":
    main()
