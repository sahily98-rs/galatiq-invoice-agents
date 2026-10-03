"""Date parsing and normalization for invoice dates.

Handles the formats seen in the wild (ISO, "Jan 30 2026", "26-Jan-2026",
"01/28/2026", "February 26, 2026") plus relative words ("yesterday",
"today", "tomorrow") resolved against the invoice date.
"""
from __future__ import annotations

import datetime
import re
from typing import Optional

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_PATTERNS = [
    # 2026-02-01 / 2026/02/01
    (re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$"),
     lambda m: (int(m.group(1)), int(m.group(2)), int(m.group(3)))),
    # 01/28/2026 / 1/5/26
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$"),
     lambda m: (_full_year(m.group(3)), int(m.group(1)), int(m.group(2)))),
    # Jan 30 2026 / January 30, 2026
    (re.compile(r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$"),
     lambda m: (int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))),
    # 26-Jan-2026 / 26 Jan 2026
    (re.compile(r"^(\d{1,2})[- ]([A-Za-z]+)[- ](\d{4})$"),
     lambda m: (int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))),
]


def _full_year(y: str) -> int:
    y = y.strip()
    return int(y) if len(y) == 4 else 2000 + int(y)


def parse_date(text: Optional[str],
               ref: Optional[datetime.date] = None) -> Optional[datetime.date]:
    """Parse a date string to a date. Relative words resolve against ref."""
    if not text:
        return None
    s = text.strip().lower().rstrip(".")
    if ref is not None:
        if s in {"yesterday"}:
            return ref - datetime.timedelta(days=1)
        if s in {"today"}:
            return ref
        if s in {"tomorrow"}:
            return ref + datetime.timedelta(days=1)
    for pat, build in _PATTERNS:
        m = pat.match(text.strip())
        if not m:
            continue
        try:
            y, mo, d = build(m)
            return datetime.date(y, mo, d)
        except (ValueError, KeyError):
            return None
    return None


def iso(d: Optional[datetime.date]) -> Optional[str]:
    return d.isoformat() if d else None
