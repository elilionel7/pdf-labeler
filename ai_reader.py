"""
Vision-based reader that can handle HANDWRITTEN scanned forms.

Supports two providers:
  * Gemini (Google AI Studio)  -- free tier, recommended
  * Claude (Anthropic)         -- paid, small per-file cost

Settings are persisted to ~/.pdf-labeler/settings.json so the user only
has to enter their API key once.

Public API used by app.py:
  load_settings() -> dict
  save_settings(settings: dict) -> None
  is_available(settings: dict) -> bool
  analyze_pdf(pdf_path, settings: dict) -> FileLabel
"""

from __future__ import annotations

import base64
import difflib
import json
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

from formatter import FileLabel, VALID_TYPES

# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------

SETTINGS_DIR = Path.home() / ".pdf-labeler"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"
NAMES_FILE = SETTINGS_DIR / "names.txt"
CLIENTS_FILE = SETTINGS_DIR / "clients.txt"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "provider": "gemini",  # "gemini" | "claude" | "tesseract"
    "gemini_api_key": "",
    "gemini_model": "gemini-2.5-flash",
    "claude_api_key": "",
    "claude_model": "claude-haiku-4-5",
    # Hint for handwriting-ambiguous year digits. A single year ("2026") or
    # a range ("2025-2026") for forms that may straddle Dec/Jan. Empty = off.
    "expected_years": "2026",
    # Same idea for months — handwritten 1/7, 4/9, 3/8 confuse the reader.
    # Accepts "5", "4-6", "1,3,5" or "1-3,5,7-9". Empty = off (no constraint).
    "expected_months": "",
}


def parse_year_range(text: str):
    """Parse '2026' or '2025-2026' (also en-dash) into (low, high). None on miss."""
    if not text:
        return None
    s = text.strip().replace("–", "-").replace("—", "-")
    m = re.match(r"^(\d{4})\s*(?:-\s*(\d{4}))?$", s)
    if not m:
        return None
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    if lo > hi:
        lo, hi = hi, lo
    return (lo, hi)


def parse_month_set(text: str):
    """Parse '5', '4-6', '1,3,5' or '1-3,5,7-9' into a sorted set of months.
    Returns None if the field is empty or any token is invalid."""
    if not text or not text.strip():
        return None
    s = text.strip().replace("–", "-").replace("—", "-")
    out: set = set()
    for chunk in s.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"^(\d{1,2})\s*(?:-\s*(\d{1,2}))?$", chunk)
        if not m:
            return None
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        if lo > hi:
            lo, hi = hi, lo
        if not (1 <= lo <= 12 and 1 <= hi <= 12):
            return None
        out.update(range(lo, hi + 1))
    return sorted(out) or None


def load_known_names() -> List[str]:
    """Return the list of known names from ~/.pdf-labeler/names.txt.

    Bootstraps from a names.docx sitting next to the app on first run, so the
    user gets the roster automatically without clicking Import.
    """
    if not NAMES_FILE.exists():
        bootstrapped = _bootstrap_from_local_docx()
        if bootstrapped:
            save_known_names(bootstrapped)
            return bootstrapped
        return []
    try:
        with NAMES_FILE.open("r") as fh:
            return [ln.strip() for ln in fh if ln.strip()]
    except OSError:
        return []


def save_known_names(names: List[str]) -> None:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    with NAMES_FILE.open("w") as fh:
        fh.write("\n".join(n.strip() for n in names if n.strip()))


def names_from_docx(path: str) -> List[str]:
    """Parse a roster .docx. Tables: join cells in each row as 'First Last'.
    Paragraphs: each non-empty line becomes one entry."""
    try:
        from docx import Document
    except ImportError as e:
        raise ImportError(
            "Reading .docx rosters needs 'python-docx'.\n"
            "Install with:  pip install python-docx"
        ) from e

    doc = Document(path)
    out: List[str] = []
    seen: set = set()

    def add(value: str) -> None:
        v = " ".join(value.split())  # this line feel redundant why not keep value
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)

    for table in doc.tables:
        for row in table.rows:
            parts = [c.text.strip() for c in row.cells if c.text.strip()]
            if not parts:
                continue
            add(" ".join(parts))
    for para in doc.paragraphs:
        if para.text.strip():
            add(para.text)
    return out


