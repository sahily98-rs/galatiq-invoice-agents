"""Tools used by the agents: document reading, structured parsing,
inventory lookups, fraud-signal detection, and the mock payment API."""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple


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


def parse_structured(path: str) -> Optional[Dict[str, Any]]:
    """Natively parse JSON / CSV / XML invoices into a normalized dict.

    Returns None for free-text formats (txt/pdf), signalling the caller to
    use the LLM/deterministic extraction path instead.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return _normalize_json(raw)
    if ext == ".csv":
        with open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        return _normalize_rows(rows)
    if ext == ".xml":
        tree = ET.parse(path)
        return _normalize_xml(tree.getroot())
    return None


def _money(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def _normalize_json(raw: Dict[str, Any]) -> Dict[str, Any]:
    vendor = raw.get("vendor")
    if isinstance(vendor, dict):
        vendor = vendor.get("name") or ""
    items = []
    for li in raw.get("line_items", raw.get("items", [])):
        items.append({
            "item": str(li.get("item", li.get("name", ""))),
            "quantity": int(li.get("quantity", 0)),
            "unit_price": _money(li.get("unit_price", li.get("price"))),
        })
    return {
        "vendor": vendor or "",
        "invoice_number": str(raw.get("invoice_number", raw.get("invoice_id", ""))),
        "due_date": raw.get("due_date"),
        "total_amount": _money(raw.get("total", raw.get("total_amount", raw.get("amount")))),
        "items": items,
        "_warnings": [],
        "_confidence": 0.95,
    }


def _norm_key(k: str) -> str:
    return re.sub(r"[\s_\-]+", "_", k.strip().lower())


def _normalize_rows(rows: List[Dict[str, str]]) -> Dict[str, Any]:
    # Detect vertical "field,value" layout (e.g. invoice_1006.csv) and transpose it.
    if rows and {_norm_key(k) for k in rows[0].keys()} == {"field", "value"}:
        kv = {_norm_key(r.get("field", "")): (r.get("value") or "") for r in rows}
        rows = [kv]

    def pick(row: Dict[str, str], *names: str) -> str:
        low = {_norm_key(k): v for k, v in row.items()}
        for n in names:
            if n in low:
                return low[n] or ""
        return ""

    items = []
    for row in rows:
        name = pick(row, "item", "product", "name")
        if not name:
            continue
        qty_raw = pick(row, "quantity", "qty")
        try:
            qty = int(float(qty_raw)) if qty_raw else 0
        except ValueError:
            qty = 0
        items.append({
            "item": name,
            "quantity": qty,
            "unit_price": _money(pick(row, "unit_price", "price")),
        })
    first = rows[0] if rows else {}
    total = _money(pick(first, "total", "total_amount", "amount", "line_total"))
    computed = sum((i["quantity"] or 0) * (i["unit_price"] or 0) for i in items)
    return {
        "vendor": pick(first, "vendor", "vendor_name"),
        "invoice_number": pick(first, "invoice_number", "invoice", "inv", "inv_no"),
        "due_date": pick(first, "due_date", "due") or None,
        "total_amount": total if total else (computed or None),
        "items": items,
        "_warnings": [],
        "_confidence": 0.95,
    }


def _normalize_xml(root: ET.Element) -> Dict[str, Any]:
    def text(tag: str) -> str:
        el = root.find(f".//{tag}")
        return (el.text or "").strip() if el is not None and el.text else ""

    items = []
    for li in root.findall(".//line_item") + root.findall(".//item"):
        name_el = li.find("name")
        name = (name_el.text or "").strip() if name_el is not None and name_el.text else (li.get("name") or "")
        if not name:
            continue
        qty_el = li.find("quantity")
        price_el = li.find("unit_price")
        items.append({
            "item": name,
            "quantity": int(float((qty_el.text or "0") if qty_el is not None else li.get("quantity", 0))),
            "unit_price": _money(price_el.text if price_el is not None else li.get("unit_price")),
        })
    return {
        "vendor": text("vendor") or text("vendor_name"),
        "invoice_number": text("invoice_number") or text("invoice_id"),
        "due_date": text("due_date") or None,
        "total_amount": _money(text("total") or text("total_amount")),
        "items": items,
        "_warnings": [],
        "_confidence": 0.95,
    }


# ------------------------------------------------------------------ inventory

class InventoryDB:
    """SQLite-backed mock inventory database for the validation agent."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def lookup(self, item_name: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT item, stock FROM inventory WHERE lower(item) = lower(?)", (item_name,))
        row = cur.fetchone()
        if row:
            return row
        # tolerant fallback: substring match either direction
        cur = self.conn.execute("SELECT item, stock FROM inventory")
        for r in cur.fetchall():
            a, b = r["item"].lower(), item_name.lower()
            if a in b or b in a:
                return r
        return None


def create_inventory_db(db_path: str, seed: Optional[List[Tuple[str, int]]] = None) -> str:
    """Create the mock inventory database with starter seed data."""
    seed = seed or [("WidgetA", 15), ("WidgetB", 10), ("GadgetX", 5), ("FakeItem", 0)]
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("CREATE TABLE inventory (item TEXT PRIMARY KEY, stock INTEGER)")
    cur.executemany("INSERT INTO inventory VALUES (?, ?)", seed)
    conn.commit()
    conn.close()
    return db_path


# ------------------------------------------------------------- fraud signals

FRAUD_KEYWORDS = [
    "urgent", "immediately", "wire transfer", "penalty", "penalties",
    "asap", "do not delay", "confidential", "secret payment",
]

def detect_fraud_signals(raw_text: str) -> List[str]:
    lowered = raw_text.lower()
    return sorted({kw for kw in FRAUD_KEYWORDS if kw in lowered})


# ------------------------------------------------------------------- payment

def mock_payment(vendor: str, amount: Optional[float]) -> Dict[str, Any]:
    """Mock payment API, as specified in the challenge."""
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}
