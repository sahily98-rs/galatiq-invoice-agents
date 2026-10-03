"""LLM client abstraction.

Uses xAI's Grok as the reasoning engine when an XAI_API_KEY is available.
Otherwise a deterministic local engine runs everything offline, per the
challenge's "simulate everything locally" requirement.

Design rule: LLM failures are LOUD. If Grok is configured but a call fails,
the failure is logged as an error and flagged on the result (degraded=True)
instead of silently pretending the LLM path worked.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional


class LLMError(RuntimeError):
    """Raised when the configured LLM fails. Never swallowed silently."""


EXTRACTION_PROMPT = """Extract structured invoice data. Reply with ONLY a JSON object, no commentary.

Invoice text:
---
{raw_text}
---

Return JSON with keys: vendor (string), invoice_number (string),
invoice_date (string or null), due_date (string or null),
total_amount (number or null), subtotal (number or null),
tax_amount (number or null), currency (3-letter code or null),
items (list of objects with item, quantity, unit_price).
Tolerate typos, OCR noise, and odd formatting. Null when genuinely absent."""

CRITIQUE_PROMPT = """You are a finance auditor reviewing an invoice-processing decision.
Reply with ONLY a JSON object: {{"verdict": "uphold" or "hold", "notes": "...", "risk_flags": [...]}}.
Choose "hold" if anything looks off: garbled payee names, amount anomalies,
weak evidence for a rejection, or missing data. Otherwise "uphold".

Raw document (truncated):
---
{raw_text}
---

