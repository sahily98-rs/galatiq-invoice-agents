"""Shared data models for the invoice processing pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class LineItem:
    item: str
    quantity: int
    unit_price: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"item": self.item, "quantity": self.quantity, "unit_price": self.unit_price}


@dataclass
class Invoice:
    vendor: str
    invoice_number: str
    invoice_date: Optional[str]          # normalized ISO date or None
    due_date: Optional[str]             # normalized ISO date or None
    due_date_raw: Optional[str] = None  # as extracted, before normalization
    items: List[LineItem] = field(default_factory=list)
    total_amount: Optional[float] = None
    subtotal: Optional[float] = None
    tax_amount: Optional[float] = None
    shipping_amount: Optional[float] = None
    currency: str = "USD"
    raw_text: str = ""
    source_file: str = ""
    extraction_confidence: float = 1.0
    extraction_warnings: List[str] = field(default_factory=list)
    extraction_strategy: str = "v1"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "vendor": self.vendor,
            "invoice_number": self.invoice_number,
            "invoice_date": self.invoice_date,
            "due_date": self.due_date,
            "items": [i.to_dict() for i in self.items],
            "total_amount": self.total_amount,
            "subtotal": self.subtotal,
            "tax_amount": self.tax_amount,
            "shipping_amount": self.shipping_amount,
            "currency": self.currency,
            "extraction_confidence": self.extraction_confidence,
            "extraction_warnings": self.extraction_warnings,
            "extraction_strategy": self.extraction_strategy,
        }


@dataclass
class ValidationIssue:
    code: str
    severity: str  # "error" | "warning"
    message: str
    item: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "message": self.message, "item": self.item}


@dataclass
class ValidationResult:
    passed: bool
    issues: List[ValidationIssue] = field(default_factory=list)

    def errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    def to_dict(self) -> Dict[str, Any]:
        return {"passed": self.passed, "issues": [i.to_dict() for i in self.issues]}


@dataclass
class RiskSignal:
    signal: str
    severity: str  # "critical" | "suspicious" | "info"
    evidence: str

    def to_dict(self) -> Dict[str, Any]:
        return {"signal": self.signal, "severity": self.severity, "evidence": self.evidence}


@dataclass
class ApprovalDecision:
    """approved=True  -> pay. approved=False -> do not pay.
    held=True         -> route to human review instead of auto-deciding."""
    approved: bool
    held: bool = False
    reasoning: str = ""
    critique_notes: str = ""
    risk_flags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approved": self.approved,
            "held": self.held,
            "reasoning": self.reasoning,
            "critique_notes": self.critique_notes,
            "risk_flags": self.risk_flags,
        }


@dataclass
class PaymentResult:
    executed: bool
    vendor: str
    amount: Optional[float]
    currency: str
    status: str
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "executed": self.executed,
            "vendor": self.vendor,
            "amount": self.amount,
            "currency": self.currency,
            "status": self.status,
            "detail": self.detail,
        }


# Outcome vocabulary. PAID and REJECTED_* are terminal; HOLD_* needs a human.
OUTCOMES = (
    "PAID",
    "REJECTED_VALIDATION",
    "REJECTED_FRAUD",
    "REJECTED_DUPLICATE",
    "HOLD_REVIEW",
)


@dataclass
class PipelineResult:
    run_id: str
    invoice: Invoice
    validation: ValidationResult
    approval: ApprovalDecision
    payment: PaymentResult
    outcome: str
    outcome_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "outcome": self.outcome,
            "outcome_reason": self.outcome_reason,
            "source_file": self.invoice.source_file,
            "invoice": self.invoice.to_dict(),
            "validation": self.validation.to_dict(),
            "approval": self.approval.to_dict(),
            "payment": self.payment.to_dict(),
        }
