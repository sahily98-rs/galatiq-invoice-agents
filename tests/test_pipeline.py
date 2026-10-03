"""Unit tests for the pipeline stages (golden end-to-end tests live in test_golden.py)."""
import os

import pytest

from src.agents.approval import ApprovalAgent
from src.agents.ingestion import IngestionAgent
from src.agents.payment import PaymentAgent
from src.agents.validation import ValidationAgent
from src.llm import LLMClient
from src.models import (ApprovalDecision, Invoice, LineItem, ValidationIssue,
                        ValidationResult)
from src.orchestrator import InvoicePipeline
from src.tools import (InventoryDB, analyze_risk, create_inventory_db,
                       mock_payment, sanitize_vendor)

DATA = os.path.join(os.path.dirname(__file__), "..", "data", "invoices")


@pytest.fixture()
def db(tmp_path):
    path = create_inventory_db(str(tmp_path / "test_inventory.db"))
    d = InventoryDB(path)
    yield d
    d.close()


@pytest.fixture()
def llm():
    return LLMClient()


def _inv(**kw):
    base = dict(vendor="Widgets Inc.", invoice_number="INV-T1",
                invoice_date="2026-01-15", due_date="2026-02-15",
                items=[LineItem("WidgetA", 2, 250.0)],
                total_amount=500.0, currency="USD")
    base.update(kw)
    return Invoice(**base)


# ------------------------------------------------------------------ ingestion

def test_ingestion_txt(llm):
    inv = IngestionAgent(llm).run(os.path.join(DATA, "invoice_1001.txt"))
    assert inv.vendor == "Widgets Inc."
    assert inv.invoice_number == "INV-1001"
    assert inv.total_amount == 5000.00
    assert len(inv.items) == 2


def test_ingestion_parenthetical_item_kept(llm):
    """'WidgetA (rush order)' must not be dropped (invoice_1010)."""
    inv = IngestionAgent(llm).run(os.path.join(DATA, "invoice_1010.txt"))
    names = [i.item for i in inv.items]
    assert any("WidgetA" in n for n in names)
    assert len(inv.items) == 4


def test_ingestion_csv_vertical_multi_item(llm):
    """Vertical CSV with two item groups keeps BOTH items."""
    inv = IngestionAgent(llm).run(os.path.join(DATA, "invoice_1006.csv"))
    assert len(inv.items) == 2
    assert {i.item for i in inv.items} == {"WidgetA", "WidgetB"}
    assert inv.total_amount == 2750.00


def test_ingestion_csv_horizontal_total(llm):
    """Horizontal CSV total comes from the Total row, never a line total."""
    inv = IngestionAgent(llm).run(os.path.join(DATA, "invoice_1015.csv"))
    assert inv.total_amount == 6500.00


def test_ingestion_xml_currency(llm):
    inv = IngestionAgent(llm).run(os.path.join(DATA, "invoice_1014.xml"))
    assert inv.currency == "EUR"


def test_vendor_sanitized():
    """Two-column PDF bleed ('... Due: 2026-03-24') is stripped from payees."""
    assert sanitize_vendor("Atlas Industrial Supply Due: 2026-03-24") == \
        "Atlas Industrial Supply"


# ----------------------------------------------------------------- validation

def test_validation_passes_clean(db):
    vr, _ = ValidationAgent(db).run(_inv())
    assert vr.passed


def test_validation_rejects_stock_mismatch(db):
    vr, _ = ValidationAgent(db).run(_inv(items=[LineItem("GadgetX", 20, 750.0)],
                                         total_amount=15000.0))
    assert any(i.code == "stock_mismatch" for i in vr.errors())


def test_validation_rejects_unknown_item_exact_match(db):
    """'A' must NOT fuzzy-match 'WidgetA'; unknown SKUs are errors."""
    vr, _ = ValidationAgent(db).run(_inv(items=[LineItem("A", 1, 9.0)],
                                         total_amount=9.0))
    assert any(i.code == "unknown_item" for i in vr.errors())


