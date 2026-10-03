"""Golden-file tests: expected outcome + amount for every sample invoice.

These assert what the pipeline SHOULD do (reviewed by hand), not just what
the code happens to do. The shared-ledger run is order-dependent by design:
duplicate invoice numbers (1004/1004_revised, 1011.pdf/1011.txt) are only
caught because the ledger persists across invoices in one run — exactly how
the production idempotency guard works.
"""
import glob
import json
import os

import pytest

from src.orchestrator import InvoicePipeline
from src.tools import create_inventory_db

DATA = os.path.join(os.path.dirname(__file__), "..", "data", "invoices")
GOLDEN = json.load(open(os.path.join(os.path.dirname(__file__), "golden.json")))


@pytest.fixture()
def pipeline(tmp_path):
    db = create_inventory_db(str(tmp_path / "inv.db"))
    pipe = InvoicePipeline(db_path=db,
                           ledger_path=str(tmp_path / "ledger.jsonl"),
                           log_path=str(tmp_path / "runs.jsonl"),
                           quiet=True)
    yield pipe
    pipe.close()


def test_golden_outcomes(pipeline):
    """Every sample invoice produces its reviewed expected outcome."""
    paths = sorted(glob.glob(os.path.join(DATA, "*")))
    missing = sorted(set(GOLDEN) - {os.path.basename(p) for p in paths})
    assert not missing, f"missing sample files — run python data/fetch_pdfs.py: {missing}"
    for p in paths:
        name = os.path.basename(p)
        r = pipeline.run(p).to_dict()
        exp = GOLDEN[name]
        assert r["outcome"] == exp["outcome"], f"{name}: outcome"
        assert r["invoice"]["vendor"] == exp["vendor"], f"{name}: vendor"
        assert r["invoice"]["currency"] == exp["currency"], f"{name}: currency"
        assert r["invoice"]["total_amount"] == pytest.approx(exp["total"]), \
            f"{name}: total"


def test_duplicate_invoice_rejected(pipeline, tmp_path):
    """Paying the same invoice number twice is refused (1004 pair)."""
    r1 = pipeline.run(os.path.join(DATA, "invoice_1004.json"))
    r2 = pipeline.run(os.path.join(DATA, "invoice_1004_revised.json"))
    assert r1.outcome == "PAID"
    assert r2.outcome == "REJECTED_DUPLICATE"


def test_ledger_records_every_invoice(pipeline, tmp_path):
    """Paid, rejected, and held invoices all land in the ledger with an
    invoice number and run ID — nothing disappears from the audit trail."""
    for name in ["invoice_1001.txt", "invoice_1002.txt", "invoice_1014.xml"]:
        pipeline.run(os.path.join(DATA, name))
    entries = [json.loads(l) for l in
               open(tmp_path / "ledger.jsonl", encoding="utf-8")]
    assert len(entries) == 3
    for e in entries:
        assert e["run_id"] and e["invoice_number"] and e["outcome"]


def test_crash_becomes_hold_not_traceback(pipeline, tmp_path):
    """A malformed document routes to HOLD_REVIEW with a ledger entry."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    r = pipeline.run(str(bad))
    assert r.outcome == "HOLD_REVIEW"
    entries = [json.loads(l) for l in
               open(tmp_path / "ledger.jsonl", encoding="utf-8")]
    assert entries and entries[-1]["outcome"] == "HOLD_REVIEW"


def test_inflated_total_is_rejected(pipeline, tmp_path):
    """Overbilling: stated total far above line items must not be paid."""
    inv = tmp_path / "inflated.json"
    inv.write_text(json.dumps({
        "vendor": "Widgets Inc.", "invoice_number": "INV-EDGE-1",
        "date": "2026-02-01", "due_date": "2026-03-01",
        "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 250.0}],
        "total": 9999.0}), encoding="utf-8")
    r = pipeline.run(str(inv))
    assert r.outcome == "REJECTED_VALIDATION"
    assert any(i.code == "total_mismatch" for i in r.validation.errors())


def test_per_sku_aggregation(pipeline, tmp_path):
    """Stock is checked per SKU across lines, not per line."""
    inv = tmp_path / "agg.json"
    inv.write_text(json.dumps({
        "vendor": "Widgets Inc.", "invoice_number": "INV-EDGE-2",
        "date": "2026-02-01", "due_date": "2026-03-01",
        "line_items": [
            {"item": "WidgetA", "quantity": 10, "unit_price": 250.0},
            {"item": "WidgetA", "quantity": 10, "unit_price": 250.0}],
        "total": 5000.0}), encoding="utf-8")
    r = pipeline.run(str(inv))
    assert r.outcome == "REJECTED_VALIDATION"
    assert any(i.code == "stock_mismatch" for i in r.validation.errors())
