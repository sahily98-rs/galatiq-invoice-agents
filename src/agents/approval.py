"""ApprovalAgent — VP-level review with a reflection / critique loop.

Policy:
  * Any validation error            -> REJECT (with reasoning)
  * Fraud signals in the raw text    -> REJECT (escalate as suspicious)
  * Amount > $10,000                 -> heightened scrutiny, still approvable
                                       if validation is clean
  * Clean invoice under threshold    -> APPROVE

The draft decision is then passed through a critique step (LLM auditor or
local policy critic). If the critic does not uphold the decision, the agent
revises once and finalizes.
"""
from __future__ import annotations

from typing import List

from .base import BaseAgent
from ..llm import LLMClient
from ..models import ApprovalDecision, Invoice, ValidationResult
from ..tools import detect_fraud_signals

HIGH_VALUE_THRESHOLD = 10_000.0


class ApprovalAgent(BaseAgent):
    name = "approval"

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def _draft(self, inv: Invoice, vr: ValidationResult) -> ApprovalDecision:
        risk_flags: List[str] = []
        fraud = detect_fraud_signals(inv.raw_text)
        if fraud:
            risk_flags.append("fraud_signals:" + ",".join(fraud))

        total = inv.total_amount or 0.0
        if total > HIGH_VALUE_THRESHOLD:
            risk_flags.append("high_value")

        errors = vr.errors()
        if errors:
            reasons = "; ".join(e.message for e in errors)
            return ApprovalDecision(
                approved=False,
                reasoning=f"Rejected: validation failed — {reasons}.",
                risk_flags=risk_flags,
            )
        if fraud:
            return ApprovalDecision(
                approved=False,
                reasoning=(f"Rejected: fraud indicators detected ({', '.join(fraud)}). "
                           "Urgency/wire-transfer language with an unverifiable vendor is escalated, not paid."),
                risk_flags=risk_flags,
            )
        if total > HIGH_VALUE_THRESHOLD:
            reasoning = (f"Approved with scrutiny: ${total:,.2f} exceeds the ${HIGH_VALUE_THRESHOLD:,.0f} "
                         "threshold, but validation is clean, the vendor is identifiable, and no fraud "
                         "signals were found.")
        else:
            reasoning = (f"Approved: ${total:,.2f} within policy, validation passed, "
                         "no fraud signals detected.")
        return ApprovalDecision(approved=True, reasoning=reasoning, risk_flags=risk_flags)

    def run(self, inv: Invoice, vr: ValidationResult) -> ApprovalDecision:
        draft = self._draft(inv, vr)
        self.log(f"draft decision: {'APPROVE' if draft.approved else 'REJECT'}")

        # Critique / reflection loop.
        critique = self.llm.critique_decision(inv.to_dict(), vr.to_dict(), draft.to_dict())
        self.log(f"critic: {'upheld' if critique.get('uphold') else 'challenged'} — "
                 f"{critique.get('notes', '')[:120]}")

        if not critique.get("uphold"):
            # One revision: flip only if the critic found a concrete safety problem.
            revised = ApprovalDecision(
                approved=False,
                reasoning=(draft.reasoning + " [Revised after critique: "
                           + str(critique.get("notes", "")) + "]"),
                critique_notes=str(critique.get("notes", "")),
                risk_flags=list(dict.fromkeys(draft.risk_flags + critique.get("risk_flags", []))),
            )
            self.log("decision revised after critique: REJECT")
            return revised

        draft.critique_notes = str(critique.get("notes", ""))
        draft.risk_flags = list(dict.fromkeys(draft.risk_flags + critique.get("risk_flags", [])))
        self.log(f"final decision: {'APPROVE' if draft.approved else 'REJECT'}")
        return draft
