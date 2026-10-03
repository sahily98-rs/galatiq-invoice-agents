"""Smoke tests for the invoice pipeline (run: python -m pytest tests/ -q)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from src.orchestrator import InvoicePipeline

DB = os.path.join(os.path.dirname(__file__), "..", "test_inventory.db")
DATA = os.path.join(os.path.dirname(__file__), "..", "data", "invoices")


@pytest.fixture(scope="module")
def pipeline():
    from src.tools import create_inventory_db

    create_inventory_db(DB)
    p = InvoicePipeline(db_path=DB, ledger_path=os.path.join(os.path.dirname(__file__), "test_ledger.jsonl"))
    yield p
    p.close()


def inv(name):
    return os.path.join(DATA, name)


def test_clean_invoice_is_paid(pipeline):
    r = pipeline.run(inv("invoice_1001.txt"))
    assert r.outcome == "PAID"
    assert r.invoice.vendor == "Widgets Inc."
    assert r.invoice.total_amount == 5000.0


def test_stock_mismatch_rejected(pipeline):
    r = pipeline.run(inv("invoice_1002.txt"))
    assert r.outcome == "REJECTED_VALIDATION"
    assert any(i.code == "stock_mismatch" for i in r.validation.issues)


def test_fraudulent_invoice_rejected(pipeline):
    r = pipeline.run(inv("invoice_1003.txt"))
    assert r.outcome == "REJECTED_VALIDATION"
    assert any(i.code == "zero_stock" for i in r.validation.issues)
    assert any("fraud_signals" in f for f in r.approval.risk_flags)


def test_unknown_items_rejected(pipeline):
    r = pipeline.run(inv("invoice_1008.txt"))
    assert r.outcome == "REJECTED_VALIDATION"
    assert any(i.code == "unknown_item" for i in r.validation.issues)


def test_negative_quantity_rejected(pipeline):
    r = pipeline.run(inv("invoice_1009.json"))
    assert r.outcome == "REJECTED_VALIDATION"
    assert any(i.code == "negative_quantity" for i in r.validation.issues)


def test_ocr_invoice_recovers(pipeline):
    r = pipeline.run(inv("invoice_1012.txt"))
    assert r.outcome == "PAID"
    assert r.invoice.vendor == "QuickShip Distributers"
    assert len(r.invoice.items) == 3


def test_pdf_ingestion(pipeline):
    r = pipeline.run(inv("invoice_1011.pdf"))
    assert r.outcome == "PAID"
    assert len(r.invoice.items) == 2


def test_csv_vertical_layout(pipeline):
    r = pipeline.run(inv("invoice_1006.csv"))
    assert r.invoice.vendor == "Acme Industrial Supplies"
    assert len(r.invoice.items) == 1


def test_high_value_clean_invoice_approved_with_scrutiny(pipeline):
    r = pipeline.run(inv("invoice_1013.json"))
    assert r.outcome == "PAID"
    assert "high_value" in r.approval.risk_flags
