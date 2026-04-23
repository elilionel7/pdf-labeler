"""
Tesseract-based extractor: pulls file type, name, and dates out of the plain
text produced by ocr.py. This is used when no AI provider is configured.

Because scan templates vary, the patterns below are easy to adjust.
"""

from __future__ import annotations

import re
from datetime import date
from typing import List, Optional, Tuple

from formatter import FileLabel


# Client-service types are checked BEFORE timesheet so that a form titled
# "PCS Activity Sheet" doesn't accidentally match "Timesheet".
_TYPE_RULES: List[Tuple[str, re.Pattern]] = [
    ("PCS",   re.compile(r"\b(personal\s+care\s+services?|pcs(?:\s+(?:activity|notes))?)\b", re.I)),
    ("IHR",   re.compile(r"\b(in[-\s]*home\s+respite|respite\s+care|\bihr\b)\b", re.I)),
    ("Trans", re.compile(r"\b(transportation|transport\s+log|mileage\s+log|trip\s+log|\btrans\b)\b", re.I)),
    ("TS",    re.compile(r"\b(time\s*sheet|timesheet|employee\s+hours|pay\s*period|bi[-\s]*weekly\s+time\s+entry|payroll)\b", re.I)),
]


def detect_type(text: str) -> Optional[str]:
    for type_code, pattern in _TYPE_RULES:
        if pattern.search(text):
            return type_code
    return None


_NAME_LINE = re.compile(
    r"""
    (?:client|employee|member|consumer|participant|recipient|caregiver|worker|staff|name)
    \s*(?:name)?\s*[:\-]\s*
    (?P<name>[A-Z][A-Za-z'’\.\-]+(?:[ \t]+[A-Z][A-Za-z'’\.\-]+){1,3})
    """,
    re.I | re.X,
)


def extract_name(text: str, file_type: Optional[str] = None) -> Optional[str]:
    preferred_labels = {"TS": "employee", "PCS": "client",
                        "IHR": "client", "Trans": "client"}
    preferred = preferred_labels.get(file_type or "", None)

    matches = list(_NAME_LINE.finditer(text))
    if matches:
        if preferred:
            for m in matches:
                if preferred in m.group(0).lower():
                    return m.group("name").strip()
        return matches[0].group("name").strip()

    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^([A-Z][a-z'’\.\-]+(?:\s+[A-Z][a-z'’\.\-]+){1,3})\s*$", line)
        if m:
            return m.group(1).strip()
    return None


_MONTH_MAP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_NUMERIC_DATE = re.compile(
    r"\b(?P<m>0?[1-9]|1[0-2])[/\-\.](?P<d>0?[1-9]|[12]\d|3[01])[/\-\.](?P<y>\d{2}|\d{4})\b"
)

_TEXT_DATE = re.compile(
    r"\b(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+"
    r"(?P<d>0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?,?\s+(?P<y>\d{2,4})\b",
    re.I,
)


def _normalize_year(y: str) -> int:
    y = int(y)
    return 2000 + y if y < 100 else y


def extract_dates(text: str) -> List[date]:
    found: List[date] = []

    for m in _NUMERIC_DATE.finditer(text):
        try:
            found.append(date(_normalize_year(m.group("y")),
                              int(m.group("m")),
                              int(m.group("d"))))
        except (ValueError, KeyError):
            continue

    for m in _TEXT_DATE.finditer(text):
        mon = _MONTH_MAP.get(m.group("mon").lower().rstrip("."))
        if not mon:
            continue
        try:
            found.append(date(_normalize_year(m.group("y")),
                              mon,
                              int(m.group("d"))))
        except ValueError:
            continue

    seen = set()
    unique: List[date] = []
    for d in found:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    return unique


def analyze(text: str, source_path: str = "") -> FileLabel:
    ftype = detect_type(text)
    name  = extract_name(text, ftype)
    dates = extract_dates(text)
    return FileLabel(
        full_name=name or "",
        file_type=ftype or "",
        dates=dates,
        source_path=source_path,
    )
