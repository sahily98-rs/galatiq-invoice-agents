#!/usr/bin/env python3
"""CLI entry point.

Usage:
    python main.py --invoice_path=data/invoices/invoice_1001.txt
    python main.py --invoice_path=data/invoices/invoice_1002.txt --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.orchestrator import InvoicePipeline


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-agent invoice processing pipeline")
    parser.add_argument("--invoice_path", required=True, help="Path to the invoice file")
    parser.add_argument("--db", default="inventory.db", help="Path to the inventory SQLite DB")
    parser.add_argument("--ledger", default="ledger.jsonl", help="Path to the JSONL ledger")
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON")
    args = parser.parse_args()

    if not os.path.exists(args.invoice_path):
        print(f"error: invoice not found: {args.invoice_path}", file=sys.stderr)
        return 1
    if not os.path.exists(args.db):
        print(f"error: inventory DB not found: {args.db} "
              f"(run: python setup_inventory.py)", file=sys.stderr)
        return 1

    pipeline = InvoicePipeline(db_path=args.db, ledger_path=args.ledger)
    try:
        result = pipeline.run(args.invoice_path)
    finally:
        pipeline.close()

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        r = result.to_dict()
        print(f"Run ID       : {r['run_id']}")
        print(f"Outcome      : {r['outcome']}")
        if r["outcome_reason"]:
            print(f"Reason       : {r['outcome_reason'][:200]}")
        print(f"Vendor       : {r['invoice']['vendor']}")
        amt = r['invoice']['total_amount']
        print(f"Total        : {r['invoice']['currency']} "
              f"{amt:,.2f}" if amt is not None else "Total        : n/a")
        print(f"Validation   : {'PASS' if r['validation']['passed'] else 'FAIL'}")
        for issue in r["validation"]["issues"]:
            print(f"  - [{issue['severity']}] {issue['code']}: {issue['message']}")
        dec = r["approval"]
        print(f"Approval     : {'APPROVED' if dec['approved'] and not dec['held'] else 'HELD' if dec['held'] else 'REJECTED'}")
        print(f"  reasoning  : {dec['reasoning'][:300]}")
        if dec["risk_flags"]:
            print(f"  risk flags : {', '.join(dec['risk_flags'])}")
        if dec["critique_notes"]:
            print(f"  critique   : {dec['critique_notes'][:200]}")
        print(f"Payment      : {r['payment']['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
