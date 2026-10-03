"""ApprovalAgent — VP-level review with a real reflection loop.

Policy:
  * validation errors (stock, unknown SKU, bad data, total mismatch) -> REJECT
  * critical fraud signals (wire pressure, bank-detail change, backdated
    due date) -> REJECT_FRAUD (explicit decision type, never text search)
  * missing payee / items / total, low extraction confidence           -> HOLD
  * vendor identity questions, zero amounts, foreign currency           -> HOLD
  * totals above the auto-pay ceiling                                  -> HOLD
  * clean invoice                                                      -> APPROVE
    (>$10K gets an explicit scrutiny note but is still payable)

The draft then goes through a critic that sees the RAW document text —
not just the extraction — so it can catch artefacts like garbled payee
names. When Grok is the engine and the draft is uncertain, the critic runs
a ReAct loop: Grok itself chooses between inventory_lookup, ledger_search
and risk_analyze, and its reasoning trace is recorded. Verdicts:
uphold | hold. The critic routes uncertainty to humans; it never flips
approve<->reject on its own, in either direction.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from .base import BaseAgent
from ..llm import LLMClient
from ..models import ApprovalDecision, Invoice, RiskSignal, ValidationResult
from ..tools import InventoryDB, analyze_risk

HIGH_VALUE_THRESHOLD = 10_000.0   # above: scrutiny note, still payable
AUTO_PAY_CEILING = 50_000.0       # above: human must review
LOW_CONFIDENCE_THRESHOLD = 0.5

HOLD_CODES = {"missing_vendor", "missing_items", "missing_total"}


class ApprovalAgent(BaseAgent):
    name = "approval"

    def __init__(self, llm: LLMClient, tracer=None, skus=None,
                 db: Optional[InventoryDB] = None,
                 ledger_path: str = "ledger.jsonl"):
        super().__init__(tracer)
        self.llm = llm
        self.skus = skus or []
        self.db = db
        self.ledger_path = ledger_path

    def _critic_tools(self, inv: Invoice) -> Dict[str, Callable]:
        """Tools the Grok critic may call itself during reflection."""
        def inventory_lookup(item: str) -> Dict:
            if self.db is None:
                return {"error": "no db"}
            row = self.db.lookup(item)
            if row is None:
                return {"found": False, "item": item}
            return {"found": True, "item": row["item"],
                    "stock": row["stock"], "list_price": row["list_price"]}

        def ledger_search(invoice_number: str = "",
                          vendor: str = "") -> Dict:
            import json, os
            hits = []
            if os.path.exists(self.ledger_path):
                with open(self.ledger_path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            e = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if invoice_number and e.get("invoice_number") != invoice_number:
                            continue
                        if vendor and InventoryDB.normalize(
                                e.get("vendor", "")) != InventoryDB.normalize(vendor):
                            continue
                        hits.append({k: e.get(k) for k in
                                     ("invoice_number", "vendor", "amount",
                                      "outcome", "payment_status", "run_id")})
            return {"hits": hits}

        def risk_analyze() -> Dict:
            return {"signals": [s.to_dict() for s in
                                analyze_risk(inv.raw_text, inv.invoice_date,
                                             inv.due_date)]}

        return {"inventory_lookup": inventory_lookup,
                "ledger_search": ledger_search,
                "risk_analyze": risk_analyze}

    def run(self, inv: Invoice, vr: ValidationResult) -> ApprovalDecision:
        signals = self.use_tool(
            "risk.analyze",
            {"source": inv.source_file},
            lambda: analyze_risk(inv.raw_text, inv.invoice_date, inv.due_date),
        )
        for s in signals:
            self.log(f"risk signal [{s.severity}]: {s.signal} — {s.evidence}")

        draft = self._draft(inv, vr, signals)
        self.log(f"draft: {draft.decision_type.upper()}")

        critique = self.use_tool(
            "llm.critique",
            {"mode": self.llm.mode,
             "react": self.llm.mode == "grok" and draft.decision_type != "approve"},
            lambda: self.llm.critique_decision(
                inv.raw_text, inv.to_dict(), vr.to_dict(), draft.to_dict(),
                inventory_skus=self.skus,
                tools=self._critic_tools(inv)
                if self.llm.mode == "grok" and draft.decision_type != "approve"
                else None),
        )
        self.log(f"critic ({self.llm.mode}): {critique.get('verdict')} — "
                 f"{str(critique.get('notes', ''))[:140]}")
        if critique.get("trace"):
            self.tracer(self.name, "critic.trace",
                        {"steps": critique["trace"]})

        if critique.get("verdict") == "hold" and not draft.held:
            draft.held = True
            draft.approved = False
            draft.decision_type = "hold"
            draft.reasoning += f" [Held after critique: {critique.get('notes', '')}]"
            self.log("critic diverted decision to human review")
        draft.critique_notes = str(critique.get("notes", ""))
        extra = [f for f in critique.get("risk_flags", []) if f not in draft.risk_flags]
        draft.risk_flags.extend(extra)
        self.log(f"final: {draft.decision_type.upper()}")
        return draft

    def _draft(self, inv: Invoice, vr: ValidationResult,
               signals: List[RiskSignal]) -> ApprovalDecision:
        risk_flags = [f"{s.severity}:{s.signal}" for s in signals]
        total = inv.total_amount or 0.0

        # Critical fraud signals decide first: a fraudulent invoice is
        # rejected as fraud even when it also has validation errors.
        critical = [s for s in signals if s.severity == "critical"]
        if critical:
            return ApprovalDecision(
                approved=False, decision_type="reject_fraud",
                reasoning="Rejected as suspected fraud: "
                          + "; ".join(s.evidence for s in critical),
                risk_flags=risk_flags)

        # Missing fundamentals -> human, not auto-reject.
        missing = [i for i in vr.errors() if i.code in HOLD_CODES]
        if missing:
            return ApprovalDecision(
                approved=False, held=True, decision_type="hold",
                reasoning="Held for review: " + "; ".join(i.message for i in missing),
                risk_flags=risk_flags)

        errors = vr.errors()
        if errors:
            return ApprovalDecision(
                approved=False, decision_type="reject",
                reasoning="Rejected: " + "; ".join(i.message for i in errors),
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
        if total > AUTO_PAY_CEILING:
            holds.append(f"${total:,.2f} exceeds the ${AUTO_PAY_CEILING:,.0f} "
                         f"auto-pay ceiling — human sign-off required")
        if holds:
            return ApprovalDecision(
                approved=False, held=True, decision_type="hold",
                reasoning="Held for review: " + "; ".join(holds),
                risk_flags=risk_flags)

        if not inv.currency_explicit:
            risk_flags.append("info:currency_assumed_usd")
        if total > HIGH_VALUE_THRESHOLD:
            risk_flags.append("info:high_value_scrutiny")
            reasoning = (f"Approved with scrutiny: ${total:,.2f} exceeds the "
                         f"${HIGH_VALUE_THRESHOLD:,.0f} threshold, but validation is clean "
                         f"and no fraud signals were found.")
        else:
            reasoning = (f"Approved: ${total:,.2f} {inv.currency} within policy, "
                         f"validation passed, no fraud signals.")
        return ApprovalDecision(approved=True, decision_type="approve",
                                reasoning=reasoning, risk_flags=risk_flags)
