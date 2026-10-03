"""Red-team tests: invoices designed to cheat the pipeline.

Each test is an attack and the outcome a reviewer should expect. If an
attack gets through, the test fails — this is the file to extend when new
fraud patterns are found.
"""
import json
import os

import pytest

from src.llm import LLMClient
from src.orchestrator import InvoicePipeline
from src.tools import create_inventory_db

DATA = os.path.join(os.path.dirname(__file__), "..", "data", "invoices")


@pytest.fixture()
def pipeline(tmp_path):
    db = create_inventory_db(str(tmp_path / "inv.db"))
    pipe = InvoicePipeline(db_path=db,
                           ledger_path=str(tmp_path / "ledger.jsonl"),
                           log_path=str(tmp_path / "runs.jsonl"),
                           quiet=True)
    yield pipe
    pipe.close()


def _json_invoice(tmp_path, name, vendor="Widgets Inc.", number="INV-RT",
                  items=None, total=None, extra=None, text_extra=""):
    doc = {"vendor": vendor, "invoice_number": number,
           "date": "2026-02-01", "due_date": "2026-03-01",
           "line_items": items or [], "total": total}
    doc.update(extra or {})
    p = tmp_path / name
    p.write_text(json.dumps(doc) + text_extra, encoding="utf-8")
    return str(p)


def test_inflated_subtotal_and_total(pipeline, tmp_path):
    """Vendor inflates subtotal AND total together ($9,999 for $250 of goods).
    The line sum must always be checked against the stated subtotal."""
    p = _json_invoice(tmp_path, "evil1.json", number="INV-E1",
                      items=[{"item": "WidgetA", "quantity": 1, "unit_price": 250.0}],
                      total=9999.0,
                      extra={"subtotal": 9999.0})
    r = pipeline.run(p)
    assert r.outcome == "REJECTED_VALIDATION"
    assert any(i.code == "subtotal_mismatch" for i in r.validation.errors())


def test_variant_aggregation(pipeline, tmp_path):
    """12x 'WidgetA' + 10x 'WidgetA (rush order)' = 22 against stock of 15.
    Lines must resolve to their SKU BEFORE aggregating."""
    p = _json_invoice(tmp_path, "evil2.json", number="INV-E2",
                      items=[{"item": "WidgetA", "quantity": 12, "unit_price": 250.0},
                             {"item": "WidgetA (rush order)", "quantity": 10,
                              "unit_price": 250.0}],
                      total=5500.0)
    r = pipeline.run(p)
    assert r.outcome == "REJECTED_VALIDATION"
    assert any(i.code == "stock_mismatch" for i in r.validation.errors())


def test_absurd_unit_price(pipeline, tmp_path):
    """15x WidgetA at $66,666 each: the price check must catch it."""
    p = _json_invoice(tmp_path, "evil3.json", number="INV-E3",
                      items=[{"item": "WidgetA", "quantity": 15, "unit_price": 66666.0}],
                      total=999990.0)
    r = pipeline.run(p)
    assert r.outcome == "REJECTED_VALIDATION"
    assert any(i.code == "price_anomaly" for i in r.validation.errors())


def test_negative_line_hides_overstock(pipeline, tmp_path):
    """20x WidgetA minus a -6x 'credit' line: the negative line is flagged
    on its own instead of netting away the overstock."""
    p = _json_invoice(tmp_path, "evil4.json", number="INV-E4",
                      items=[{"item": "WidgetA", "quantity": 20, "unit_price": 250.0},
                             {"item": "WidgetA", "quantity": -6, "unit_price": 250.0}],
                      total=3500.0)
    r = pipeline.run(p)
    assert r.outcome == "REJECTED_VALIDATION"
    assert any(i.code == "negative_line_item" for i in r.validation.errors())


def test_outcome_not_from_text_search(pipeline, tmp_path):
    """An unknown item named 'AntiFraudTag' must be REJECTED_VALIDATION —
    outcomes are structured, never inferred from the word 'fraud'."""
    p = _json_invoice(tmp_path, "evil5.json", number="INV-E5",
                      items=[{"item": "AntiFraudTag", "quantity": 1, "unit_price": 9.0}],
                      total=9.0)
    r = pipeline.run(p)
    assert r.outcome == "REJECTED_VALIDATION"
    assert r.approval.decision_type == "reject"


