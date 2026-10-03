"""PaymentAgent — executes the mock payment or logs the rejection."""
from __future__ import annotations

import datetime
import json
import os

from .base import BaseAgent
from ..models import ApprovalDecision, Invoice, PaymentResult
from ..tools import mock_payment


class PaymentAgent(BaseAgent):
    name = "payment"

    def __init__(self, ledger_path: str = "ledger.jsonl"):
        self.ledger_path = ledger_path

    def _record(self, entry: dict) -> None:
        entry = {"timestamp": datetime.datetime.now().isoformat(), **entry}
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def run(self, inv: Invoice, decision: ApprovalDecision) -> PaymentResult:
        if decision.approved:
            self.log(f"executing payment of ${inv.total_amount:,.2f} to {inv.vendor}")
            resp = mock_payment(inv.vendor, inv.total_amount)
            result = PaymentResult(
                executed=True, vendor=inv.vendor, amount=inv.total_amount,
                status=resp.get("status", "unknown"),
                detail="Mock payment API called successfully.",
            )
        else:
            self.log("payment withheld — logging rejection with reasoning")
            result = PaymentResult(
                executed=False, vendor=inv.vendor, amount=inv.total_amount,
                status="rejected",
                detail=decision.reasoning,
            )
        self._record({
            "source_file": os.path.basename(inv.source_file),
            "vendor": inv.vendor,
            "amount": inv.total_amount,
            "approved": decision.approved,
            "payment_status": result.status,
            "reasoning": decision.reasoning,
            "risk_flags": decision.risk_flags,
        })
        return result