def test_validation_missing_total_is_error(db):
    vr, _ = ValidationAgent(db).run(_inv(total_amount=None))
    assert any(i.code == "missing_total" for i in vr.errors())


def test_validation_total_mismatch_is_error(db):
    vr, _ = ValidationAgent(db).run(_inv(total_amount=9999.0))
    assert any(i.code == "total_mismatch" for i in vr.errors())


def test_validation_subtotal_tax_shipping_model(db):
    inv = _inv(subtotal=6700.0, tax_amount=335.0, shipping_amount=150.0,
               total_amount=7185.0,
               items=[LineItem("WidgetA", 8, 250.0), LineItem("WidgetB", 4, 500.0),
                      LineItem("GadgetX", 2, 750.0), LineItem("WidgetA", 4, 300.0)])
    vr, _ = ValidationAgent(db).run(inv)
    assert vr.passed


# ------------------------------------------------------------------- approval

def test_approval_rejects_fraud(llm):
    signals = analyze_risk("URGENT - pay immediately. Wire transfer preferred.",
                           "2026-01-20", "2026-01-19")
    assert any(s.severity == "critical" for s in signals)
    vr = ValidationResult(
        passed=False,
        issues=[ValidationIssue("zero_stock", "error", "zero stock", item="FakeItem")])
    dec = ApprovalAgent(llm).run(
        _inv(vendor="Fraudster LLC", total_amount=100000.0,
             items=[LineItem("FakeItem", 1, 100000.0)]), vr)
    assert not dec.approved and not dec.held


def test_approval_holds_missing_vendor(llm):
    vr = ValidationResult(
        passed=False,
        issues=[ValidationIssue("missing_vendor", "error", "no vendor")])
    dec = ApprovalAgent(llm).run(_inv(vendor="", total_amount=100.0), vr)
    assert dec.held and not dec.approved


def test_risk_ignores_benign_penalty_language():
    """'late payment penalty' in normal terms is not a fraud signal."""
    signals = analyze_risk("Payment terms: Net 30. A late payment penalty of "
                           "1.5% per month applies.", "2026-01-20", "2026-02-20")
    assert not [s for s in signals if s.severity == "critical"]


def test_risk_flags_vendor_name_change():
    signals = analyze_risk("Vendor: QuickShip (formerly FastShip Ltd.)",
                           "2026-01-20", "2026-02-20")
    assert any(s.signal == "vendor_name_change" for s in signals)


def test_critic_catches_garbled_payee(llm, db):
    """The critic sees raw text and holds on a garbled payee name."""
    inv = _inv(vendor="Atlas Industrial Supply Due: 2026-03-24")
    inv.raw_text = "Vendor: Atlas Industrial Supply Due: 2026-03-24\nTotal: $100.00"
    dec = ApprovalAgent(llm, skus=db.skus()).run(inv, ValidationResult(passed=True))
    assert dec.held


# -------------------------------------------------------------------- payment

def test_payment_not_executed_when_rejected(tmp_path):
    agent = PaymentAgent(str(tmp_path / "ledger.jsonl"))
    dec = ApprovalDecision(approved=False, reasoning="nope")
    res = agent.run("r1", _inv(), dec, "REJECTED_VALIDATION")
    assert not res.executed and res.status == "rejected"


def test_mock_payment():
    assert mock_payment("Acme", 10.0)["status"] == "success"


# ----------------------------------------------------------------- pipeline

def test_pipeline_end_to_end(tmp_path):
    db_path = create_inventory_db(str(tmp_path / "inv.db"))
    pipe = InvoicePipeline(db_path=db_path,
                           ledger_path=str(tmp_path / "ledger.jsonl"),
                           log_path=str(tmp_path / "runs.jsonl"), quiet=True)
    try:
        r = pipe.run(os.path.join(DATA, "invoice_1001.txt"))
        assert r.outcome == "PAID"
        assert r.payment.executed
    finally:
        pipe.close()