def _bootstrap_from_local_docx() -> List[str]:
    """Look for a names.docx next to the app (or in CWD) and parse it.
    Returns [] on any failure — bootstrap is best-effort."""
    candidates = [
        Path(__file__).resolve().parent / "names.docx",
        Path.cwd() / "names.docx",
    ]
    for path in candidates:
        if path.exists():
            try:
                return names_from_docx(str(path))
            except Exception:
                return []
    return []


# --- Clients roster (for PCS / IHR / Trans / E&D forms) -------------------
#
# names.txt holds EMPLOYEES (used on TS forms). clients.txt holds the people
# who receive the service (the "Client Name:" on the other four form types).
# Separating them keeps each prompt short and stops the model from biasing a
# client read toward an employee spelling, or vice versa.


def load_known_clients() -> List[str]:
    if not CLIENTS_FILE.exists():
        bootstrapped = _bootstrap_clients_from_hpscans()
        if bootstrapped:
            save_known_clients(bootstrapped)
            return bootstrapped
        return []
    try:
        with CLIENTS_FILE.open("r") as fh:
            return [ln.strip() for ln in fh if ln.strip()]
    except OSError:
        return []


def save_known_clients(clients: List[str]) -> None:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    with CLIENTS_FILE.open("w") as fh:
        fh.write("\n".join(c.strip() for c in clients if c.strip()))


def _bootstrap_clients_from_hpscans() -> List[str]:
    """Extract client names from manually-labeled filenames in HPSCANS/.

    Files are named '<First Last>_<TYPE>_<dates>.pdf'; the first underscore-
    separated chunk of any PCS/IHR/Trans/E&D file is a client name we can
    reuse as a roster entry. TS files are skipped (those are employees)."""
    candidates = [
        Path(__file__).resolve().parent / "HPSCANS",
        Path.cwd() / "HPSCANS",
    ]
    client_types = {"PCS", "IHR", "Trans", "E&D"}
    seen: set = set()
    out: List[str] = []
    for dir_path in candidates:
        if not dir_path.is_dir():
            continue
        for pdf in dir_path.glob("*.pdf"):
            stem = pdf.stem  # strip .pdf
            parts = re.split(r"[_-]", stem, maxsplit=2)
            if len(parts) < 2:
                continue
            name, ftype = parts[0].strip(), parts[1].strip()
            if ftype not in client_types or not name:
                continue
            canon = " ".join(name.split())
            key = canon.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(canon)
        if out:
            return sorted(out, key=str.lower)
    return []


# --- Snap-to-roster fuzzy matching ----------------------------------------
#
# The prompt asks the model to prefer roster spellings, but nothing guarantees
# it complies. This post-processing step makes the guarantee in code: if the
# transcribed name is close enough to a roster entry, replace it with the
# roster's exact spelling. The model only has to get *close*.

SNAP_THRESHOLD = 0.80


def roster_for_type(ftype: str) -> List[str]:
    """Return the roster that applies to a form type (employees for TS,
    clients for the rest), or [] when the type is unknown."""
    if ftype == "TS":
        return load_known_names()
    if ftype in ("PCS", "IHR", "Trans", "E&D"):
        return load_known_clients()
    return []


def _name_similarity(a: str, b: str) -> float:
    direct = difflib.SequenceMatcher(None, a, b).ratio()
    # Also compare with words sorted, so a reversed "Lastname Firstname"
    # transcription still matches its roster entry.
    sa = " ".join(sorted(a.split()))
    sb = " ".join(sorted(b.split()))
    return max(direct, difflib.SequenceMatcher(None, sa, sb).ratio())


