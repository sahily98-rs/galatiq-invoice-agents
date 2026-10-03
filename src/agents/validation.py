"""ValidationAgent — verifies extracted data against the inventory DB.

Checks, in order:
  * structural: missing vendor / items / total (missing data -> human review)
  * per-line: negative quantities are always an error, even if the net is fine
  * per-SKU: every line is resolved to its canonical inventory SKU FIRST
    (so "WidgetA (rush order)" counts toward WidgetA's stock), then
    quantities are aggregated and checked against stock; unknown items,
    zero quantities, and zero stock are errors
  * price: unit prices more than 3x (or under 1/3x) the inventory list price
    are errors — a $66,666 widget is data corruption or fraud
  * financial: line items are ALWAYS summed and checked against the stated
    subtotal (a vendor who inflates both together does not pass); then
    expected_total = subtotal + tax + shipping is checked against the total.
    Unexplained mismatch beyond $1.00 is an error.

Variant-aware SKU lookup ("WidgetA (rush order)" -> "WidgetA") logs a
warning when a fallback variant is what matched — not a blind rerun.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from .base import BaseAgent
from ..models import Invoice, ValidationIssue, ValidationResult
from ..tools import InventoryDB
from .ingestion import IngestionAgent

MISMATCH_TOLERANCE = 1.00
PRICE_ANOMALY_RATIO = 3.0


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

    def run(self, inv: Invoice) -> Tuple[ValidationResult, Dict[str, Optional[float]]]:
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

        # -- per-line: negative quantities --------------------------------
        for li in inv.items:
            if li.quantity < 0:
                issues.append(ValidationIssue(
                    "negative_line_item", "error",
                    f"Negative line quantity {li.quantity} for {li.item!r} — "
                    f"credit lines must not offset stock checks", item=li.item))

        # -- resolve each line to a canonical SKU, THEN aggregate ---------
        # key: ("sku", canonical_name) or ("raw", normalized_name)
        sku_qty: Dict[tuple, float] = defaultdict(float)
        sku_row: Dict[tuple, object] = {}
        sku_names: Dict[tuple, List[str]] = defaultdict(list)
        for li in inv.items:
            if li.quantity < 0:
                continue  # already flagged; exclude from stock math
            row, variant = self._lookup_with_variants(li.item)
            if row is not None:
                key = ("sku", row["item"])
                sku_row[key] = row
            else:
                key = ("raw", InventoryDB.normalize(li.item))
            sku_qty[key] += li.quantity
            sku_names[key].append(li.item)
            if row is not None and variant is not None:
                issues.append(ValidationIssue(
                    "fuzzy_sku_match", "warning",
                    f"Item {li.item!r} matched inventory as {row['item']!r} after "
                    f"normalization — verify the SKU", item=li.item))

        for key, qty in sku_qty.items():
            kind, name = key
            if kind == "raw":
                issues.append(ValidationIssue("unknown_item", "error",
                                              f"Item {sku_names[key][0]!r} not found in "
                                              f"inventory database",
                                              item=sku_names[key][0]))
                continue
            row = sku_row[key]
            if qty == 0:
                issues.append(ValidationIssue("zero_quantity", "error",
                                              f"Zero net quantity for {name!r} — "
                                              f"nothing to pay for", item=name))
            elif row["stock"] <= 0:
                issues.append(ValidationIssue("zero_stock", "error",
                                              f"Item {name!r} has zero stock — possible "
                                              f"fraudulent entry", item=name))
            elif qty > row["stock"]:
                issues.append(ValidationIssue(
                    "stock_mismatch", "error",
                    f"Requested {qty:g}x {name!r} but only {row['stock']} in stock "
                    f"(aggregated across lines)", item=name))

        # -- price sanity vs list -----------------------------------------
        for li in inv.items:
            if li.unit_price is None or li.quantity < 0:
                continue
            row, _ = self._lookup_with_variants(li.item)
            if row is None:
                continue
            list_p = row["list_price"]
            if not list_p:
                continue
            ratio = li.unit_price / list_p
            if ratio > PRICE_ANOMALY_RATIO or ratio < 1 / PRICE_ANOMALY_RATIO:
                issues.append(ValidationIssue(
                    "price_anomaly", "error",
                    f"{li.item!r} at ${li.unit_price:,.2f}/unit vs list "
                    f"${list_p:,.2f} ({ratio:.1f}x) — data error or fraud",
                    item=li.item))

        # -- financial total model ----------------------------------------
        totals = self._expected_totals(inv)
        line_sum = totals["line_sum"]
        if (line_sum is not None and inv.subtotal is not None
                and abs(line_sum - inv.subtotal) > MISMATCH_TOLERANCE):
            issues.append(ValidationIssue(
                "subtotal_mismatch", "error",
                f"Stated subtotal ${inv.subtotal:,.2f} != sum of line items "
                f"${line_sum:,.2f} — inflated subtotal"))
        if inv.total_amount is not None and totals["expected"] is not None:
            diff = abs(inv.total_amount - totals["expected"])
            if diff > MISMATCH_TOLERANCE:
                issues.append(ValidationIssue(
                    "total_mismatch", "error",
                    f"Stated total ${inv.total_amount:,.2f} != expected "
                    f"${totals['expected']:,.2f} ({totals['basis']}); "
                    f"unexplained difference ${diff:,.2f}"))
        elif inv.total_amount is not None and totals["expected"] is None:
            issues.append(ValidationIssue(
                "total_unverifiable", "warning",
                "Total cannot be verified against line items (no prices)"))

        passed = not any(i.severity == "error" for i in issues)
        self.log(f"{'PASS' if passed else 'FAIL'} ({len(issues)} issues)")
        return ValidationResult(passed=passed, issues=issues), totals

    @staticmethod
    def _expected_totals(inv: Invoice) -> Dict[str, Optional[float]]:
        """expected = subtotal + tax + shipping, each parsed or computed.
        line_sum is ALWAYS computed when prices exist, so a stated subtotal
        can be checked against it (overbilling via inflated subtotal)."""
        good_lines = [li for li in inv.items if li.quantity >= 0]
        line_sum = (round(sum(li.quantity * li.unit_price for li in good_lines), 2)
                    if good_lines and all(li.unit_price is not None
                                          for li in good_lines)
                    else None)
        subtotal = inv.subtotal if inv.subtotal is not None else line_sum
        if subtotal is None:
            return {"expected": None, "line_sum": line_sum, "basis": "no basis"}
        parts = [f"subtotal ${subtotal:,.2f}"]
        expected = subtotal
        if inv.tax_amount:
            expected += inv.tax_amount
            parts.append(f"tax ${inv.tax_amount:,.2f}")
        if inv.shipping_amount:
            expected += inv.shipping_amount
            parts.append(f"shipping ${inv.shipping_amount:,.2f}")
        return {"expected": round(expected, 2), "line_sum": line_sum,
                "basis": " + ".join(parts)}
