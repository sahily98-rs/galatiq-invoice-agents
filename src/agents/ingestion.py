"""IngestionAgent — extracts structured invoice data from any document.

Strategy: structured formats (JSON/CSV/XML) are parsed natively; free text
goes through the LLM (Grok when configured, deterministic engine otherwise).
If the primary extraction leaves critical fields missing, a genuinely
different algorithm (v2: line-oriented labeled scan + price-anchored item
detection) runs as the repair pass — not a rerun of the same code.

Dates are normalized to ISO; currency is detected; vendor names are
sanitized against PDF column-bleed artefacts.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import BaseAgent
from .. import dates as dateparse
from ..llm import LLMClient
from ..models import Invoice, LineItem
from ..tools import parse_currency, parse_structured, read_document, sanitize_vendor


class IngestionAgent(BaseAgent):
    name = "ingestion"

    def __init__(self, llm: LLMClient, tracer=None):
        super().__init__(tracer)
        self.llm = llm

    def _to_invoice(self, data: Dict[str, Any], raw_text: str, source: str) -> Invoice:
        items = [
            LineItem(item=str(i.get("item", "")),
                     quantity=int(i.get("quantity", 0)),
                     unit_price=i.get("unit_price"))
            for i in data.get("items", [])
        ]
        inv_date = self._norm_date(data.get("invoice_date"))
        due_raw = data.get("due_date")
        due_norm = self._norm_date(due_raw, ref=inv_date)
        currency = (data.get("currency") or parse_currency(raw_text) or "USD").upper()
        return Invoice(
            vendor=sanitize_vendor(str(data.get("vendor") or "")),
            invoice_number=str(data.get("invoice_number") or "").strip(),
            invoice_date=inv_date,
            due_date=due_norm,
            due_date_raw=str(due_raw) if due_raw else None,
            items=items,
            total_amount=data.get("total_amount"),
            subtotal=data.get("subtotal"),
            tax_amount=data.get("tax_amount"),
            shipping_amount=data.get("shipping_amount"),
            currency=currency,
            raw_text=raw_text,
            source_file=source,
            extraction_confidence=float(data.get("_confidence", 0.8)),
            extraction_warnings=list(data.get("_warnings", [])),
            extraction_strategy=str(data.get("_strategy", "v1")),
        )

    @staticmethod
    def _norm_date(v: Any, ref: Optional[str] = None) -> Optional[str]:
        if not v:
            return None
        ref_d = dateparse.parse_date(ref) if ref else None
        d = dateparse.parse_date(str(v), ref=ref_d)
        return dateparse.iso(d)

    @staticmethod
    def _needs_repair(inv: Invoice) -> bool:
        return (not inv.vendor) or (not inv.items) or (inv.total_amount is None)

    def run(self, path: str) -> Invoice:
        self.log(f"reading {path}")
        raw_text = self.use_tool("read_document", {"path": path},
                                 lambda: read_document(path))

        structured = self.use_tool("parse_structured", {"path": path},
                                   lambda: parse_structured(path))
        if structured is not None:
            self.log("structured format — native parse (no LLM needed)")
            return self._to_invoice(structured, raw_text, path)

        self.log(f"free text — extracting (engine={self.llm.mode})")
        data = self.use_tool("llm.extract", {"strategy": "v1"},
                             lambda: self.llm.extract_invoice(raw_text, strategy="v1"))
        inv = self._to_invoice(data, raw_text, path)

        if self._needs_repair(inv):
            self.log("critical fields missing — repair pass with alternate algorithm (v2)")
            data2 = self.use_tool("llm.extract", {"strategy": "v2"},
                                  lambda: self.llm.extract_invoice(raw_text, strategy="v2"))
            inv2 = self._to_invoice(data2, raw_text, path)
            if not self._needs_repair(inv2):
                self.log(f"repair recovered fields via {inv2.extraction_strategy}")
                return inv2
            # Keep whichever extraction is more complete.
            score = lambda i: (bool(i.vendor), bool(i.items), i.total_amount is not None)
            inv = inv2 if score(inv2) > score(inv) else inv
            inv.extraction_confidence = min(inv.extraction_confidence, 0.4)
            self.log("repair incomplete — flagging low confidence for human review")
        else:
            self.log(f"extracted vendor={inv.vendor!r} items={len(inv.items)} "
                     f"total={inv.total_amount} {inv.currency} "
                     f"confidence={inv.extraction_confidence:.2f}")
        return inv

    @staticmethod
    def _name_variants(name: str) -> List[str]:
        """Alternate normalizations for near-miss SKUs, most canonical first."""
        import re
        cands = [
            name,
            re.sub(r"\s*\([^)]*\)", "", name).strip(),  # "WidgetA (rush)" -> "WidgetA"
            re.sub(r"[^A-Za-z0-9]", "", name),          # punctuation-stripped
        ]
        out: List[str] = []
        for c in cands:
            if c and c not in out:
                out.append(c)
        return out

