"""
Reads scanned (handwritten) PDFs by sending them to the Claude API.

Claude's vision models are very good at cursive/handwriting — far better
than Tesseract on the kinds of forms this app processes.

Setup:
    pip install anthropic
    export ANTHROPIC_API_KEY=sk-ant-...

The API key can also be supplied explicitly via `set_api_key()` (the GUI
uses this when the user pastes a key into the Settings dialog).
"""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import date
from typing import List, Optional

from formatter import FileLabel, VALID_TYPES

# --- API key plumbing -----------------------------------------------------

# Module-level override that beats the environment variable.
_API_KEY_OVERRIDE: Optional[str] = None

# The cheapest, fastest vision-capable model is fine for reading forms.
DEFAULT_MODEL = "claude-haiku-4-5"


def set_api_key(key: Optional[str]) -> None:
    """Set (or clear) the API key at runtime; wins over ANTHROPIC_API_KEY."""
    global _API_KEY_OVERRIDE
    _API_KEY_OVERRIDE = key.strip() if key else None


def get_api_key() -> Optional[str]:
    return _API_KEY_OVERRIDE or os.environ.get("ANTHROPIC_API_KEY")


def is_available() -> bool:
    """True if both the SDK is installed and we have an API key."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return bool(get_api_key())


# --- Prompt used for extraction ------------------------------------------

_SYSTEM_PROMPT = (
    "You read scanned service/time-entry forms from home-care agencies. "
    "Many fields are handwritten. Extract structured data precisely and return JSON."
)

_USER_PROMPT = """\
This PDF is a scanned form. Extract the following fields and return **only** JSON
(no prose, no markdown fences, no comments):

{
  "file_type": "PCS" | "IHR" | "Trans" | "TS" | null,
  "full_name": "Firstname Lastname" | null,
  "dates":     ["YYYY-MM-DD", ...]
}

Rules:
- `file_type` mapping:
    * "Personal Care Services" / "PCS Activity Sheet" / "PCS Notes" -> "PCS"
    * "In-Home Respite" / "Respite Care" -> "IHR"
    * "Transportation" / "Trip Log" / "Mileage Log" -> "Trans"
    * "Timesheet" / "Bi-Weekly Time Entry" / "Payroll" / "Employee Time" -> "TS"
  If the form clearly states a PCS/IHR/Trans service, prefer that over "TS"
  even if the word "timesheet" appears.

- `full_name`:
    * For PCS/IHR/Trans forms, return the **Client** name.
    * For TS forms, return the **Employee** name.
    * If two names appear (e.g. both client and employee), still return
      the one consistent with the rule above.
    * Clean up obvious OCR artefacts. Capitalise the first letter of each part.
    * If unreadable, return null.

- `dates`:
    * Every service date that appears filled in on the form (not blank rows).
    * Format each as an ISO date string (e.g. "2026-03-24").
    * Assume 20YY for two-digit years.
    * Sort ascending, dedupe.
    * If none are legible, return [].

Return ONLY the JSON object, nothing else.
"""


# --- PDF -> API call ------------------------------------------------------

def _encode_pdf(pdf_path: str) -> str:
    with open(pdf_path, "rb") as fh:
        return base64.standard_b64encode(fh.read()).decode("ascii")


def _parse_json_block(text: str) -> dict:
    """Be forgiving: strip markdown fences if the model added any."""
    text = text.strip()
    # Remove ```json ... ``` fences
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```\s*$", text, re.S)
    if fence:
        text = fence.group(1)
    # If there's prose around a JSON object, find the outermost braces.
    if not text.startswith("{"):
        start = text.find("{")
        end   = text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
    return json.loads(text)


def _to_dates(date_strings: List[str]) -> List[date]:
    out: List[date] = []
    for s in date_strings or []:
        s = str(s).strip()
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
        if not m:
            continue
        y, mo, dy = (int(x) for x in m.groups())
        try:
            out.append(date(y, mo, dy))
        except ValueError:
            continue
    # Dedupe while preserving sorted order
    return sorted(set(out))


def analyze_pdf(pdf_path: str, *, model: str = DEFAULT_MODEL) -> FileLabel:
    """
    Send a PDF to Claude and return a structured FileLabel.

    Raises:
        ImportError  if the `anthropic` package isn't installed.
        RuntimeError if no API key is set.
        Any anthropic.* API errors are propagated up so the UI can surface them.
    """
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "The 'anthropic' package is required for Claude vision. "
            "Install with:  pip install anthropic"
        ) from e

    api_key = get_api_key()
    if not api_key:
        raise RuntimeError(
            "No Anthropic API key set. Either export ANTHROPIC_API_KEY "
            "or paste a key in Settings."
        )

    client = anthropic.Anthropic(api_key=api_key)
    pdf_b64 = _encode_pdf(pdf_path)

    resp = client.messages.create(
        model=model,
        max_tokens=400,
        system=_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type":       "base64",
                        "media_type": "application/pdf",
                        "data":       pdf_b64,
                    },
                },
                {"type": "text", "text": _USER_PROMPT},
            ],
        }],
    )

    # Concatenate all text blocks of the assistant response.
    text_blocks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    raw = "\n".join(text_blocks).strip()
    data = _parse_json_block(raw)

    ftype = data.get("file_type") or ""
    if ftype not in VALID_TYPES:
        ftype = ""

    name = (data.get("full_name") or "").strip()
    dates = _to_dates(data.get("dates") or [])

    return FileLabel(
        full_name=name,
        file_type=ftype,
        dates=dates,
        source_path=pdf_path,
    )
