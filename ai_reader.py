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

DEFAULT_SETTINGS: Dict[str, Any] = {
    "provider":  "gemini",     # "gemini" | "claude" | "tesseract"
    "gemini_api_key": "",
    "gemini_model": "gemini-2.5-flash",
    "claude_api_key": "",
    "claude_model": "claude-haiku-4-5",
}


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
        settings["gemini_api_key"] = (
            os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        )
    if os.environ.get("ANTHROPIC_API_KEY"):
        settings["claude_api_key"] = os.environ["ANTHROPIC_API_KEY"]
    return settings


def save_settings(settings: Dict[str, Any]) -> None:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    with SETTINGS_FILE.open("w") as fh:
        json.dump(settings, fh, indent=2)
    try:
        os.chmod(SETTINGS_FILE, 0o600)   # API keys are sensitive
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
  "file_type": "PCS" | "IHR" | "Trans" | "TS" | null,
  "full_name": "Firstname Lastname" | null,
  "dates":     ["YYYY-MM-DD", ...]
}

Rules:
- file_type — determined ONLY by the form's title / header line:
    * "PCS Activity Sheet" / "PCS Notes" / "Personal Care Services" -> "PCS"
    * "In-Home Respite" / "Respite Care" -> "IHR"
    * "Transportation" / "Trip Log" / "Mileage Log" -> "Trans"
    * "Employee Bi-Weekly Time Entry" / "Timesheet" / "Payroll" -> "TS"
  Use ONLY the printed title/header to decide; ignore service-type words
  (e.g. "In Home Respite", "PCS") that appear inside date rows or notes —
  those are service codes logged within the sheet, not the form type.

- full_name:
    * For PCS/IHR/Trans forms: return the name from the "Client Name:" field.
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
        end   = text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
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

    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(3):
        try:
            response = model.generate_content([
                {"mime_type": "application/pdf", "data": pdf_bytes},
                USER_PROMPT,
            ])
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
        messages=[{
            "role": "user",
            "content": [
                {"type": "document",
                 "source": {"type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64}},
                {"type": "text", "text": USER_PROMPT},
            ],
        }],
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
    raise RuntimeError(
        f"AI provider '{provider}' is not one of 'gemini' or 'claude'."
    )
