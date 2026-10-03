"""Tools used by the agents: document reading, structured parsing,
inventory lookups, risk-signal analysis, and the mock payment API."""
from __future__ import annotations

import csv
import datetime
import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from . import dates as dateparse
from .models import RiskSignal


# ---------------------------------------------------------------- document I/O

def read_document(path: str) -> str:
    """Extract raw text from a document, dispatching on file extension."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _money(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").replace("€", "").strip())
    except (ValueError, TypeError):
        return None


def _norm_key(k: str) -> str:
    return re.sub(r"[\s_\-]+", "_", k.strip().lower())


# ------------------------------------------------------- structured parsing

def parse_structured(path: str) -> Optional[Dict[str, Any]]:
    """Natively parse JSON / CSV / XML invoices into a normalized dict.

    Returns None for free-text formats (txt/pdf), signalling the caller to
    use the LLM/deterministic extraction path instead.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        with open(path, encoding="utf-8") as f:
            return _normalize_json(json.load(f))
    if ext == ".csv":
        with open(path, encoding="utf-8", newline="") as f:
            return _normalize_csv(list(csv.DictReader(f)))
    if ext == ".xml":
        return _normalize_xml(ET.parse(path).getroot())
    return None


def _normalize_json(raw: Dict[str, Any]) -> Dict[str, Any]:
    vendor = raw.get("vendor")
    if isinstance(vendor, dict):
        vendor = vendor.get("name") or ""
    items = []
    for li in raw.get("line_items", raw.get("items", [])):
        items.append({
            "item": str(li.get("item", li.get("name", ""))),
            "quantity": _qty(li.get("quantity", 0)),
            "unit_price": _money(li.get("unit_price", li.get("price"))),
        })
    curr_raw = json.dumps(raw)
    return {
        "vendor": sanitize_vendor(vendor or ""),
        "invoice_number": str(raw.get("invoice_number", raw.get("invoice_id", ""))),
        "revision": str(raw.get("revision", raw.get("rev", "") or "")).strip() or None,
        "invoice_date": _norm_date(raw.get("date")),
        "due_date": _norm_date(raw.get("due_date")),
        "total_amount": _money(raw.get("total", raw.get("total_amount", raw.get("amount")))),
        "subtotal": _money(raw.get("subtotal")),
        "tax_amount": _money(raw.get("tax_amount", raw.get("tax"))),
        "shipping_amount": _money(raw.get("shipping_amount", raw.get("shipping"))),
        "currency": parse_currency(curr_raw) or "USD",
        "currency_explicit": parse_currency(curr_raw) is not None,
        "items": items,
        "_warnings": [],
        "_confidence": 0.95,
    }


def _qty(v: Any) -> int:
    """Parse a quantity strictly: non-numeric or malformed -> raise, so the
    caller can route to human review instead of silently coercing to 0."""
    if isinstance(v, bool):
        raise ValueError(f"non-numeric quantity: {v!r}")
    if isinstance(v, (int, float)):
        if isinstance(v, float) and not v.is_integer():
            raise ValueError(f"fractional quantity: {v!r}")
        return int(v)
    s = str(v).strip().replace(",", "")
    if not re.fullmatch(r"-?\d+", s):
        raise ValueError(f"non-numeric quantity: {v!r}")
    return int(s)


def _normalize_csv(rows: List[Dict[str, str]]) -> Dict[str, Any]:
    rows = [{_norm_key(k): (v or "") for k, v in r.items()} for r in rows]

    # Vertical "field,value" layout -> collect repeated item groups.
    if rows and set(rows[0].keys()) == {"field", "value"}:
        return _normalize_csv_vertical(rows)
    return _normalize_csv_horizontal(rows)


def _normalize_csv_vertical(rows: List[Dict[str, str]]) -> Dict[str, Any]:
    header: Dict[str, str] = {}
    items: List[Dict[str, Any]] = []
    cur: Dict[str, Any] = {}
    for r in rows:
        k, v = r["field"], r["value"]
        if k in ("item", "quantity", "unit_price", "price"):
            cur[{"item": "item", "quantity": "quantity"}.get(k, "unit_price")] = v
            if len(cur) == 3 or k in ("unit_price", "price"):
                # flush on a complete triple; tolerant of missing price
                if "item" in cur:
                    items.append({
                        "item": cur["item"],
                        "quantity": _qty(cur.get("quantity", 0)),
                        "unit_price": _money(cur.get("unit_price")),
                    })
                cur = {}
        else:
            header[k] = v
    if cur and "item" in cur:  # trailing partial group
        items.append({"item": cur["item"], "quantity": _qty(cur.get("quantity", 0)),
                      "unit_price": _money(cur.get("unit_price"))})
    return {
        "vendor": sanitize_vendor(header.get("vendor", "")),
        "invoice_number": header.get("invoice_number", header.get("invoice", "")),
        "invoice_date": _norm_date(header.get("date")),
        "due_date": _norm_date(header.get("due_date", header.get("due"))),
        "total_amount": _money(header.get("total")),
        "subtotal": _money(header.get("subtotal")),
        "tax_amount": _money(header.get("tax")),
        "shipping_amount": _money(header.get("shipping")),
        "currency": "USD",
        "items": items,
        "_warnings": [],
        "_confidence": 0.95,
    }