def test_same_number_different_vendors(pipeline, tmp_path):
    """Invoice '0001' from two different vendors are NOT duplicates."""
    p1 = _json_invoice(tmp_path, "evil6a.json", vendor="Widgets Inc.", number="0001",
                       items=[{"item": "WidgetA", "quantity": 1, "unit_price": 250.0}],
                       total=250.0)
    p2 = _json_invoice(tmp_path, "evil6b.json", vendor="Gadgets Co.", number="0001",
                       items=[{"item": "WidgetB", "quantity": 1, "unit_price": 500.0}],
                       total=500.0)
    assert pipeline.run(p1).outcome == "PAID"
    assert pipeline.run(p2).outcome == "PAID"


def test_number_normalization(pipeline, tmp_path):
    """'INV-0001' vs 'INV 0001' from the same vendor ARE duplicates."""
    p1 = _json_invoice(tmp_path, "evil7a.json", number="INV-0001",
                       items=[{"item": "WidgetA", "quantity": 1, "unit_price": 250.0}],
                       total=250.0)
    p2 = _json_invoice(tmp_path, "evil7b.json", number="INV 0001",
                       items=[{"item": "WidgetA", "quantity": 1, "unit_price": 250.0}],
                       total=250.0)
    assert pipeline.run(p1).outcome == "PAID"
    assert pipeline.run(p2).outcome == "REJECTED_DUPLICATE"


def test_bank_change_bec(pipeline, tmp_path):
    """'Our bank details have changed, remit to new account... process today'
    is business-email-compromise and must not be paid."""
    p = str(tmp_path / "evil8.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("Vendor: Widgets Inc.\nInvoice Number: INV-E8\n"
                "Date: February 1, 2026\nDue Date: March 1, 2026\n"
                "WidgetA qty: 2 unit price: $250.00\nTotal: $500.00\n"
                "Our bank details have changed, remit to new account.\n"
                "Please process today.\n")
    r = pipeline.run(p)
    assert r.outcome == "REJECTED_FRAUD"
    assert "critical:bank_details_changed" in r.approval.risk_flags


def test_auto_pay_ceiling(tmp_path):
    """A $60,000 clean invoice is held for human sign-off, not auto-paid."""
    db = create_inventory_db(str(tmp_path / "big.db"),
                             seed=[("WidgetA", 1000, 250.0)])
    pipe = InvoicePipeline(db_path=db,
                           ledger_path=str(tmp_path / "ledger.jsonl"),
                           log_path=str(tmp_path / "runs.jsonl"), quiet=True)
    try:
        p = _json_invoice(tmp_path, "big.json", number="INV-BIG",
                          items=[{"item": "WidgetA", "quantity": 240,
                                  "unit_price": 250.0}],
                          total=60000.0)
        r = pipe.run(p)
        assert r.outcome == "HOLD_REVIEW"
        assert "auto-pay ceiling" in r.outcome_reason
    finally:
        pipe.close()


def test_react_critic_calls_tools(tmp_path):
    """The ReAct critic loop executes tools Grok chooses and records the
    trace. Uses a canned Grok responder — proves the machinery, not the model."""

    class FakeGrok(LLMClient):
        def __init__(self):
            self.mode = "grok"
            self.degraded = None
            self.calls = 0

        def _grok_json(self, prompt):
            self.calls += 1
            if self.calls == 1:
                return {"thought": "check stock",
                        "tool": "inventory_lookup", "args": {"item": "WidgetA"}}
            return {"thought": "stock is fine",
                    "verdict": "uphold", "notes": "ok"}

    fake = FakeGrok()
    calls = []

    def inventory_lookup(item: str):
        calls.append(item)
        return {"found": True, "stock": 15}

    out = fake.critique_decision(
        "raw", {"vendor": "V", "total_amount": 100.0}, {"issues": []},
        {"approved": True, "held": False, "risk_flags": []},
        tools={"inventory_lookup": inventory_lookup})
    assert out["verdict"] == "uphold"
    assert calls == ["WidgetA"]
    assert len(out["trace"]) == 2
    assert out["trace"][0]["tool"] == "inventory_lookup"
