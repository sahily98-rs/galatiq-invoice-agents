"""Pipeline orchestrator: ingestion -> validation -> approval -> payment."""
from __future__ import annotations

import os

from .agents.approval import ApprovalAgent
from .agents.ingestion import IngestionAgent
from .agents.payment import PaymentAgent
from .agents.validation import ValidationAgent
from .llm import LLMClient
from .models import PipelineResult
from .tools import InventoryDB


class InvoicePipeline:
    def __init__(self, db_path: str = "inventory.db", ledger_path: str = "ledger.jsonl"):
        self.db = InventoryDB(db_path)
        self.llm = LLMClient()
        self.ingestion = IngestionAgent(self.llm)
        self.validation = ValidationAgent(self.db)
        self.approval = ApprovalAgent(self.llm)
        self.payment = PaymentAgent(ledger_path)
        print(f"[pipeline] LLM engine mode: {self.llm.mode} "
              f"(set XAI_API_KEY to use Grok)")

    def run(self, invoice_path: str) -> PipelineResult:
        print(f"\n[pipeline] === processing {os.path.basename(invoice_path)} ===")
        invoice = self.ingestion.run(invoice_path)
        validation = self.validation.run(
            invoice, reextract=lambda: self.ingestion.run(invoice_path))
        approval = self.approval.run(invoice, validation)
        payment = self.payment.run(invoice, approval)
        result = PipelineResult(invoice=invoice, validation=validation,
                                approval=approval, payment=payment)
        print(f"[pipeline] === outcome: {result.outcome} ===\n")
        return result

    def close(self) -> None:
        self.db.close()