def snap_to_roster(
    name: str,
    ftype: str,
    roster: "List[str] | None" = None,
) -> "tuple[str, bool]":
    """Fuzzy-match a transcribed name against the roster for this form type.

    Returns (roster_spelling, True) when a confident match exists, otherwise
    (name unchanged, False)."""
    name = " ".join((name or "").split())
    if not name:
        return name, False
    if roster is None:
        roster = roster_for_type(ftype)
    if not roster:
        return name, False
    target = name.lower()
    best, best_score = "", 0.0
    for entry in roster:
        cand = " ".join(entry.split())
        score = _name_similarity(target, cand.lower())
        if score > best_score:
            best, best_score = cand, score
    if best and best_score >= SNAP_THRESHOLD:
        return best, True
    return name, False


def load_settings() -> Dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    try:
        if SETTINGS_FILE.exists():
            with SETTINGS_FILE.open("r") as fh:
                settings.update(json.load(fh))
    except (OSError, json.JSONDecodeError):
        pass
    # Env vars override whatever is in the file
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        settings["gemini_api_key"] = os.environ.get("GEMINI_API_KEY") or os.environ.get(
            "GOOGLE_API_KEY"
        )
    if os.environ.get("ANTHROPIC_API_KEY"):
        settings["claude_api_key"] = os.environ["ANTHROPIC_API_KEY"]
    return settings


def save_settings(settings: Dict[str, Any]) -> None:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    with SETTINGS_FILE.open("w") as fh:
        json.dump(settings, fh, indent=2)
    try:
        os.chmod(SETTINGS_FILE, 0o600)  # API keys are sensitive
    except OSError:
        pass


def is_available(settings: Dict[str, Any]) -> bool:
    provider = settings.get("provider")
    if provider == "gemini":
        return bool(settings.get("gemini_api_key"))
    if provider == "claude":
        return bool(settings.get("claude_api_key"))
    return False


# ---------------------------------------------------------------------------
# Shared prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You read scanned service/time-entry forms from home-care agencies. "
    "Many fields are handwritten. Extract structured data precisely and return JSON."
)

USER_PROMPT = """\
This PDF is a scanned form. Extract the following fields and return ONLY
JSON (no prose, no markdown fences, no comments):

{
  "file_type": "PCS" | "IHR" | "Trans" | "TS" | "E&D" | null,
  "full_name": "Firstname Lastname" | null,
  "dates":     ["YYYY-MM-DD", ...]
}

Rules:
- file_type — determined ONLY by the form's title / header line:
    * "PCS Activity Sheet" / "PCS Notes" / "Personal Care Services" -> "PCS"
    * "In-Home Respite" / "Respite Care" -> "IHR"
    * "Transportation" / "Trip Log" / "Mileage Log" -> "Trans"
    * "Employee Bi-Weekly Time Entry" / "Timesheet" / "Payroll" -> "TS"
    * "Parkway Medical Group E&D Waiver Supervisory Visit Form" or any
      header containing "E&D Waiver" -> "E&D"
  Use ONLY the printed title/header to decide; ignore service-type words
  (e.g. "In Home Respite", "PCS") that appear inside date rows or notes —
  those are service codes logged within the sheet, not the form type.

- full_name:
    * For PCS / IHR / Trans / E&D forms: return the name from the
      "Client Name:" (or "Patient Name:") field. These people are CLIENTS.
    * For TS forms: return the name from the "Employee Name:" field.
      Do NOT use any "Client Name:", "WEEK 1 Client Name:", or similar field
      — those are client names and must be ignored for TS forms.
    * CRITICAL: Transcribe the name EXACTLY as it is written on the form.
      Do NOT substitute a different name, invent letters that are not visible,
      or reorder the words. When a letter is ambiguous, write your best reading
      of it — never return null simply because the handwriting is difficult.
      Only remove stray non-letter characters (digits, punctuation) caused by
      scan noise; capitalise each word.
    * Return null ONLY if the relevant name field is entirely blank.

- dates:
    * Every service date that is filled in on the form (skip blank/empty rows).
    * Read the year DIRECTLY from the year portion of each individual date field.
      Do NOT default to the current year or infer the year from context.
    * ISO format: "YYYY-MM-DD".  Expand two-digit years with 2000
      (e.g. "26" -> 2026, "24" -> 2024).
    * Handwriting caution: dates are written M-D-YY. The day part may be
      1 OR 2 digits (e.g. "1", "23", "31"). Read each digit separately and
      carefully — "31" is NOT "3" followed by a stray mark; "1" as a day means
      the 1st of the month, not the 2nd. When uncertain between two adjacent
      digit values (e.g. "1" vs "2"), prefer the lower one for day numbers,
      since writers tend to make small digits look larger than they are.
    * On PCS Activity Sheets the form prints day-of-week labels (Sunday …
      Saturday) in order; dates in consecutive filled rows should be
      approximately sequential. Use this as a sanity check on what you read.
    * Sort ascending, deduplicate.
    * Return [] if none are legible.

Return ONLY the JSON object, nothing else.
"""

