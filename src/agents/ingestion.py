"""IngestionAgent — extracts structured invoice data from any document.

Strategy: structured formats (JSON/CSV/XML) are parsed natively for speed
and accuracy; free-text formats (TXT/PDF) go through the LLM extraction
tool, with a deterministic repair pass as a self-correction loop when
critical fields are missing.
"""
from __future__ import annotations

from typing import Any, Dict

from .base import BaseAgent
from ..llm import LLMClient
from ..models import Invoice, LineItem
from ..tools import parse_structured, read_document


class IngestionAgent(BaseAgent):
    name = "ingestion"

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def _to_invoice(self, data: Dict[str, Any], raw_text: str, source: str) -> Invoice:
        items = [
            LineItem(item=str(i.get("item", "")),
                     quantity=int(i.get("quantity", 0)),
                     unit_price=i.get("unit_price"))
            for i in data.get("items", [])
        ]
        warnings = list(data.get("_warnings", []))
        return Invoice(
            vendor=str(data.get("vendor") or ""),
            invoice_number=str(data.get("invoice_number") or ""),
            due_date=data.get("due_date"),
            items=items,
            total_amount=data.get("total_amount"),
            raw_text=raw_text,
            source_file=source,
            extraction_confidence=float(data.get("_confidence", 0.8)),
            extraction_warnings=warnings,
        )

    def _needs_repair(self, inv: Invoice) -> bool:
        return (not inv.vendor) or (not inv.items) or (inv.total_amount is None)

    def run(self, path: str) -> Invoice:
        self.log(f"reading {path}")
        raw_text = read_document(path)

        structured = parse_structured(path)
        if structured is not None:
            self.log("structured format detected — native parse (no LLM needed)")
            inv = self._to_invoice(structured, raw_text, path)
        else:
            self.log(f"free-text format — extracting via LLM engine (mode={self.llm.mode})")
            data = self.llm.extract_invoice(raw_text)
            inv = self._to_invoice(data, raw_text, path)

        # Self-correction loop: one repair attempt when critical fields are missing.
        if self._needs_repair(inv):
            self.log("missing critical fields — attempting repair pass")
            hint = (raw_text + "\n\nNOTE: previous extraction missed "
                    + ", ".join(inv.extraction_warnings or ["fields"])
                    + ". Look harder at headers, totals and item lines.")
            data = self.llm.extract_invoice(hint)
            repaired = self._to_invoice(data, raw_text, path)
            if not self._needs_repair(repaired):
                self.log("repair pass recovered missing fields")
                inv = repaired
            else:
                self.log("repair pass incomplete — flagging low confidence")
                inv.extraction_confidence = min(inv.extraction_confidence, 0.4)

        self.log(f"extracted vendor={inv.vendor!r} items={len(inv.items)} "
                 f"total={inv.total_amount} confidence={inv.extraction_confidence:.2f}")
        return inv
