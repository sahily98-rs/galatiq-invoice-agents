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
    due_date: Optional[str]
    items: List[LineItem]
    total_amount: Optional[float]
    currency: str = "USD"
    raw_text: str = ""
    source_file: str = ""
    extraction_confidence: float = 1.0
    extraction_warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "vendor": self.vendor,
            "invoice_number": self.invoice_number,
            "due_date": self.due_date,
            "items": [i.to_dict() for i in self.items],
            "total_amount": self.total_amount,
            "currency": self.currency,
            "extraction_confidence": self.extraction_confidence,
            "extraction_warnings": self.extraction_warnings,
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
class ApprovalDecision:
    approved: bool
    reasoning: str
    critique_notes: str = ""
    risk_flags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approved": self.approved,
            "reasoning": self.reasoning,
            "critique_notes": self.critique_notes,
            "risk_flags": self.risk_flags,
        }


@dataclass
class PaymentResult:
    executed: bool
    vendor: str
    amount: Optional[float]
    status: str
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "executed": self.executed,
            "vendor": self.vendor,
            "amount": self.amount,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class PipelineResult:
    invoice: Invoice
    validation: ValidationResult
    approval: ApprovalDecision
    payment: PaymentResult

    @property
    def outcome(self) -> str:
        if self.payment.executed:
            return "PAID"
        if not self.validation.passed:
            return "REJECTED_VALIDATION"
        return "REJECTED_APPROVAL"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome,
            "source_file": self.invoice.source_file,
            "invoice": self.invoice.to_dict(),
            "validation": self.validation.to_dict(),
            "approval": self.approval.to_dict(),
            "payment": self.payment.to_dict(),
        }