_ROSTER_HINT = """\

- rosters: Two lists below name people known to appear on these forms.
  Match the file_type to the right list and prefer the EXACT spelling and
  capitalisation from that list when the handwriting is plausibly one of its
  entries (same starting letter(s), similar length, consistent strokes).
  Do not invent variants. If nothing in the relevant list is a reasonable
  match, transcribe what you actually see.

  Employees (use ONLY when file_type is "TS"):
  {employees_list}

  Clients (use ONLY when file_type is "PCS", "IHR", "Trans", or "E&D"):
  {clients_list}
"""

_YEAR_HINT_SINGLE = """\

- expected service year: {year}.
  When the year digits on the form are ambiguous (handwritten "26" is often
  confused with "20", "28", "23"), prefer {year}. ONLY override this default
  when you can clearly read a different and plausible year on the page (e.g.
  a December {prior} form on a Dec→Jan bi-weekly timesheet).
"""

_YEAR_HINT_RANGE = """\

- expected service years: {low}-{high}.
  When the year digits on the form are ambiguous, prefer years in this range.
  ONLY override when you can clearly read a different and plausible year.
"""

_MONTH_HINT = """\

- expected service months: {months}.
  When the month digit is ambiguous (handwritten 1/7, 4/9, 3/8, 5/6 are
  commonly confused), prefer a month in this list. ONLY override when you
  can clearly read a different and plausible month on the page.
"""


def build_prompt(
    employees: List[str] = (),
    clients: List[str] = (),
    year_range: "tuple[int, int] | None" = None,
    months: "list[int] | None" = None,
) -> str:
    """Return the user prompt with roster + year + month hints when provided."""
    out = USER_PROMPT
    if employees or clients:
        out += _ROSTER_HINT.format(
            employees_list=", ".join(f'"{n}"' for n in employees) or "(none provided)",
            clients_list=", ".join(f'"{n}"' for n in clients) or "(none provided)",
        )
    if year_range is not None:
        lo, hi = year_range
        if lo == hi:
            out += _YEAR_HINT_SINGLE.format(year=lo, prior=lo - 1)
        else:
            out += _YEAR_HINT_RANGE.format(low=lo, high=hi)
    if months:
        out += _MONTH_HINT.format(months=", ".join(str(m) for m in months))
    return out


# ---------------------------------------------------------------------------
# Shared response parsing
# ---------------------------------------------------------------------------


