"""Pipeline orchestrator: ingestion -> validation -> approval -> payment.

Crash safety: any exception becomes a HOLD_REVIEW outcome with a ledger
entry — an invoice never disappears from the audit trail. Duplicate invoice
numbers (already paid per the ledger) are rejected before money moves.
"""
from __future__ import annotations

import json
import os
import traceback
import uuid

from .agents.approval import ApprovalAgent
from .agents.ingestion import IngestionAgent
from .agents.payment import PaymentAgent
from .agents.validation import ValidationAgent
from .llm import LLMClient
from .logging import RunLogger
from .models import ApprovalDecision, Invoice, PipelineResult
from .tools import InventoryDB


class InvoicePipeline:
    def __init__(self, db_path: str = "inventory.db", ledger_path: str = "ledger.jsonl",
                 log_path: str = "runs.jsonl", quiet: bool = False):
        self.db_path = db_path
        self.ledger_path = ledger_path
        self.log_path = log_path
        self.quiet = quiet
        self.db = InventoryDB(db_path)
        self.llm = LLMClient()

    def _tracer(self, logger: RunLogger):
        return lambda agent, tool, args: logger.tool_call(agent, tool, args)

    def _paid_invoices(self) -> Dict[tuple, Dict]:
        """(normalized vendor, normalized invoice number) -> ledger entry,
        for everything ever paid. Keying on vendor+number avoids flagging
        two vendors' '0001' as duplicates."""
        seen: Dict[tuple, Dict] = {}
        if not os.path.exists(self.ledger_path):
            return seen
        with open(self.ledger_path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("outcome") == "PAID" and e.get("invoice_number"):
                    key = (InventoryDB.normalize(e.get("vendor", "")),
                           InventoryDB.normalize(e["invoice_number"]))
                    seen[key] = e
        return seen

    def _crash_ledger(self, run_id: str, source: str, invoice_number: str,
                      reason: str) -> None:
        import datetime
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                "run_id": run_id,
                "invoice_number": invoice_number,
                "source_file": os.path.basename(source),
                "outcome": "HOLD_REVIEW",
                "payment_status": "held",
                "reasoning": f"Pipeline crashed; routed to human review. {reason}",
            }) + "\n")

    def run(self, invoice_path: str) -> PipelineResult:
        run_id = uuid.uuid4().hex[:8]
        logger = RunLogger(self.log_path, run_id=run_id, quiet=self.quiet)
        tracer = self._tracer(logger)
        ingestion = IngestionAgent(self.llm, tracer)
        validation = ValidationAgent(self.db, tracer)
        approval = ApprovalAgent(self.llm, tracer, skus=self.db.skus(),
                                 db=self.db, ledger_path=self.ledger_path)
        payment = PaymentAgent(self.ledger_path, tracer)

        logger.say(f"\n[pipeline] === {os.path.basename(invoice_path)} "
                   f"(run {run_id}, llm={self.llm.mode}) ===")
        if self.llm.degraded:
            logger.say(f"[pipeline] WARNING: {self.llm.degraded}")
        logger.pipeline_start(invoice_path)

        invoice_number = ""
        try:
            logger.stage_start("ingestion")
            invoice = ingestion.run(invoice_path)
            invoice_number = invoice.invoice_number
            logger.stage_end("ingestion", strategy=invoice.extraction_strategy,
                             confidence=invoice.extraction_confidence)

            logger.stage_start("validation")
            vr, totals = validation.run(invoice)
            logger.stage_end("validation", passed=vr.passed,
                             issues=len(vr.issues))

            # Duplicate-invoice guard: never pay the same (vendor, number)
            # twice. A *revised* invoice (different revision marker) is held
            # with the difference calculated — the vendor may be legitimately
            # owed more, but a human must confirm.
            dup_key = (InventoryDB.normalize(invoice.vendor),
                       InventoryDB.normalize(invoice.invoice_number))
            paid = self._paid_invoices().get(dup_key) if invoice.invoice_number else None
            if paid:
                prev_rev = (paid.get("revision") or "").strip().lower()
                cur_rev = (invoice.revision or "").strip().lower()
                if cur_rev and cur_rev != prev_rev:
                    prev_amt = paid.get("amount") or 0.0
                    cur_amt = invoice.total_amount or 0.0
                    reason = (f"Revision {invoice.revision} of invoice "
                              f"{invoice.invoice_number}: previously paid "
                              f"${prev_amt:,.2f}, revised total ${cur_amt:,.2f} "
                              f"(difference ${cur_amt - prev_amt:,.2f}) — "
                              f"verify the revision before paying the difference.")
                    decision = ApprovalDecision(approved=False, held=True,
                                                decision_type="hold",
                                                reasoning=reason,
                                                risk_flags=["info:invoice_revision"])
                    outcome = "HOLD_REVIEW"
                else:
                    reason = (f"Duplicate invoice {invoice.invoice_number!r} from "
                              f"{invoice.vendor} — already paid per ledger; "
                              f"refusing second payment.")
                    decision = ApprovalDecision(approved=False,
                                                decision_type="reject",
                                                reasoning=reason,
                                                risk_flags=["info:duplicate_invoice"])
                    outcome = "REJECTED_DUPLICATE"
                logger.say(f"[pipeline] {reason}")
                payment_result = payment.run(run_id, invoice, decision, outcome)
                result = PipelineResult(run_id, invoice, vr, decision,
                                        payment_result, outcome, reason)
                logger.outcome(outcome, reason)
                logger.close()
                return result

            logger.stage_start("approval")
            decision = approval.run(invoice, vr)
            logger.stage_end("approval", approved=decision.approved, held=decision.held)

            # Structured outcome — set by the approval agent, never inferred
            # from reason text.
            outcome = {"approve": "PAID",
                       "reject": "REJECTED_VALIDATION",
                       "reject_fraud": "REJECTED_FRAUD",
                       "hold": "HOLD_REVIEW"}[decision.decision_type]
            reason = decision.reasoning

            logger.stage_start("payment")
            payment_result = payment.run(run_id, invoice, decision, outcome)
            logger.stage_end("payment", status=payment_result.status)

            result = PipelineResult(run_id, invoice, vr, decision,
                                    payment_result, outcome, reason)
            logger.outcome(outcome, reason)
            logger.say(f"[pipeline] === outcome: {outcome} ===")
            return result
        except Exception as e:  # noqa: BLE001 — crash -> HOLD, never a bare traceback
            reason = f"{type(e).__name__}: {e}"
            logger.say(f"[pipeline] CRASH -> HOLD_REVIEW ({reason})")
            logger.outcome("HOLD_REVIEW", reason)
            self._crash_ledger(run_id, invoice_path, invoice_number, reason)
            # Best-effort result so the invoice stays visible in the audit trail.
            invoice = Invoice(vendor="", invoice_number="", invoice_date=None,
                              due_date=None, source_file=invoice_path,
                              extraction_warnings=[f"pipeline crash: {reason}"])
            from .models import ValidationResult, PaymentResult
            vr = ValidationResult(passed=False, issues=[])
            decision = ApprovalDecision(approved=False, held=True, reasoning=reason)
            pres = PaymentResult(executed=False, vendor="", amount=None,
                                 currency="USD", status="held", detail=reason)
            return PipelineResult(run_id, invoice, vr, decision, pres,
                                  "HOLD_REVIEW", reason)
        finally:
            logger.close()

    def close(self) -> None:
        self.db.close()
