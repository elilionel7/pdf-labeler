"""
Filename formatter implementing the naming protocol.

  Client-Based (PCS, IHR, Trans):
      Firstname Lastname_Type_Date
  Employee-Based (TS):
      Firstname Lastname_TS_Date

  Date formatting:
      Single day           -> M-D-YY          (2-23-26)
      Multiple days / 1 mo -> M-D1,D2,Dn-YY   (2-23,24,26-26)
      Multiple months      -> M1-D1,D2_M2-D1,D2-YY (2-23,24_3-1,2-26)
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, List


VALID_TYPES = {"PCS", "IHR", "Trans", "TS"}


@dataclass
class FileLabel:
    """Structured result of parsing one PDF."""
    full_name: str = ""
    file_type: str = ""
    dates: List[date] = field(default_factory=list)
    source_path: str = ""

    def filename(self, extension: str = ".pdf") -> str:
        return build_filename(self.full_name, self.file_type, self.dates, extension)


def clean_name(raw: str) -> str:
    if not raw:
        return ""
    parts = re.split(r"\s+", raw.strip())
    parts = [p.capitalize() for p in parts if p]
    return " ".join(parts)


def _two_digit_year(d: date) -> str:
    return f"{d.year % 100:02d}"


def _group_by_month(dates: Iterable[date]):
    dates = sorted(set(dates))
    groups: "OrderedDict[tuple[int,int], list[int]]" = OrderedDict()
    for d in dates:
        key = (d.year, d.month)
        groups.setdefault(key, []).append(d.day)
    for key in groups:
        groups[key] = sorted(set(groups[key]))
    return groups


def format_dates(dates: List[date]) -> str:
    if not dates:
        raise ValueError("At least one date is required")

    groups = _group_by_month(dates)

    years = {y for (y, _m) in groups.keys()}
    if len(years) > 1:
        raise ValueError(f"All dates must share one year; got years {sorted(years)}")
    year = _two_digit_year(date(next(iter(years)), 1, 1))

    if len(groups) == 1:
        (_y, m), days = next(iter(groups.items()))
        if len(days) == 1:
            return f"{m}-{days[0]}-{year}"
        return f"{m}-{','.join(str(d) for d in days)}-{year}"

    month_parts = [
        f"{m}-{','.join(str(d) for d in days)}"
        for (_y, m), days in groups.items()
    ]
    return f"{'_'.join(month_parts)}-{year}"


def build_filename(full_name: str, file_type: str, dates: List[date],
                   extension: str = ".pdf") -> str:
    if file_type not in VALID_TYPES:
        raise ValueError(f"Invalid file type {file_type!r}; must be one of {sorted(VALID_TYPES)}")
    name = clean_name(full_name)
    if not name:
        raise ValueError("A name is required")
    if not extension.startswith("."):
        extension = "." + extension
    return f"{name}_{file_type}_{format_dates(dates)}{extension}"


_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def sanitize(filename: str) -> str:
    return _ILLEGAL_CHARS.sub("-", filename).strip().rstrip(".")