def _parse_json_block(text: str) -> Dict[str, Any]:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```\s*$", text, re.S)
    if fence:
        text = fence.group(1)
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


def _to_dates(date_strings: List[str]) -> List[date]:
    out: List[date] = []
    for s in date_strings or []:
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", str(s).strip())
        if not m:
            continue
        try:
            out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            continue
    return sorted(set(out))


def _label_from_data(data: Dict[str, Any], source_path: str) -> FileLabel:
    ftype = data.get("file_type") or ""
    if ftype not in VALID_TYPES:
        ftype = ""
    name = (data.get("full_name") or "").strip()
    name, _ = snap_to_roster(name, ftype)
    dates = _to_dates(data.get("dates") or [])
    return FileLabel(
        full_name=name,
        file_type=ftype,
        dates=dates,
        source_path=source_path,
    )


# ---------------------------------------------------------------------------
# Gemini (Google AI Studio) -- free tier
# ---------------------------------------------------------------------------


def _analyze_gemini(pdf_path: str, settings: Dict[str, Any]) -> FileLabel:
    try:
        import google.generativeai as genai
    except ImportError as e:
        raise ImportError(
            "The 'google-generativeai' package is required for the Gemini reader.\n"
            "Install with:  pip install google-generativeai"
        ) from e

    api_key = settings.get("gemini_api_key", "").strip()
    if not api_key:
        raise RuntimeError("No Gemini API key set. Open Settings and paste yours.")

    genai.configure(api_key=api_key)
    model_name = settings.get("gemini_model") or DEFAULT_SETTINGS["gemini_model"]
    model = genai.GenerativeModel(model_name, system_instruction=SYSTEM_PROMPT)

    with open(pdf_path, "rb") as fh:
        pdf_bytes = fh.read()

    prompt = build_prompt(
        load_known_names(),
        load_known_clients(),
        parse_year_range(settings.get("expected_years", "")),
        parse_month_set(settings.get("expected_months", "")),
    )
    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(3):
        try:
            response = model.generate_content(
                [
                    {"mime_type": "application/pdf", "data": pdf_bytes},
                    prompt,
                ]
            )
            break
        except Exception as e:
            last_exc = e
            err_name = type(e).__name__
            err_str = str(e)
            is_rate_limit = (
                "ResourceExhausted" in err_name
                or "429" in err_str
                or "quota" in err_str.lower()
            )
            if is_rate_limit and attempt < 2:
                time.sleep(15 * (attempt + 1))
                continue
            raise
    else:
        raise last_exc
    raw = (response.text or "").strip()
    data = _parse_json_block(raw)
    return _label_from_data(data, pdf_path)


# ---------------------------------------------------------------------------
# Claude (Anthropic) -- paid
# ---------------------------------------------------------------------------


def _analyze_claude(pdf_path: str, settings: Dict[str, Any]) -> FileLabel:
    try:
        import anthropic
    except ImportError as e:
        raise ImportError(
            "The 'anthropic' package is required for the Claude reader.\n"
            "Install with:  pip install anthropic"
        ) from e

    api_key = settings.get("claude_api_key", "").strip()
    if not api_key:
        raise RuntimeError("No Claude API key set. Open Settings and paste yours.")

    client = anthropic.Anthropic(api_key=api_key)
    model_name = settings.get("claude_model") or DEFAULT_SETTINGS["claude_model"]

    with open(pdf_path, "rb") as fh:
        pdf_b64 = base64.standard_b64encode(fh.read()).decode("ascii")

    resp = client.messages.create(
        model=model_name,
        max_tokens=800,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": build_prompt(
                            load_known_names(),
                            load_known_clients(),
                            parse_year_range(settings.get("expected_years", "")),
                            parse_month_set(settings.get("expected_months", "")),
                        ),
                    },
                ],
            }
        ],
    )
    text_blocks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    raw = "\n".join(text_blocks).strip()
    data = _parse_json_block(raw)
    return _label_from_data(data, pdf_path)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def analyze_pdf(pdf_path: str, settings: Dict[str, Any]) -> FileLabel:
    provider = settings.get("provider", "gemini")
    if provider == "gemini":
        return _analyze_gemini(pdf_path, settings)
    if provider == "claude":
        return _analyze_claude(pdf_path, settings)
    raise RuntimeError(f"AI provider '{provider}' is not one of 'gemini' or 'claude'.")
