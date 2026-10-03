"""ApprovalAgent — VP-level review with a real reflection loop.

Policy:
  * validation errors (stock, unknown SKU, bad data, total mismatch) -> REJECT
  * missing payee / items / total, low extraction confidence           -> HOLD
  * critical fraud signals (wire pressure, backdated due date)          -> REJECT (fraud)
  * vendor identity questions, zero amounts, foreign currency           -> HOLD
  * clean invoice                                                      -> APPROVE
    (>$10K gets an explicit scrutiny note but is still payable)

The draft then goes through a critic that sees the RAW document text —
not just the extraction — so it can catch artefacts like garbled payee
names. Verdicts: uphold | hold. The critic routes uncertainty to humans;
it never flips approve<->reject on its own, in either direction.
"""
from __future__ import annotations

from typing import List

from .base import BaseAgent
from ..llm import LLMClient
from ..models import ApprovalDecision, Invoice, RiskSignal, ValidationResult
from ..tools import analyze_risk

HIGH_VALUE_THRESHOLD = 10_000.0
LOW_CONFIDENCE_THRESHOLD = 0.5

HOLD_CODES = {"missing_vendor", "missing_items", "missing_total"}


class ApprovalAgent(BaseAgent):
    name = "approval"

    def __init__(self, llm: LLMClient, tracer=None, skus=None):
        super().__init__(tracer)
        self.llm = llm
        self.skus = skus or []

    def run(self, inv: Invoice, vr: ValidationResult) -> ApprovalDecision:
        signals = self.use_tool(
            "risk.analyze",
            {"source": inv.source_file},
            lambda: analyze_risk(inv.raw_text, inv.invoice_date, inv.due_date),
        )
        for s in signals:
            self.log(f"risk signal [{s.severity}]: {s.signal} — {s.evidence}")

        draft = self._draft(inv, vr, signals)
        self.log(f"draft: {'APPROVE' if draft.approved and not draft.held else 'HOLD' if draft.held else 'REJECT'}")

        critique = self.use_tool(
            "llm.critique",
            {"mode": self.llm.mode},
            lambda: self.llm.critique_decision(inv.raw_text, inv.to_dict(),
                                              vr.to_dict(), draft.to_dict(),
                                              inventory_skus=self.skus),
        )
        self.log(f"critic ({self.llm.mode}): {critique.get('verdict')} — "
                 f"{str(critique.get('notes', ''))[:140]}")

        if critique.get("verdict") == "hold" and not draft.held:
            draft.held = True
            draft.approved = False
            draft.reasoning += f" [Held after critique: {critique.get('notes', '')}]"
            self.log("critic diverted decision to human review")
        draft.critique_notes = str(critique.get("notes", ""))
        extra = [f for f in critique.get("risk_flags", []) if f not in draft.risk_flags]
        draft.risk_flags.extend(extra)
        self.log(f"final: {'APPROVE' if draft.approved and not draft.held else 'HOLD' if draft.held else 'REJECT'}")
        return draft

    def _draft(self, inv: Invoice, vr: ValidationResult,
               signals: List[RiskSignal]) -> ApprovalDecision:
        risk_flags = [f"{s.severity}:{s.signal}" for s in signals]
        total = inv.total_amount or 0.0

        # Missing fundamentals -> human, not auto-reject.
        missing = [i for i in vr.errors() if i.code in HOLD_CODES]
        if missing:
            return ApprovalDecision(
                approved=False, held=True,
                reasoning="Held for review: " + "; ".join(i.message for i in missing),
                risk_flags=risk_flags)

        errors = vr.errors()
        if errors:
            return ApprovalDecision(
                approved=False,
                reasoning="Rejected: " + "; ".join(i.message for i in errors),
                risk_flags=risk_flags)

        critical = [s for s in signals if s.severity == "critical"]
        if critical:
            return ApprovalDecision(
                approved=False,
                reasoning="Rejected as suspected fraud: "
                          + "; ".join(s.evidence for s in critical),
                risk_flags=risk_flags)

        holds = []
        if any(s.signal == "vendor_name_change" for s in signals):
            holds.append("vendor identity changed — verify before paying (possible BEC)")
        if total <= 0:
            holds.append(f"non-positive total ${total:,.2f}")
        if inv.currency != "USD":
            holds.append(f"foreign currency {inv.currency} — no FX handling")
        if inv.extraction_confidence < LOW_CONFIDENCE_THRESHOLD:
            holds.append(f"low extraction confidence {inv.extraction_confidence:.2f}")
        if holds:
            return ApprovalDecision(
                approved=False, held=True,
                reasoning="Held for review: " + "; ".join(holds),
                risk_flags=risk_flags)

        if total > HIGH_VALUE_THRESHOLD:
            risk_flags.append("info:high_value_scrutiny")
            reasoning = (f"Approved with scrutiny: ${total:,.2f} exceeds the "
                         f"${HIGH_VALUE_THRESHOLD:,.0f} threshold, but validation is clean "
                         f"and no fraud signals were found.")
        else:
            reasoning = (f"Approved: ${total:,.2f} {inv.currency} within policy, "
                         f"validation passed, no fraud signals.")
        return ApprovalDecision(approved=True, reasoning=reasoning, risk_flags=risk_flags)
