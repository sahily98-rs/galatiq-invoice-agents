"""PaymentAgent — executes the mock payment or records why money didn't move.

Crash-proof by design: amounts are validated before formatting, and every
path — paid, rejected, held — writes a ledger entry with the invoice number
and run ID, so no invoice ever disappears from the audit trail.
"""
from __future__ import annotations

import datetime
import json
import os

from .base import BaseAgent
from ..models import ApprovalDecision, Invoice, PaymentResult
from ..tools import mock_payment


class PaymentAgent(BaseAgent):
    name = "payment"

    def __init__(self, ledger_path: str = "ledger.jsonl", tracer=None):
        super().__init__(tracer)
        self.ledger_path = ledger_path

    def _record(self, entry: dict) -> None:
        entry = {"timestamp": datetime.datetime.now().isoformat(timespec="seconds"), **entry}
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def run(self, run_id: str, inv: Invoice, decision: ApprovalDecision,
            outcome: str) -> PaymentResult:
        if decision.approved and not decision.held:
            if inv.total_amount is None or inv.total_amount <= 0:
                # Defensive: approval should never let this through, but a
                # crash here would lose the audit trail — so hold instead.
                self.log("approved with invalid amount — holding instead of crashing")
                result = PaymentResult(
                    executed=False, vendor=inv.vendor, amount=inv.total_amount,
                    currency=inv.currency, status="held",
                    detail="Approved amount was missing or non-positive; routed to review.")
            else:
                self.log(f"executing payment of ${inv.total_amount:,.2f} {inv.currency} "
                         f"to {inv.vendor}")
                resp = self.use_tool("mock_payment",
                                     {"vendor": inv.vendor, "amount": inv.total_amount,
                                      "currency": inv.currency},
                                     lambda: mock_payment(inv.vendor, inv.total_amount,
                                                          inv.currency))
                result = PaymentResult(
                    executed=True, vendor=inv.vendor, amount=inv.total_amount,
                    currency=inv.currency, status=resp.get("status", "unknown"),
                    detail="Mock payment API called successfully.")
        else:
            kind = "held for human review" if decision.held else "rejected"
            self.log(f"payment withheld ({kind}) — logging with reasoning")
            result = PaymentResult(
                executed=False, vendor=inv.vendor, amount=inv.total_amount,
                currency=inv.currency,
                status="held" if decision.held else "rejected",
                detail=decision.reasoning)
        self._record({
            "run_id": run_id,
            "invoice_number": inv.invoice_number,
            "revision": inv.revision,
            "source_file": os.path.basename(inv.source_file),
            "vendor": inv.vendor,
            "amount": inv.total_amount,
            "currency": inv.currency,
            "approved": decision.approved,
            "held": decision.held,
            "outcome": outcome,
            "payment_status": result.status,
            "reasoning": decision.reasoning,
            "risk_flags": decision.risk_flags,
        })
        return result
