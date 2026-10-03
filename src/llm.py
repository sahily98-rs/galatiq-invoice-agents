"""LLM client abstraction.

Uses xAI's Grok as the reasoning engine when an XAI_API_KEY is available.
Otherwise falls back to a fully local deterministic engine so the system
runs offline, per the challenge's "simulate everything locally" requirement.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional


EXTRACTION_PROMPT = """You extract structured invoice data. Reply with ONLY a JSON object.

Invoice text:
---
{raw_text}
---

Return JSON with keys: vendor (string), invoice_number (string),
due_date (string or null), total_amount (number or null),
items (list of objects with item, quantity, unit_price).
Tolerate typos (e.g. "Vndr", "INVOCE"), inconsistent labels, and odd
formatting. If a field is genuinely absent, use null / empty values.
JSON only, no commentary."""

CRITIQUE_PROMPT = """You are a meticulous finance auditor reviewing an invoice approval decision.

Invoice: {invoice_json}
Validation result: {validation_json}
Proposed decision: {decision_json}

Critique the proposed decision. Consider: fraud signals (urgency language,
wire-transfer requests, unknown vendors), amount thresholds, validation
errors, and missing data. Reply with ONLY a JSON object:
{{"uphold": true/false, "notes": "your critique", "risk_flags": ["..."]}}"""


class LocalEngine:
    """Deterministic local extraction + critique. No network needed."""

    VENDOR_RES = [
        re.compile(r"(?im)^\s*(?:vendor|vndr)\s*:\s*(.+?)\s*$"),
        re.compile(r"(?im)^\s*from\s*:\s*(.+?)\s*$"),  # fallback, e.g. "FROM: QuickShip Distributers"
    ]
    TOTAL_RE = re.compile(r"(?im)^\s*(?:total amount|grand total|amount due|amt|total)\s*:?\s*\$?\s*([\d,]+\.\d{2})")
    DUE_RE = re.compile(r"(?im)^\s*(?:due date|due dt|due)\s*:\s*(.+?)\s*$")
    INVNO_RES = [
        re.compile(r"(?im)(?:invoice\s*number|inv\s*no)\s*:?\s*([A-Za-z0-9][A-Za-z0-9\- ]*)"),
        re.compile(r"(?im)(?:invoice|inv\.?\s*#?)\s*:?\s*([A-Za-z0-9][A-Za-z0-9\-]*)"),
    ]
    ITEM_RES = [
        # "WidgetA    qty: 10    unit price: $250.00" / "GadgetX  qty 20   @ $750 ea"
        re.compile(
            r"(?im)^\s*[-*\u2022]?\s*(?P<item>[A-Za-z][A-Za-z0-9_]*)\s+qty\s*:?\s*(?P<qty>-?\d+)"
            r"(?:\s*(?:@|unit price:?|each|ea))?\s*\$?\s*(?P<price>[\d,]+\.\d{2})?"
        ),
        # "- SuperGizmo       x12     $400.00 each" (also catches "Gadget X  4")
        re.compile(
            r"(?im)^\s*[-*\u2022]?\s*(?P<item>[A-Za-z][A-Za-z0-9_]*(?: [A-Za-z][A-Za-z0-9_]*)?)"
            r"\s*[xX]\s*(?P<qty>-?\d+)\s+\$?\s*(?P<price>[\d,]+(?:\.\d{2})?)?"
        ),
        # Table rows: "WidgetA 6 $250.00 $1,500.00" (item, qty, unit price, line total).
        # Item may be two tokens ("Widget A") — normalized to the SKU form later.
        # Prices may lack cents ("$250") and amounts may carry OCR noise ("$3,500.O0").
        re.compile(
            r"(?im)^\s*(?P<item>[A-Za-z][A-Za-z0-9_]*(?: [A-Za-z][A-Za-z0-9_]*)?)"
            r"\s+(?P<qty>-?\d+)\s+\$?(?P<price>[\d,]+(?:\.\d{2})?)\s+\$?[\d,O][\d,O.,]*"
        ),
    ]

    @staticmethod
    def _money(s: Optional[str]) -> Optional[float]:
        if not s:
            return None
        try:
            return float(s.replace(",", "").replace("$", "").strip())
        except ValueError:
            return None

    @staticmethod
    def _dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Drop partial-name duplicates: when two items share a quantity and one
        name is strictly contained in the other ("Gadget" vs "GadgetX"), the
        shorter is an artefact of overlapping patterns — keep the longer."""
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
        vendor_m = next((p.search(raw_text) for p in self.VENDOR_RES if p.search(raw_text)), None)
        total_m = self.TOTAL_RE.search(raw_text)
        due_m = self.DUE_RE.search(raw_text)
        invno_m = next((p.search(raw_text) for p in self.INVNO_RES if p.search(raw_text)), None)

        items: List[Dict[str, Any]] = []
        seen = set()
        for pat in self.ITEM_RES:
            for m in pat.finditer(raw_text):
                # normalize OCR spacing: "Widget A" -> "WidgetA" to match inventory SKUs
                name = "".join(m.group("item").split())
                key = (name.lower(), m.group("qty"))
                if key in seen:
                    continue
                seen.add(key)
                # skip matches that are actually the totals line etc.
                if name.lower() in {"total", "subtotal", "tax", "amount", "invoice"}:
                    continue
                items.append(
                    {
                        "item": name,
                        "quantity": int(m.group("qty")),
                        "unit_price": self._money(m.group("price")),
                    }
                )

        warnings = []
        if not vendor_m:
            warnings.append("vendor not found")
        if total_m is None:
            warnings.append("total amount not found")
        if not items:
            warnings.append("no line items found")

        items = self._dedupe(items)

        confidence = 1.0 - 0.2 * len(warnings)
        return {
            "vendor": vendor_m.group(1).strip() if vendor_m else "",
            "invoice_number": invno_m.group(1).strip() if invno_m else "",
            "due_date": due_m.group(1).strip() if due_m else None,
            "total_amount": self._money(total_m.group(1)) if total_m else None,
            "items": items,
            "_warnings": warnings,
            "_confidence": max(confidence, 0.2),
        }

    def critique(self, invoice: Dict[str, Any], validation: Dict[str, Any],
                 decision: Dict[str, Any]) -> Dict[str, Any]:
        """Rule-grounded second pass over the proposed decision."""
        notes = []
        risk_flags = list(decision.get("risk_flags", []))
        uphold = True

        total = invoice.get("total_amount") or 0
        errors = [i for i in validation.get("issues", []) if i.get("severity") == "error"]

        if decision.get("approved") and errors:
            uphold = False
            notes.append("Decision approves despite validation errors — unsafe.")
        if decision.get("approved") and total > 10000 and "high_value" not in risk_flags:
            notes.append("High-value invoice approved without explicit scrutiny flag.")
        if not decision.get("approved") and not errors and not risk_flags and total <= 10000:
            notes.append("Rejection looks overcautious: no errors, no risk flags, modest amount.")
        if not notes:
            notes.append("Decision is consistent with validation evidence and policy.")
        return {"uphold": uphold, "notes": " ".join(notes), "risk_flags": risk_flags}