def _normalize_csv_horizontal(rows: List[Dict[str, str]]) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    subtotal = tax = shipping = total = None
    first = rows[0] if rows else {}
    for r in rows:
        name = (r.get("item") or r.get("product") or r.get("name") or "").strip()
        if not name:
            # footer rows: Subtotal: / Tax (6%): / Total:
            label = " ".join(v for v in r.values() if v).lower()
            if "subtotal" in label:
                subtotal = _money(_last_money_cell(r))
            elif "tax" in label:
                tax = _money(_last_money_cell(r))
            elif re.search(r"(^|[\s,])total\s*:", label):
                total = _money(_last_money_cell(r))
            continue
        items.append({
            "item": name,
            "quantity": _qty(r.get("quantity", r.get("qty", 0))),
            "unit_price": _money(r.get("unit_price", r.get("price"))),
        })
    # Never mistake a per-line total for the invoice total.
    computed = sum((i["quantity"] or 0) * (i["unit_price"] or 0) for i in items)
    return {
        "vendor": sanitize_vendor(first.get("vendor", first.get("vendor_name", ""))),
        "invoice_number": first.get("invoice_number", first.get("invoice", "")),
        "invoice_date": _norm_date(first.get("date")),
        "due_date": _norm_date(first.get("due_date", first.get("due"))),
        "total_amount": total if total else (computed or None),
        "subtotal": subtotal,
        "tax_amount": tax,
        "shipping_amount": shipping,
        "currency": "USD",
        "items": items,
        "_warnings": [],
        "_confidence": 0.95,
    }


def _last_money_cell(row: Dict[str, str]) -> str:
    for v in reversed(list(row.values())):
        if v and re.search(r"\d", v):
            return v
    return ""


def _normalize_xml(root: ET.Element) -> Dict[str, Any]:
    def text(*tags: str) -> str:
        for tag in tags:
            el = root.find(f".//{tag}")
            if el is not None and el.text and el.text.strip():
                return el.text.strip()
        return ""

    items = []
    for li in root.findall(".//line_item") + root.findall(".//item"):
        name_el = li.find("name")
        name = ((name_el.text or "").strip() if name_el is not None else "") or (li.get("name") or "")
        if not name:
            continue
        qty_el = li.find("quantity")
        price_el = li.find("unit_price")
        items.append({
            "item": name,
            "quantity": _qty(qty_el.text if qty_el is not None else li.get("quantity", 0)),
            "unit_price": _money(price_el.text if price_el is not None else li.get("unit_price")),
        })
    curr = text("currency") or "USD"
    return {
        "vendor": sanitize_vendor(text("vendor", "vendor_name")),
        "invoice_number": text("invoice_number", "invoice_id"),
        "invoice_date": _norm_date(text("date")),
        "due_date": _norm_date(text("due_date")),
        "total_amount": _money(text("total", "total_amount")),
        "subtotal": _money(text("subtotal")),
        "tax_amount": _money(text("tax_amount", "tax")),
        "shipping_amount": _money(text("shipping_amount", "shipping")),
        "currency": curr if len(curr) == 3 else "USD",
        "items": items,
        "_warnings": [],
        "_confidence": 0.95,
    }


def _norm_date(v: Any) -> Optional[str]:
    if not v:
        return None
    d = dateparse.parse_date(str(v))
    return dateparse.iso(d)


# ------------------------------------------------------------------ vendors

# Trailing fragments leaked in from two-column PDF layouts, e.g.
# "Atlas Industrial Supply Due: 2026-03-24".
_VENDOR_TRAILING_JUNK = re.compile(
    r"\s*(due|date|invoice)\s*:?\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}\s*$", re.IGNORECASE)


def sanitize_vendor(vendor: str) -> str:
    """Strip date-like fragments that leak into payee names from multi-column
    PDF extraction. Returns "" if nothing sane remains."""
    v = (vendor or "").strip()
    v = _VENDOR_TRAILING_JUNK.sub("", v).strip(" ,;:")
    # A payee that still contains a date fragment is garbled, not a name.
    if re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", v):
        return ""
    if len(v) < 2:
        return ""
    return v


# ------------------------------------------------------------------ currency

_CURRENCY_RE = re.compile(r"\b(USD|EUR|GBP|JPY|CAD|AUD|CHF)\b", re.IGNORECASE)


def parse_currency(text: str) -> Optional[str]:
    """Find an explicit ISO currency code; None if the document is silent."""
    if not text:
        return None
    if "€" in text:
        return "EUR"
    if "£" in text:
        return "GBP"
    m = _CURRENCY_RE.search(text)
    return m.group(1).upper() if m else None


# ------------------------------------------------------------------ inventory

