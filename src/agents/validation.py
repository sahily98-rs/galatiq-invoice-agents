"""ValidationAgent — verifies extracted data against the inventory DB.

Checks: unknown items, quantity exceeding stock, zero-stock / fraudulent
items, negative quantities, and stated-total vs computed-total mismatches.
Includes a self-correction loop: when extraction confidence is low and the
issues look like extraction artefacts, it asks ingestion for one more pass.
"""
from __future__ import annotations

from typing import List

from .base import BaseAgent
from ..models import Invoice, ValidationIssue, ValidationResult
from ..tools import InventoryDB


class ValidationAgent(BaseAgent):
    name = "validation"

    def __init__(self, db: InventoryDB):
        self.db = db

    def _validate(self, inv: Invoice) -> ValidationResult:
        issues: List[ValidationIssue] = []

        if not inv.vendor:
            issues.append(ValidationIssue("missing_vendor", "error", "Vendor name could not be determined"))
        if not inv.items:
            issues.append(ValidationIssue("missing_items", "error", "No line items extracted"))
        if inv.total_amount is None:
            issues.append(ValidationIssue("missing_total", "warning", "Total amount missing; cannot cross-check"))

        for li in inv.items:
            if li.quantity < 0:
                issues.append(ValidationIssue("negative_quantity", "error",
                                              f"Negative quantity {li.quantity} is a data-integrity issue",
                                              item=li.item))
                continue
            row = self.db.lookup(li.item)
            if row is None:
                issues.append(ValidationIssue("unknown_item", "error",
                                              f"Item {li.item!r} not found in inventory database",
                                              item=li.item))
            elif row["stock"] <= 0:
                issues.append(ValidationIssue("zero_stock", "error",
                                              f"Item {li.item!r} has zero stock — possible fraudulent entry",
                                              item=li.item))
            elif li.quantity > row["stock"]:
                issues.append(ValidationIssue(
                    "stock_mismatch", "error",
                    f"Requested {li.quantity}x {li.item!r} but only {row['stock']} in stock",
                    item=li.item))

        # Cross-check stated total against computed line total.
        if inv.total_amount is not None and inv.items:
            priced = [i for i in inv.items if i.unit_price]
            if priced:
                computed = sum(i.quantity * (i.unit_price or 0) for i in priced)
                if abs(computed - inv.total_amount) > 1.0:
                    issues.append(ValidationIssue(
                        "total_mismatch", "warning",
                        f"Stated total ${inv.total_amount:,.2f} != computed ${computed:,.2f}"))

        passed = not any(i.severity == "error" for i in issues)
        return ValidationResult(passed=passed, issues=issues)

    def run(self, inv: Invoice, reextract=None) -> ValidationResult:
        """reextract: optional callable returning a fresh Invoice for the correction loop."""
        result = self._validate(inv)
        self.log(f"initial check: {'PASS' if result.passed else 'FAIL'} "
                 f"({len(result.issues)} issues)")

        # Self-correction: low-confidence extraction + suspicious issues -> one more pass.
        if (not result.passed and inv.extraction_confidence < 0.6 and reextract is not None
                and any(i.code in {"missing_vendor", "missing_items", "unknown_item"}
                        for i in result.issues)):
            self.log("low extraction confidence — requesting one re-extraction")
            inv2 = reextract()
            result2 = self._validate(inv2)
            self.log(f"re-check: {'PASS' if result2.passed else 'FAIL'}")
            if result2.passed or len(result2.errors()) < len(result.errors()):
                inv.items, inv.vendor = inv2.items, inv2.vendor or inv.vendor
                return result2
        return result
