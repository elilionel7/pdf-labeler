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

import json
import os
import re
import threading
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional

import ai_reader
from extractor import analyze as tesseract_analyze
from formatter import VALID_TYPES, FileLabel, build_filename, sanitize
from ocr import extract_text as tesseract_extract_text

# Preview rendering — optional. The app still launches without these so users
# who only want filename auto-rename never get blocked on poppler/Pillow.
try:
    from PIL import Image, ImageTk  # noqa: F401
    from pdf2image import convert_from_path
    _PREVIEW_OK = True
    _PREVIEW_ERR = ""
except Exception as _e:
    _PREVIEW_OK = False
    _PREVIEW_ERR = f"{type(_e).__name__}: {_e}"


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
# Queue persistence
# ---------------------------------------------------------------------------
#
# The queue survives app restarts so users don't lose in-progress work. On
# restore, each row's file is fingerprinted (size + mtime) and compared to the
# saved fingerprint — this catches the "same filename, different scan" case
# where trusting the old label would mis-file a fresh document.

QUEUE_FILE = Path.home() / ".pdf-labeler" / "queue.json"


def _file_fingerprint(path: str) -> Optional[Dict[str, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    # int(mtime) — sub-second precision isn't stable across filesystems and
    # would trigger spurious "changed" flags after a copy.
    return {"size": st.st_size, "mtime": int(st.st_mtime)}


def _serialize_rows(rows: List["Row"]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows:
        fp = _file_fingerprint(r.path)
        out.append({
            "path":   r.path,
            "name":   r.name,
            "ftype":  r.ftype,
            "dates":  [d.isoformat() for d in r.dates],
            "status": r.status,
            "size":   fp["size"]  if fp else None,
            "mtime":  fp["mtime"] if fp else None,
        })
    return out


def _persist_queue(rows: List["Row"]) -> None:
    """Best-effort save of the current row list to ~/.pdf-labeler/queue.json.
    Silently swallows I/O errors — persistence must never crash the app."""
    try:
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with QUEUE_FILE.open("w") as fh:
            json.dump(_serialize_rows(rows), fh, indent=2)
    except OSError:
        pass


def _load_persisted_queue() -> List[Dict[str, Any]]:
    if not QUEUE_FILE.exists():
        return []
    try:
        with QUEUE_FILE.open("r") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


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
# PDF preview pane
# ---------------------------------------------------------------------------

class PreviewPane(ttk.Frame):
    """Right-hand side: renders the selected PDF page + edit fields.

    Bind `on_save_one` to a single-row rename function. Editing the fields
    writes back into the selected Row so the table stays in sync.
    """

    _IMG_CACHE_LIMIT = 8       # path → PIL.Image, evicted LRU-style
    _RENDER_DPI      = 110     # preview quality; bumps fidelity vs. speed

    def __init__(self, parent, app, on_save_one, on_next=None):
        super().__init__(parent)
        self._app = app
        self._on_save_one = on_save_one
        self._on_next = on_next
        self._row: Optional[Row] = None
        self._page = 1
        self._page_count = 1
        self._tk_img = None                # keep reference so GC doesn't kill it
        self._img_cache: Dict[tuple, "Image.Image"] = {}

        # Header: filename + page nav
        header = ttk.Frame(self)
        header.pack(fill="x", padx=8, pady=(8, 4))
        self._title_var = tk.StringVar(value="No file selected")
        ttk.Label(header, textvariable=self._title_var, anchor="w",
                  font=("TkDefaultFont", 10, "bold")).pack(side="left", fill="x", expand=True)
        self._prev_btn = ttk.Button(header, text="◀", width=3, command=self._prev_page)
        self._prev_btn.pack(side="left", padx=(4, 0))
        self._page_var = tk.StringVar(value="–/–")
        ttk.Label(header, textvariable=self._page_var, width=8, anchor="center").pack(side="left")
        self._next_btn = ttk.Button(header, text="▶", width=3, command=self._next_page)
        self._next_btn.pack(side="left")

        # Image canvas
        canvas_frame = ttk.Frame(self, relief="sunken", borderwidth=1)
        canvas_frame.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self._canvas = tk.Canvas(canvas_frame, background="#e6e6e6", highlightthickness=0)
        self._canvas.pack(fill="both", expand=True)
        self._canvas.bind("<Configure>", self._on_canvas_resize)
        if not _PREVIEW_OK:
            self._canvas.create_text(
                10, 10, anchor="nw", width=300,
                fill="#666",
                text=("PDF preview unavailable.\n"
                      "Install with:  pip install Pillow pdf2image\n"
                      "and the system poppler binary (brew install poppler).\n\n"
                      f"Import error: {_PREVIEW_ERR}"),
            )

        # Editor form
        form = ttk.Frame(self)
        form.pack(fill="x", padx=8, pady=(0, 8))
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Name:").grid(row=0, column=0, sticky="w", pady=2)
        self._name_var = tk.StringVar()
        self._name_entry = ttk.Entry(form, textvariable=self._name_var)
        self._name_entry.grid(row=0, column=1, sticky="we", padx=(4, 0), pady=2)

        ttk.Label(form, text="Type:").grid(row=1, column=0, sticky="w", pady=2)
        self._type_var = tk.StringVar()
        self._type_combo = ttk.Combobox(form, textvariable=self._type_var,
                                        values=sorted(VALID_TYPES), state="readonly")
        self._type_combo.grid(row=1, column=1, sticky="we", padx=(4, 0), pady=2)

        ttk.Label(form, text="Dates:").grid(row=2, column=0, sticky="w", pady=2)
        self._dates_var = tk.StringVar()
        self._dates_entry = ttk.Entry(form, textvariable=self._dates_var)
        self._dates_entry.grid(row=2, column=1, sticky="we", padx=(4, 0), pady=2)

        ttk.Label(form, text="Proposed:").grid(row=3, column=0, sticky="w", pady=2)
        self._proposed_var = tk.StringVar(value="—")
        ttk.Label(form, textvariable=self._proposed_var, foreground="#225",
                  wraplength=380, justify="left").grid(row=3, column=1, sticky="we",
                                                        padx=(4, 0), pady=2)

        for var in (self._name_var, self._type_var, self._dates_var):
            var.trace_add("write", lambda *_a: self._on_field_edit())

        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", padx=8, pady=(0, 8))
        # Rightmost = the button you'll hit most: save this file and jump
        # straight to the next one that still needs work.
        self._save_next_btn = ttk.Button(btn_row, text="Save & Next \u25b6",
                                         command=self._save_next_clicked, state="disabled")
        self._save_next_btn.pack(side="right")
        self._save_btn = ttk.Button(btn_row, text="Save & Rename This File",
                                    command=self._save_clicked, state="disabled")
        self._save_btn.pack(side="right", padx=(0, 6))
        self._skip_btn = ttk.Button(btn_row, text="Skip \u25b6", width=8,
                                    command=self._skip_clicked, state="disabled")
        self._skip_btn.pack(side="left")

        # Keyboard shortcut for the same thing — bound to the pane's own
        # widgets (not bind_all) so dialogs elsewhere keep their Enter key.
        for w in (self, self._name_entry, self._type_combo,
                  self._dates_entry, self._canvas):
            for seq in ("<Command-Return>", "<Control-Return>"):
                w.bind(seq, self._save_next_key)

        self._set_enabled(False)

    # ---- public API ------------------------------------------------------

    def show_row(self, row: Optional[Row]) -> None:
        """Switch preview + form to this row (or clear if None)."""
        self._row = row
        if row is None:
            self._title_var.set("No file selected")
            self._page_var.set("–/–")
            self._proposed_var.set("—")
            self._canvas.delete("img")
            self._set_enabled(False)
            for v in (self._name_var, self._type_var, self._dates_var):
                v.set("")
            return

        self._set_enabled(True)
        self._title_var.set(os.path.basename(row.path))
        # Push row values into the entries WITHOUT triggering write-back.
        self._suspend_trace = True
        self._name_var.set(row.name)
        self._type_var.set(row.ftype)
        self._dates_var.set(dates_to_string(row.dates))
        self._suspend_trace = False
        self._refresh_proposed()
        self._page = 1
        self._render_async()

    def refresh_from_row(self) -> None:
        """Re-pull values from current row (e.g. after background analysis finishes)."""
        if self._row is not None:
            self.show_row(self._row)

    # ---- page navigation -------------------------------------------------

    def _prev_page(self) -> None:
        if self._page > 1:
            self._page -= 1
            self._render_async()

    def _next_page(self) -> None:
        if self._page < self._page_count:
            self._page += 1
            self._render_async()

    # ---- rendering -------------------------------------------------------

    def _on_canvas_resize(self, _event) -> None:
        # Re-place the cached image to fit the new canvas size.
        if self._row is not None and _PREVIEW_OK:
            self._render_async()

    def _render_async(self) -> None:
        if not _PREVIEW_OK or self._row is None:
            return
        path, page = self._row.path, self._page
        cw = max(self._canvas.winfo_width(), 1)
        ch = max(self._canvas.winfo_height(), 1)

        # Snapshot for the worker; if user clicks a different row mid-render,
        # the stale result gets discarded by the path/page check.
        def worker():
            try:
                img, total = self._get_page_image(path, page)
            except Exception as e:
                self._app.after(0, lambda: self._show_render_error(str(e)))
                return
            self._app.after(0, lambda: self._display_image(path, page, img, total, cw, ch))

        threading.Thread(target=worker, daemon=True).start()

    def _get_page_image(self, path: str, page: int):
        # Render page count once (cheap) — pdf2image's last_page=None gives all,
        # but we only render a single page at a time for speed.
        cache_key = (path, page, self._RENDER_DPI)
        if cache_key in self._img_cache:
            img = self._img_cache[cache_key]
        else:
            pages = convert_from_path(path, dpi=self._RENDER_DPI,
                                      first_page=page, last_page=page)
            if not pages:
                raise RuntimeError("PDF returned no pages")
            img = pages[0]
            self._img_cache[cache_key] = img
            # Evict oldest entries beyond limit.
            while len(self._img_cache) > self._IMG_CACHE_LIMIT:
                self._img_cache.pop(next(iter(self._img_cache)))

        # Cheap way to learn page count: render-on-demand and discover when
        # next-page fails. Better: use pypdf once.
        total = self._page_count_for(path)
        return img, total

    def _page_count_for(self, path: str) -> int:
        try:
            from pypdf import PdfReader
            return len(PdfReader(path).pages)
        except Exception:
            return self._page  # unknown — show current as last

    def _display_image(self, path, page, pil_img, total, cw, ch) -> None:
        # Stale guard — selection may have changed during the worker thread.
        if self._row is None or self._row.path != path or self._page != page:
            return
        self._page_count = max(total, 1)
        self._page_var.set(f"{self._page}/{self._page_count}")
        self._prev_btn.configure(state="normal" if self._page > 1 else "disabled")
        self._next_btn.configure(state="normal" if self._page < self._page_count else "disabled")
        scaled = pil_img.copy()
        scaled.thumbnail((cw, ch), Image.LANCZOS)
        self._tk_img = ImageTk.PhotoImage(scaled)
        self._canvas.delete("img")
        x = (cw - scaled.width) // 2
        y = (ch - scaled.height) // 2
        self._canvas.create_image(x, y, image=self._tk_img, anchor="nw", tags=("img",))

    def _show_render_error(self, msg: str) -> None:
        self._canvas.delete("img")
        self._canvas.create_text(10, 10, anchor="nw", width=400, fill="#a00",
                                  text=f"Preview failed:\n{msg}", tags=("img",))

    # ---- field <-> row sync ---------------------------------------------

    _suspend_trace = False

    def _on_field_edit(self) -> None:
        if self._suspend_trace or self._row is None:
            return
        self._row.name  = self._name_var.get().strip()
        self._row.ftype = self._type_var.get().strip()
        self._row.dates = parse_dates_field(self._dates_var.get())
        self._row.status = self._app._status_for(self._row)
        self._app._refresh_row(self._row)
        self._refresh_proposed()

    def _refresh_proposed(self) -> None:
        if self._row is None:
            self._proposed_var.set("—")
            return
        proposed = self._row.proposed_filename() or "(incomplete — fill all fields)"
        self._proposed_var.set(proposed)

    def _set_enabled(self, on: bool) -> None:
        state = "normal" if on else "disabled"
        self._name_entry.configure(state=state)
        # Combobox uses readonly to remain dropdown-like when enabled.
        self._type_combo.configure(state=("readonly" if on else "disabled"))
        self._dates_entry.configure(state=state)
        self._save_btn.configure(state=state)
        self._save_next_btn.configure(state=state)
        self._skip_btn.configure(state=state)
        self._prev_btn.configure(state="disabled")
        self._next_btn.configure(state="disabled")

    def _save_clicked(self) -> None:
        if self._row is None:
            return
        self._on_save_one(self._row)

    def _save_next_clicked(self) -> None:
        """Save this file, then move the selection to the next one.

        Only advances when the rename actually succeeded — a failed or
        incomplete save leaves you on the row so you can fix it."""
        if self._row is None:
            return
        if self._on_save_one(self._row) and self._on_next is not None:
            self._on_next(self._row)

    def _skip_clicked(self) -> None:
        """Move on without saving (e.g. a file you'll come back to)."""
        if self._row is not None and self._on_next is not None:
            self._on_next(self._row)

    def _save_next_key(self, _event=None):
        if self._row is not None and str(self._save_next_btn["state"]) != "disabled":
            self._save_next_clicked()
        return "break"


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

        # Date hints (apply to all readers)
        ttk.Separator(self, orient="horizontal").grid(row=7, column=0, columnspan=2,
                                                     sticky="we", pady=8)
        ttk.Label(self, text="Expected year(s):").grid(row=8, column=0, sticky="w", **pad)
        self.years_var = tk.StringVar(value=self._settings.get("expected_years", "2026"))
        ttk.Entry(self, textvariable=self.years_var, width=20).grid(
            row=8, column=1, sticky="w", **pad)
        ttk.Label(self,
                  text=('  e.g. "2026" or "2025-2026". Hints the reader when year\n'
                        '  digits are ambiguous, and flags dates outside the range.'),
                  foreground="#666", justify="left").grid(
            row=9, column=1, sticky="w", padx=10)

        ttk.Label(self, text="Expected month(s):").grid(row=10, column=0, sticky="w", **pad)
        self.months_var = tk.StringVar(value=self._settings.get("expected_months", ""))
        ttk.Entry(self, textvariable=self.months_var, width=20).grid(
            row=10, column=1, sticky="w", **pad)
        ttk.Label(self,
                  text=('  e.g. "5", "4-6" or "1,3,5". Leave blank for no constraint.\n'
                        '  Helps disambiguate 1/7, 4/9, 3/8 misreads.'),
                  foreground="#666", justify="left").grid(
            row=11, column=1, sticky="w", padx=10)

        # Buttons
        btns = ttk.Frame(self)
        btns.grid(row=12, column=0, columnspan=2, sticky="e", padx=10, pady=10)
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
            "provider":        self.provider_var.get(),
            "gemini_api_key":  self.gemini_key.get().strip(),
            "gemini_model":    self.gemini_model.get().strip() or "gemini-2.5-flash",
            "claude_api_key":  self.claude_key.get().strip(),
            "claude_model":    self.claude_model.get().strip() or "claude-haiku-4-5",
            "expected_years":  self.years_var.get().strip(),
            "expected_months": self.months_var.get().strip(),
        }
        self.destroy()


# ---------------------------------------------------------------------------
# Names dialog
# ---------------------------------------------------------------------------

class NamesDialog(tk.Toplevel):
    """Edit a roster (employees or clients) used to guide handwriting recognition."""

    def __init__(self, parent, kind: str = "employees"):
        super().__init__(parent)
        if kind == "clients":
            self._title       = "Known Clients"
            self._blurb       = (
                "Clients are the people receiving services on PCS / IHR / Trans / E&D forms.\n"
                "One name per line; full \"Firstname Lastname\" pairs work best."
            )
            self._load_fn = ai_reader.load_known_clients
            self._save_fn = ai_reader.save_known_clients
        else:
            self._title       = "Known Employees"
            self._blurb       = (
                "Employees are the staff named on TS (timesheet) forms.\n"
                "One name per line; full \"Firstname Lastname\" pairs work best."
            )
            self._load_fn = ai_reader.load_known_names
            self._save_fn = ai_reader.save_known_names

        self.title(self._title)
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()
        self.geometry("440x500")

        ttk.Label(self, text=(
            self._blurb +
            "\nImport a .docx roster (table of names) or CSV via Import…"
        ), justify="left").pack(anchor="w", padx=10, pady=(10, 4))

        # Pack buttons FIRST so they anchor to the bottom before the text
        # area claims all remaining space.
        btns = ttk.Frame(self)
        btns.pack(side="bottom", fill="x", padx=10, pady=(6, 10))
        ttk.Button(btns, text="Import…", command=self._import_file).pack(side="left")
        ttk.Button(btns, text="Cancel",      command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="Save",        command=self._save).pack(side="right")

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 4))

        self._text = tk.Text(frame, wrap="word", font=("TkFixedFont", 12))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self._text.yview)
        self._text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._text.pack(side="left", fill="both", expand=True)

        self._text.insert("1.0", "\n".join(self._load_fn()))

        self.wait_window()

    def _import_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Select roster file (CSV or DOCX)",
            filetypes=[
                ("Roster files", "*.csv *.docx"),
                ("CSV files", "*.csv"),
                ("Word documents", "*.docx"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        imported: List[str] = []
        try:
            if path.lower().endswith(".docx"):
                imported = ai_reader.names_from_docx(path)
            else:
                import csv
                with open(path, newline="", encoding="utf-8-sig") as fh:
                    for row in csv.reader(fh):
                        # Treat each row as a full "First Last" entry; cells
                        # joined preserve pair-context that the AI prefers.
                        joined = " ".join(c.strip() for c in row if c.strip())
                        if joined:
                            imported.append(joined)
        except Exception as e:
            messagebox.showerror("Import error", str(e), parent=self)
            return
        existing = self._text.get("1.0", "end").strip()
        merged = "\n".join(filter(None, [existing] + imported))
        self._text.delete("1.0", "end")
        self._text.insert("1.0", merged)
        messagebox.showinfo("Imported", f"Added {len(imported)} name(s). Click Save to keep them.", parent=self)

    def _save(self) -> None:
        raw = self._text.get("1.0", "end")
        entries = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        self._save_fn(entries)
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
        # Pause gate — worker threads block on this before each file. Set =
        # running, cleared = paused. Starts running so first batch just flows.
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._build_ui()
        self._update_reader_label()
        self._restore_queue()

    # ---- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=8)

        ttk.Button(top, text="Add PDFs",         command=self.add_files).pack(side="left")
        ttk.Button(top, text="Add Folder",       command=self.add_folder).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Clear",            command=self.clear_rows).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Re-scan Selected", command=self.rescan_selected).pack(side="left", padx=(20, 0))
        self._pause_btn = ttk.Button(top, text="Pause", command=self._toggle_pause)
        self._pause_btn.pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Clients…",         command=self.open_clients).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="Employees…",       command=self.open_names).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="Settings…",        command=self.open_settings).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="Apply Rename",     command=self.apply_rename).pack(side="right")

        # Split: table on the left, PDF preview + editor on the right.
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        table_frame = ttk.Frame(paned)
        paned.add(table_frame, weight=3)

        self.tree = ttk.Treeview(table_frame, columns=self.COLS, show="headings",
                                 selectmode="browse")
        for col, label, width in [
            ("current",  "Current filename",    240),
            ("name",     "Name",                150),
            ("type",     "Type",                60),
            ("dates",    "Dates (M/D/YY, ...)", 200),
            ("proposed", "Proposed filename",   260),
            ("status",   "Status",              90),
        ]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # Preview + editor on the right.
        self.preview = PreviewPane(paned, app=self, on_save_one=self._rename_one,
                                   on_next=self.select_next_row)
        paned.add(self.preview, weight=2)

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
        self.tree.tag_configure("review",   background="#fff5d6")
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
        NamesDialog(self, kind="employees")
        self.status_var.set("Employees roster saved.")

    def open_clients(self) -> None:
        NamesDialog(self, kind="clients")
        self.status_var.set("Clients roster saved.")

    def _toggle_pause(self) -> None:
        if self._pause_event.is_set():
            self._pause_event.clear()
            self._pause_btn.configure(text="Resume")
            self._set_global_status(
                "Paused. Current file will finish, then processing stops."
            )
        else:
            self._pause_event.set()
            self._pause_btn.configure(text="Pause")
            # Worker (if any) picks up on next iteration.

    # ---- Queue restore ---------------------------------------------------

    _STATUS_TAG = {
        "ok": "ok",
        "renamed": "renamed",
        "missing": "error",
        "changed — re-scan": "review",
    }

    def _tag_for_status(self, status: str) -> str:
        if status in self._STATUS_TAG:
            return self._STATUS_TAG[status]
        if status.startswith("review"):
            return "review"
        if status.startswith("error"):
            return "error"
        return ""

    def _restore_queue(self) -> None:
        """Rehydrate rows from ~/.pdf-labeler/queue.json. Runs on startup.

        Reconciliation rules:
        - status == "renamed" AND file gone   → drop silently (job done).
        - file gone otherwise                 → keep as "missing" (red) so
                                                nothing quietly disappears.
        - file present, fingerprint differs   → "changed — re-scan" (yellow),
                                                Name/Type/Dates cleared.
        - file present, fingerprint matches   → restore verbatim.
        Transient statuses ("reading…", "queued") come back as "queued".
        """
        saved = _load_persisted_queue()
        if not saved:
            return
        restored = missing = changed = 0
        for entry in saved:
            path = entry.get("path", "")
            saved_status = entry.get("status", "")
            if not isinstance(path, str) or not path:
                continue

            file_exists = os.path.isfile(path)

            # Completed jobs whose file is gone: drop. Any other absence keeps
            # the row so the user can see what went missing.
            if not file_exists and saved_status == "renamed":
                continue

            row = Row(path)
            row.name  = entry.get("name", "") or ""
            row.ftype = entry.get("ftype", "") or ""
            row.dates = []
            for s in entry.get("dates", []) or []:
                try:
                    row.dates.append(date.fromisoformat(s))
                except (TypeError, ValueError):
                    pass

            if not file_exists:
                row.status = "missing"
                missing += 1
            else:
                fp    = _file_fingerprint(path)
                saved_size  = entry.get("size")
                saved_mtime = entry.get("mtime")
                content_changed = (
                    fp is not None and saved_size is not None
                    and saved_mtime is not None
                    and (fp["size"] != saved_size or fp["mtime"] != saved_mtime)
                )
                if content_changed:
                    # New scan behind an old row — clear the label so we don't
                    # apply the previous filename to a different document.
                    row.name  = ""
                    row.ftype = ""
                    row.dates = []
                    row.status = "changed — re-scan"
                    changed += 1
                elif saved_status in ("reading…", "queued", ""):
                    row.status = "queued"
                    restored += 1
                else:
                    row.status = saved_status
                    restored += 1

            proposed = row.proposed_filename() or ""
            iid = self.tree.insert("", "end", values=(
                os.path.basename(row.path),
                row.name, row.ftype,
                dates_to_string(row.dates),
                proposed, row.status,
            ))
            row.iid = iid
            tag = self._tag_for_status(row.status)
            if tag:
                self.tree.item(iid, tags=(tag,))
            self.rows.append(row)

        if restored or missing or changed:
            parts = [f"restored {restored}"]
            if changed: parts.append(f"{changed} changed on disk")
            if missing: parts.append(f"{missing} missing")
            self._set_global_status("Queue: " + ", ".join(parts) + ".")

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
        # Persist immediately so a crash between here and first _refresh_row
        # still preserves the file list.
        _persist_queue(self.rows)
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
            # Block here when paused. Only the NEXT file waits — a file already
            # mid-analysis will finish its AI call before we get back to this
            # check. Deliberate trade-off: keeps the worker simple and avoids
            # dropping half-processed results.
            if not self._pause_event.is_set():
                self._set_global_status("Paused. Click Resume to continue.")
                self._pause_event.wait()
                self._set_global_status("Resumed.")
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
                              status=self._status_for(row),
                              update_preview=True)
        self._set_global_status(f"{len(self.rows)} file(s) loaded.")

    # ---- Row display -----------------------------------------------------

    def _status_for(self, row: Row) -> str:
        """Compute the post-analysis status for a row, including date-range review."""
        if not row.proposed_filename():
            return "incomplete"
        roster = ai_reader.roster_for_type(row.ftype)
        if roster and row.name and \
                row.name.lower() not in {n.lower() for n in roster}:
            return "review: name not on roster"
        yr = ai_reader.parse_year_range(self.settings.get("expected_years", ""))
        if yr is not None:
            lo, hi = yr
            oor_y = sorted({d.year for d in row.dates if d.year < lo or d.year > hi})
            if oor_y:
                return f"review: year {oor_y[0]} outside {lo}-{hi}" if lo != hi \
                       else f"review: year {oor_y[0]} ≠ {lo}"
        months = ai_reader.parse_month_set(self.settings.get("expected_months", ""))
        if months is not None:
            allowed = set(months)
            oor_m = sorted({d.month for d in row.dates if d.month not in allowed})
            if oor_m:
                shown = ",".join(str(m) for m in months)
                return f"review: month {oor_m[0]} outside [{shown}]"
        return "ok"

    def _refresh_row(self, row: Row, status: Optional[str] = None,
                     update_preview: bool = False) -> None:
        proposed = row.proposed_filename() or ""
        if status is not None:
            row.status = status
        tag = self._tag_for_status(row.status)

        def update():
            self.tree.item(row.iid, values=(
                os.path.basename(row.path),
                row.name, row.ftype,
                dates_to_string(row.dates),
                proposed, row.status,
            ), tags=(tag,) if tag else ())
            # Only AI-driven or external updates push values back into the
            # preview entries — user edits must NOT round-trip, or each
            # keystroke wipes the field being typed into.
            if update_preview and self.preview._row is row:
                self.preview.refresh_from_row()
            # Persist here (main thread) so concurrent workers don't race on
            # writing the queue file. Cheap enough to do per row change.
            _persist_queue(self.rows)
        self.after(0, update)

    def _on_tree_select(self, _event=None) -> None:
        iids = self.tree.selection()
        if not iids:
            self.preview.show_row(None)
            return
        row = self._row_for_iid(iids[0])
        self.preview.show_row(row)

    def _set_status(self, row: Row, status: str) -> None:
        row.status = status
        self._refresh_row(row, update_preview=True)

    def _mark_error(self, row: Row, msg: str) -> None:
        row.status = f"error: {msg[:90]}"
        self._refresh_row(row, update_preview=True)

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
        row.status = self._status_for(row)
        # Inline edit commits a finalised value (Enter / FocusOut) — safe to
        # push into the preview pane so its fields don't lag the table.
        self._refresh_row(row, update_preview=True)

    def _row_for_iid(self, iid: str) -> Optional[Row]:
        for r in self.rows:
            if r.iid == iid:
                return r
        return None

    # ---- Actions ---------------------------------------------------------

    def clear_rows(self) -> None:
        if not self.rows:
            return
        # Two-click destruction. Message spells out that PDFs on disk are
        # untouched — only queue entries and any AI-inferred fields are lost.
        if not messagebox.askyesno(
            "Clear queue",
            f"Clear {len(self.rows)} file(s) from the queue?\n\n"
            "Files already renamed on disk stay put. Unfinished rows lose "
            "their Name / Type / Dates — you'll need to re-scan to get them "
            "back.",
        ):
            return
        for r in self.rows:
            self.tree.delete(r.iid)
        self.rows.clear()
        _persist_queue(self.rows)
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
                self._refresh_row(r, update_preview=True)
                paths.append(r.path)
        if paths:
            threading.Thread(target=self._process_rows, args=(paths,), daemon=True).start()

    def _rename_one(self, row: Row) -> bool:
        """Rename a single file in place. Triggered from the preview pane's
        Save button — lets the user commit edits one at a time after eyeballing
        the page next to the proposed filename.

        Returns True when the file is on disk under its new name, so the
        "Save & Next" caller knows whether it may advance."""
        proposed = row.proposed_filename()
        if not proposed:
            messagebox.showwarning(
                "Incomplete",
                "Fill Name, Type and at least one Date before saving.",
            )
            return False
        if row.status == "renamed":
            messagebox.showinfo("Already renamed",
                                f"This file was already renamed to:\n{os.path.basename(row.path)}")
            return False
        folder = os.path.dirname(row.path)
        target = self._unique_path(os.path.join(folder, proposed))
        try:
            os.rename(row.path, target)
        except OSError as e:
            row.status = f"error: {e}"
            self._refresh_row(row)
            messagebox.showerror("Rename failed", str(e))
            return False
        row.path = target
        row.status = "renamed"
        self._refresh_row(row, update_preview=True)
        self._set_global_status(f"Renamed → {os.path.basename(target)}")
        return True

    def select_next_row(self, current: Row) -> None:
        """Select the next file below `current` that still needs work.

        Skips rows already renamed so a long finished stretch doesn't make you
        click through it. Falls back to the very next row if everything below
        is done, and stays put at the end of the list."""
        iids = list(self.tree.get_children(""))
        try:
            start = iids.index(current.iid) + 1
        except ValueError:
            return
        rest = iids[start:]
        if not rest:
            self._set_global_status("Last file in the list — nothing after this one.")
            return
        target = next(
            (i for i in rest
             if (r := self._row_for_iid(i)) is not None and r.status != "renamed"),
            rest[0],
        )
        self.tree.selection_set(target)
        self.tree.focus(target)
        self.tree.see(target)
        # selection_set fires <<TreeviewSelect>>, which swaps the preview.

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
            self._refresh_row(r, update_preview=True)
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