class InventoryDB:
    """SQLite-backed mock inventory database.

    Matching is exact (after normalization: case/space/punctuation
    insensitive). Fuzzy substring matching is deliberately NOT used: paying
    the wrong SKU because "A" matched "WidgetA" is worse than asking a human.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def skus(self) -> List[str]:
        cur = self.conn.execute("SELECT item FROM inventory")
        return [r[0] for r in cur.fetchall()]

    def list_price(self, item: str) -> Optional[float]:
        cur = self.conn.execute("SELECT list_price FROM inventory WHERE item = ?",
                                (item,))
        row = cur.fetchone()
        return row[0] if row else None

    @staticmethod
    def normalize(name: str) -> str:
        return re.sub(r"[^a-z0-9]", "", name.lower())

    def lookup(self, item_name: str) -> Optional[sqlite3.Row]:
        norm = self.normalize(item_name)
        if not norm:
            return None
        cur = self.conn.execute("SELECT item, stock, list_price FROM inventory")
        for row in cur.fetchall():
            if self.normalize(row["item"]) == norm:
                return row
        return None


def create_inventory_db(db_path: str,
                        seed: Optional[List[Tuple[str, int, float]]] = None) -> str:
    """Create the mock inventory database with starter seed data."""
    seed = seed or [("WidgetA", 15, 250.0), ("WidgetB", 10, 500.0),
                    ("GadgetX", 5, 750.0), ("FakeItem", 0, 0.0)]
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("CREATE TABLE inventory (item TEXT PRIMARY KEY, stock INTEGER, list_price REAL)")
    cur.executemany("INSERT INTO inventory VALUES (?, ?, ?)", seed)
    conn.commit()
    conn.close()
    return db_path


# ------------------------------------------------------------- risk signals

def _word_re(*words: str) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.IGNORECASE)


_URGENCY = _word_re("urgent", "immediately", "asap", "right away")
_WIRE = _word_re("wire transfer", "wire")
_PENALTY = _word_re("penalt(?:y|ies)")
# Business-email-compromise patterns: a vendor changing where money goes is
# one of the highest-risk events in AP. These are general phrases, not
# sample-specific strings.
_BANK_CHANGE = re.compile(
    r"(?i)\b(bank details? (have |has )?(changed|updated)|new (bank )?account|"
    r"remit .* new account|updated? (bank|remittance) (details|info)|"
    r"change of account|our .* account has changed)\b")
_VENDOR_IDENTITY = re.compile(
    r"(?i)\b(formerly|previously|now known as|\bfka\b)")
_VAGUE_DUE = _word_re("upon receipt", "asap", "immediately")


def analyze_risk(raw_text: str, invoice_date: Optional[str],
                 due_date: Optional[str]) -> List[RiskSignal]:
    """Structured risk analysis. Word-boundary matching (not bare substring)
    plus date-based and vendor-based signals."""
    signals: List[RiskSignal] = []
    text = raw_text or ""

    urgency = bool(_URGENCY.search(text))
    wire = bool(_WIRE.search(text))
    penalty = bool(_PENALTY.search(text))

    if wire and urgency:
        signals.append(RiskSignal("wire_transfer_pressure", "critical",
                                  "Wire-transfer request combined with urgency language"))
    elif wire:
        signals.append(RiskSignal("wire_transfer_request", "suspicious",
                                  "Wire transfer requested without urgency markers"))
    if urgency and not wire:
        signals.append(RiskSignal("urgency_language", "suspicious",
                                  "Urgency language in invoice text"))
    # "penalty" alone is normal contract language; it matters with pressure.
    if penalty and (urgency or wire):
        signals.append(RiskSignal("penalty_threat", "suspicious",
                                  "Penalty threat combined with payment pressure"))

    # Bank-detail changes are the classic business-email-compromise move.
    m = _BANK_CHANGE.search(text)
    if m:
        signals.append(RiskSignal(
            "bank_details_changed", "critical",
            f"Remittance/bank details changed: {m.group(0)!r} — verify out of band"))

    inv_d = dateparse.parse_date(invoice_date) if invoice_date else None
    due_d = dateparse.parse_date(due_date, ref=inv_d) if due_date else None
    if inv_d and due_d:
        if due_d < inv_d:
            signals.append(RiskSignal("due_before_invoice", "critical",
                                      f"Due {due_d.isoformat()} is before invoice date "
                                      f"{inv_d.isoformat()}"))
        elif (due_d - inv_d).days <= 1:
            signals.append(RiskSignal("due_date_pressure", "suspicious",
                                      "Due within a day of the invoice date — "
                                      "no real payment terms"))
    elif due_date and _VAGUE_DUE.search(due_date):
        signals.append(RiskSignal("vague_due_date", "suspicious",
                                  f"Due date is vague ({due_date!r}), not a real date"))

    if _VENDOR_IDENTITY.search(text):
        signals.append(RiskSignal("vendor_name_change", "suspicious",
                                  "Vendor notes a former/changed name — verify identity "
                                  "(business-email-compromise pattern)"))

    return signals


# ------------------------------------------------------------------- payment

def mock_payment(vendor: str, amount: Optional[float],
                 currency: str = "USD") -> Dict[str, Any]:
    """Mock payment API, as specified in the challenge."""
    if amount is None:
        raise ValueError("cannot pay an invoice with no total amount")
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    print(f"Paid {symbol}{amount:,.2f} {currency} to {vendor}")
    return {"status": "success", "currency": currency}
