"""ValidationAgent — verifies extracted data against the inventory DB.

Checks, in order:
  * structural: missing vendor / items / total (missing data -> human review)
  * per-SKU: quantities aggregated across lines, then checked against stock;
    unknown items (exact match only), negative/zero quantities, zero stock
  * financial: expected_total = subtotal + tax + shipping (each parsed or
    computed from lines); unexplained mismatch beyond $1.00 is an error,
    because overbilling is the most common invoice fraud.

Self-correction is real here: item names get variant-aware lookup
("WidgetA (rush order)" -> "WidgetA"), with a logged warning when a
fallback variant is what matched — not a blind rerun of extraction.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from .base import BaseAgent
from ..models import Invoice, ValidationIssue, ValidationResult
from ..tools import InventoryDB
from .ingestion import IngestionAgent

MISMATCH_TOLERANCE = 1.00


class ValidationAgent(BaseAgent):
    name = "validation"

    def __init__(self, db: InventoryDB, tracer=None):
        super().__init__(tracer)
        self.db = db

    def _lookup_with_variants(self, name: str):
        """Exact inventory match, trying name variants. Returns (row, variant_used)."""
        row = self.use_tool("inventory.lookup", {"item": name},
                            lambda: self.db.lookup(name))
        if row is not None:
            return row, None
        for variant in IngestionAgent._name_variants(name)[1:]:
            row = self.use_tool("inventory.lookup", {"item": variant},
                                lambda v=variant: self.db.lookup(v))
            if row is not None:
                return row, variant
        return None, None

    def run(self, inv: Invoice) -> Tuple[ValidationResult, Dict[str, float]]:
        issues: List[ValidationIssue] = []

        # -- structural ---------------------------------------------------
        if not inv.vendor:
            issues.append(ValidationIssue("missing_vendor", "error",
                                          "Vendor/payee could not be determined"))
        if not inv.items:
            issues.append(ValidationIssue("missing_items", "error",
                                          "No line items extracted"))
        if inv.total_amount is None:
            issues.append(ValidationIssue("missing_total", "error",
                                          "No total amount — cannot verify what would be paid"))

        # -- per-SKU aggregation -------------------------------------------
        sku_qty: Dict[str, float] = defaultdict(float)
        sku_rep: Dict[str, str] = {}
        for li in inv.items:
            key = InventoryDB.normalize(li.item)
            sku_qty[key] += li.quantity
            sku_rep.setdefault(key, li.item)

        for key, qty in sku_qty.items():
            rep = sku_rep[key]
            if qty < 0:
                issues.append(ValidationIssue("negative_quantity", "error",
                                              f"Negative net quantity {qty:g} is a data-integrity issue",
                                              item=rep))
                continue
            if qty == 0:
                issues.append(ValidationIssue("zero_quantity", "error",
                                              "Zero quantity — nothing to pay for",
                                              item=rep))
                continue
            row, variant = self._lookup_with_variants(rep)
            if row is None:
                issues.append(ValidationIssue("unknown_item", "error",
                                              f"Item {rep!r} not found in inventory database",
                                              item=rep))
            elif row["stock"] <= 0:
                issues.append(ValidationIssue("zero_stock", "error",
                                              f"Item {rep!r} has zero stock — possible fraudulent entry",
                                              item=rep))
            elif qty > row["stock"]:
                issues.append(ValidationIssue(
                    "stock_mismatch", "error",
                    f"Requested {qty:g}x {rep!r} but only {row['stock']} in stock "
                    f"(aggregated across lines)", item=rep))
            elif variant is not None:
                issues.append(ValidationIssue(
                    "fuzzy_sku_match", "warning",
                    f"Item {rep!r} matched inventory as {row['item']!r} after "
                    f"normalization — verify the SKU", item=rep))

        # -- financial total model ----------------------------------------
        totals = self._expected_totals(inv)
        if inv.total_amount is not None and totals["expected"] is not None:
            diff = abs(inv.total_amount - totals["expected"])
            if diff > MISMATCH_TOLERANCE:
                issues.append(ValidationIssue(
                    "total_mismatch", "error",
                    f"Stated total ${inv.total_amount:,.2f} != expected "
                    f"${totals['expected']:,.2f} ({totals['basis']}); "
                    f"unexplained difference ${diff:,.2f}"))

        passed = not any(i.severity == "error" for i in issues)
        self.log(f"{'PASS' if passed else 'FAIL'} ({len(issues)} issues)")
        return ValidationResult(passed=passed, issues=issues), totals

    @staticmethod
    def _expected_totals(inv: Invoice) -> Dict[str, Optional[float]]:
        """expected = subtotal + tax + shipping, each parsed or computed."""
        lines = [(li.quantity or 0) * (li.unit_price or 0) for li in inv.items]
        line_sum = sum(lines) if all(li.unit_price for li in inv.items) and inv.items else None
        subtotal = inv.subtotal if inv.subtotal is not None else line_sum
        if subtotal is None:
            return {"expected": None, "basis": "no basis"}
        parts = [f"subtotal ${subtotal:,.2f}"]
        expected = subtotal
        if inv.tax_amount:
            expected += inv.tax_amount
            parts.append(f"tax ${inv.tax_amount:,.2f}")
        if inv.shipping_amount:
            expected += inv.shipping_amount
            parts.append(f"shipping ${inv.shipping_amount:,.2f}")
        return {"expected": round(expected, 2), "basis": " + ".join(parts)}