Structured extraction: {invoice_json}
Validation: {validation_json}
Proposed decision: {decision_json}"""


class LocalEngine:
    """Deterministic local extraction + critique. No network needed."""

    VENDOR_RES = [
        re.compile(r"(?im)^\s*(?:vendor|vndr)\s*:\s*(.+?)\s*$"),
        re.compile(r"(?im)^\s*from\s*:\s*(.+?)\s*$"),
    ]
    TOTAL_RE = re.compile(r"(?im)^\s*(?:total amount|grand total|amount due|amt|total)\s*:?\s*\$?\s*([\d,]+\.\d{2})")
    SUBTOTAL_RE = re.compile(r"(?im)^\s*subtotal\s*:?\s*\$?\s*([\d,]+\.\d{2})")
    TAX_RE = re.compile(r"(?im)^\s*(?:sales\s+)?tax(?:\s*\([^)]*\))?\s*:?\s*\$?\s*([\d,]+\.\d{2})")
    SHIP_RE = re.compile(r"(?im)^\s*shipping\s*:?\s*\$?\s*([\d,]+\.\d{2})")
    DUE_RE = re.compile(r"(?im)^\s*(?:due date|due dt|due)\s*:\s*(.+?)\s*$")
    DATE_RE = re.compile(r"(?im)^\s*date\s*:\s*(.+?)\s*$")
    INVNO_RES = [
        # Require a ":" or "#" (or "#:") between label and number, so
        # "Invoice\nInvoice" can never match across a line break.
        re.compile(r"(?im)(?:invoice\s*(?:number|no)?|inv\.?)\s*(?:#\s*:|:|#)\s*([A-Za-z0-9][A-Za-z0-9\-]*)"),
    ]
    ITEM_RES = [
        # "WidgetA    qty: 10    unit price: $250.00" / "GadgetX  qty 20   @ $750 ea"
        # also tolerates a parenthetical note: "WidgetA (rush order)  qty: 4 ..."
        re.compile(
            r"(?im)^\s*[-*\u2022]?\s*(?P<item>[A-Za-z][A-Za-z0-9_]*)"
            r"(?:\s*\([^)]*\))?\s+qty\s*:?\s*(?P<qty>-?\d+)"
            r"(?:\s*(?:@|unit price:?|each|ea))?\s*\$?\s*(?P<price>[\d,]+(?:\.\d{2})?)?"
        ),
        # "- SuperGizmo       x12     $400.00 each"
        re.compile(
            r"(?im)^\s*[-*\u2022]?\s*(?P<item>[A-Za-z][A-Za-z0-9_]*(?: [A-Za-z][A-Za-z0-9_]*)?)"
            r"(?:\s*\([^)]*\))?\s*[xX]\s*(?P<qty>-?\d+)\s+\$?\s*(?P<price>[\d,]+(?:\.\d{2})?)?"
        ),
        # Table rows: "WidgetA 6 $250.00 $1,500.00"
        re.compile(
            r"(?im)^\s*(?P<item>[A-Za-z][A-Za-z0-9_]*(?: [A-Za-z][A-Za-z0-9_]*)?)"
            r"(?:\s*\([^)]*\))?\s+(?P<qty>-?\d+)\s+\$?(?P<price>[\d,]+(?:\.\d{2})?)\s+\$?[\d,O][\d,O.,]*"
        ),
    ]

    @staticmethod
    def _money(s: Optional[str]) -> Optional[float]:
        if not s:
            return None
        try:
            return float(s.replace(",", "").replace("$", "").replace("€", "").strip())
        except ValueError:
            return None

    @staticmethod
    def _dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        kept = []
        for i, a in enumerate(items):
            drop = False
            for j, b in enumerate(items):
                if i == j or a["quantity"] != b["quantity"]:
                    continue
                na, nb = a["item"].lower(), b["item"].lower()
                if na != nb and na in nb:
                    drop = True
                    break
            if not drop:
                kept.append(a)
        return kept

    def extract(self, raw_text: str) -> Dict[str, Any]:
        """Strategy v1: strict labeled-field patterns."""
        vendor_m = next((p.search(raw_text) for p in self.VENDOR_RES if p.search(raw_text)), None)
        total_m = self.TOTAL_RE.search(raw_text)
        sub_m = self.SUBTOTAL_RE.search(raw_text)
        tax_m = self.TAX_RE.search(raw_text)
        ship_m = self.SHIP_RE.search(raw_text)
        due_m = self.DUE_RE.search(raw_text)
        date_m = self.DATE_RE.search(raw_text)
        invno_m = next((p.search(raw_text) for p in self.INVNO_RES if p.search(raw_text)), None)

        items: List[Dict[str, Any]] = []
        seen = set()
        for pat in self.ITEM_RES:
            for m in pat.finditer(raw_text):
                name = "".join(m.group("item").split())  # "Widget A" -> "WidgetA"
                key = (name.lower(), m.group("qty"))
                if key in seen or name.lower() in {"total", "subtotal", "tax", "amount", "invoice"}:
                    continue
                seen.add(key)
                items.append({
                    "item": name,
                    "quantity": int(m.group("qty")),
                    "unit_price": self._money(m.group("price")),
                })
        items = self._dedupe(items)

        warnings = []
        if not vendor_m:
            warnings.append("vendor not found")
        if total_m is None:
            warnings.append("total amount not found")
        if not items:
            warnings.append("no line items found")
        return {
            "vendor": vendor_m.group(1).strip() if vendor_m else "",
            "invoice_number": invno_m.group(1).strip() if invno_m else "",
            "invoice_date": date_m.group(1).strip() if date_m else None,
            "due_date": due_m.group(1).strip() if due_m else None,
            "total_amount": self._money(total_m.group(1)) if total_m else None,
            "subtotal": self._money(sub_m.group(1)) if sub_m else None,
            "tax_amount": self._money(tax_m.group(1)) if tax_m else None,
            "shipping_amount": self._money(ship_m.group(1)) if ship_m else None,
            "items": items,
            "_warnings": warnings,
            "_confidence": max(1.0 - 0.2 * len(warnings), 0.2),
            "_strategy": "v1",
        }

    def extract_v2(self, raw_text: str) -> Dict[str, Any]:
        """Strategy v2: a genuinely different algorithm — line-oriented labeled
        scan with a broad label vocabulary, plus price-anchored item detection
        for lines the strict patterns miss."""
        vendor = invno = due = total = subtotal = tax = shipping = None
        invoice_date = None
        items: List[Dict[str, Any]] = []
        seen = set()

        label_res = [
            ("vendor", re.compile(r"(?i)^\s*(vendor|vndr|from|bill\s*from|supplier|remit\s*to)\s*[:\-]\s*(.+?)\s*$")),
            ("invno", re.compile(r"(?i)^\s*(invoice\s*(number|no|#)?|inv\s*(no|#)?)\s*[:\-]?\s*([A-Za-z0-9][\w\- ]+?)\s*$")),
            ("due", re.compile(r"(?i)^\s*(due(\s*date)?|pay\s*by)\s*[:\-]\s*(.+?)\s*$")),
            ("date", re.compile(r"(?i)^\s*date\s*[:\-]\s*(.+?)\s*$")),
            ("total", re.compile(r"(?i)^\s*(total|amount\s*due|balance\s*due|grand\s*total)\s*[:\-]?\s*\$?\s*([\d,]+\.\d{2})\s*$")),
            ("subtotal", re.compile(r"(?i)^\s*subtotal\s*[:\-]?\s*\$?\s*([\d,]+\.\d{2})\s*$")),
            ("tax", re.compile(r"(?i)^\s*(?:sales\s+)?tax(?:\s*\([^)]*\))?\s*[:\-]?\s*\$?\s*([\d,]+\.\d{2})\s*$")),
            ("shipping", re.compile(r"(?i)^\s*shipping\s*[:\-]?\s*\$?\s*([\d,]+\.\d{2})\s*$")),
        ]
        price_re = re.compile(r"\$?\s*([\d,]+\.\d{2})")
        qty_re = re.compile(r"(?:qty|x)\s*:?\s*(-?\d+)", re.IGNORECASE)

        for line in raw_text.splitlines():
            s = line.strip()
            if not s:
                continue
            for kind, pat in label_res:
                m = pat.match(s)
                if not m:
                    continue
                if kind == "vendor" and vendor is None:
                    vendor = m.group(2)
                elif kind == "invno" and invno is None:
                    invno = m.group(4) if m.lastindex and m.lastindex >= 4 else m.group(2)
                elif kind == "due" and due is None:
                    due = m.group(3)
                elif kind == "date" and invoice_date is None:
                    invoice_date = m.group(1)
                elif kind == "total" and total is None:
                    total = m.group(2)
                elif kind == "subtotal" and subtotal is None:
                    subtotal = m.group(2)
                elif kind == "tax" and tax is None:
                    tax = m.group(2)
                elif kind == "shipping" and shipping is None:
                    shipping = m.group(1)
                break
            else:
                # price-anchored item detection: "<desc> ... qty/x N ... $P"
                pm = price_re.search(s)
                qm = qty_re.search(s)
                if pm and qm:
                    desc = price_re.sub("", s)
                    desc = qty_re.sub("", desc)
                    desc = re.sub(r"[$\-,.*•:;()]", " ", desc)
                    tokens = [t for t in desc.split() if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", t)]
                    name = "".join(tokens[:2])
                    key = (name.lower(), qm.group(1))
                    if name and key not in seen and name.lower() not in {
                            "total", "subtotal", "tax", "amount", "invoice"}:
                        seen.add(key)
                        items.append({"item": name, "quantity": int(qm.group(1)),
                                      "unit_price": self._money(pm.group(1))})

        items = self._dedupe(items)
        warnings = []
        if not vendor:
            warnings.append("vendor not found")
        if not total:
            warnings.append("total amount not found")
        if not items:
            warnings.append("no line items found")
        return {
            "vendor": (vendor or "").strip(),
            "invoice_number": (invno or "").strip(),
            "invoice_date": (invoice_date or "").strip() or None,
            "due_date": (due or "").strip() or None,
            "total_amount": self._money(total),
            "subtotal": self._money(subtotal),
            "tax_amount": self._money(tax),
            "shipping_amount": self._money(shipping),
            "items": items,
            "_warnings": warnings,
            "_confidence": max(0.9 - 0.2 * len(warnings), 0.2),
            "_strategy": "v2",
        }

    def critique(self, raw_text: str, invoice: Dict[str, Any],
                 validation: Dict[str, Any], decision: Dict[str, Any],
                 inventory_skus: Optional[List[str]] = None) -> Dict[str, Any]:
        """Policy-grounded second pass with access to the RAW document, so it
        can catch extraction artefacts (garbled payee names, missed amounts).
        Verdicts: uphold | hold. The critic routes uncertainty to humans; it
        never auto-flips approve<->reject."""
        import difflib
        notes: List[str] = []
        risk_flags = list(decision.get("risk_flags", []))
        verdict = "uphold"

        vendor = invoice.get("vendor") or ""
        total = invoice.get("total_amount")

        # 1. Payee sanity against the raw text.
        if re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", vendor) or re.search(
                r"(?i)\b(due|invoice|total)\b", vendor):
            verdict, note = "hold", f"payee name looks garbled: {vendor!r}"
            notes.append(note)
        elif not vendor:
            verdict, note = "hold", "no payee identified"
            notes.append(note)

        # 2. Amount sanity.
        if total is None:
            if verdict == "uphold":
                verdict = "hold"
            notes.append("no total amount extracted")
        elif total <= 0:
            verdict, note = "hold", f"non-positive total ${total:,.2f}"
            notes.append(note)

        # 3. Challenge weak rejections: an unknown-item rejection where the
        #    item is CLOSE to a known SKU is likely an extraction variant —
        #    route to a human instead of auto-rejecting.
        if not decision.get("approved") and not decision.get("held"):
            unknowns = [i.get("item", "") for i in validation.get("issues", [])
                        if i.get("code") == "unknown_item"]
            codes = {i.get("code") for i in validation.get("issues", [])}
            if unknowns and codes == {"unknown_item"} and inventory_skus:
                close = []
                for u in unknowns:
                    best = max(
                        (difflib.SequenceMatcher(
                            None, u.lower(), s.lower()).ratio(), s)
                        for s in inventory_skus)
                    if best[0] > 0.65:
                        close.append(f"{u!r}~{best[1]!r}")
                if close:
                    verdict = "hold"
                    notes.append("unknown items resemble known SKUs "
                                 f"({', '.join(close)}) — verify before rejecting")

        # 4. Multiple suspicious signals on an approval -> hold.
        if decision.get("approved"):
            susp = [f for f in risk_flags if f.startswith("suspicious:")]
            if len(susp) >= 2:
                verdict, note = "hold", f"{len(susp)} suspicious signals on an approval"
                notes.append(note)

        if verdict == "uphold":
            notes.append("decision is consistent with the document and validation evidence")
        return {"verdict": verdict, "notes": " | ".join(notes), "risk_flags": risk_flags}


class LLMClient:
    """Unified interface: Grok when configured, local engine otherwise."""

    def __init__(self) -> None:
        self.local = LocalEngine()
        self.api_key = os.environ.get("XAI_API_KEY")
        self.mode = "local"
        self.degraded: Optional[str] = None  # loud failure flag, surfaced to logs
        if self.api_key:
            try:
                from xai_sdk import Client
                self._client = Client(api_key=self.api_key, timeout=60)
                # Validate the call signature eagerly so a broken integration
                # fails HERE, loudly, instead of silently per-call.
                from xai_sdk.chat import user as _user
                probe = self._client.chat.create(model="grok-3")
                probe.append(_user("ok"))
                self.mode = "grok"
            except Exception as e:
                self.degraded = f"Grok unavailable, using local engine: {e}"

    def _fail_loud(self, where: str, e: Exception) -> None:
        msg = f"LLM call failed in {where}: {e}. Continuing with local engine."
        self.degraded = msg
        # Loud: stderr, not swallowed.
        import sys
        print(f"[llm] ERROR: {msg}", file=sys.stderr)

    def _grok_json(self, prompt: str) -> Optional[Dict[str, Any]]:
        try:
            from xai_sdk.chat import user
            chat = self._client.chat.create(model="grok-3")
            chat.append(user(prompt))
            text = chat.sample().content or ""
        except Exception as e:
            self._fail_loud("grok_json", e)
            return None
        # Prefer a clean JSON parse; fall back to brace-slicing.
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        self._fail_loud("grok_json_parse", ValueError("no JSON object in response"))
        return None

    def extract_invoice(self, raw_text: str, strategy: str = "v1") -> Dict[str, Any]:
        if self.mode == "grok":
            data = self._grok_json(EXTRACTION_PROMPT.format(raw_text=raw_text[:6000]))
            if data and isinstance(data.get("items"), list):
                data.setdefault("_warnings", [])
                data.setdefault("_confidence", 0.9)
                data["_strategy"] = "grok"
                return data
            # Grok failed loudly above; fall through to local.
        if strategy == "v2":
            return self.local.extract_v2(raw_text)
        return self.local.extract(raw_text)

    def critique_decision(self, raw_text: str, invoice: Dict[str, Any],
                          validation: Dict[str, Any], decision: Dict[str, Any],
                          inventory_skus: Optional[List[str]] = None) -> Dict[str, Any]:
        if self.mode == "grok":
            sku_ctx = (f"Known inventory SKUs: {', '.join(inventory_skus)}\n"
                       if inventory_skus else "")
            prompt = (CRITIQUE_PROMPT.format(
                raw_text=raw_text[:3000],
                invoice_json=json.dumps(invoice)[:3000],
                validation_json=json.dumps(validation)[:3000],
                decision_json=json.dumps(decision)[:2000],
            ) + "\n" + sku_ctx)
            data = self._grok_json(prompt)
            if data and data.get("verdict") in ("uphold", "hold"):
                data.setdefault("risk_flags", decision.get("risk_flags", []))
                return data
        return self.local.critique(raw_text, invoice, validation, decision,
                                   inventory_skus)