class LLMClient:
    """Unified interface: Grok when configured, local engine otherwise."""

    def __init__(self) -> None:
        self.local = LocalEngine()
        self.api_key = os.environ.get("XAI_API_KEY")
        self.mode = "local"
        if self.api_key:
            try:
                from xai_sdk import Client  # type: ignore
                self._grok = Client(api_key=self.api_key)
                self.mode = "grok"
            except Exception:
                self.mode = "local"

    def _grok_json(self, prompt: str) -> Optional[Dict[str, Any]]:
        try:
            chat = self._grok.chat.create(model="grok-3")
            chat.append(role="user", content=prompt)
            text = chat.sample().content
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end == -1:
                return None
            return json.loads(text[start:end + 1])
        except Exception:
            return None

    def extract_invoice(self, raw_text: str) -> Dict[str, Any]:
        if self.mode == "grok":
            data = self._grok_json(EXTRACTION_PROMPT.format(raw_text=raw_text[:6000]))
            if data and isinstance(data.get("items"), list):
                data.setdefault("_warnings", [])
                data.setdefault("_confidence", 0.9)
                return data
        return self.local.extract(raw_text)

    def critique_decision(self, invoice: Dict[str, Any], validation: Dict[str, Any],
                          decision: Dict[str, Any]) -> Dict[str, Any]:
        if self.mode == "grok":
            data = self._grok_json(CRITIQUE_PROMPT.format(
                invoice_json=json.dumps(invoice)[:3000],
                validation_json=json.dumps(validation)[:3000],
                decision_json=json.dumps(decision)[:2000],
            ))
            if data and "uphold" in data:
                return data
        return self.local.critique(invoice, validation, decision)
