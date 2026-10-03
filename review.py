#!/usr/bin/env python3
"""Human review queue for held invoices.

Held invoices (HOLD_REVIEW) need a person, not more automation. This CLI
lists them from the ledger and records approve/reject decisions back into
the same ledger, so the audit trail stays complete.

    python review.py --list [--ledger demo_ledger.jsonl]
    python review.py --approve INV-1014 --note "verified with vendor" [--ledger ...]
    python review.py --reject INV-1016 --note "SKU does not exist" [--ledger ...]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys


def read_ledger(path: str):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def cmd_list(args) -> int:
    entries = read_ledger(args.ledger)
    # A hold is "open" if no human review references its run_id yet.
    reviewed = {e.get("run_id") for e in entries if e.get("type") == "human_review"}
    open_holds = [e for e in entries
                  if e.get("outcome") == "HOLD_REVIEW" and e.get("run_id") not in reviewed]
    if not open_holds:
        print("No open held invoices.")
        return 0
    print(f"{'invoice':14} {'vendor':28} {'amount':>12}  reason")
    for e in open_holds:
        amt = e.get("amount")
        print(f"{e.get('invoice_number', '?'):14} "
              f"{str(e.get('vendor', ''))[:28]:28} "
              f"{('' if amt is None else f'{amt:,.2f}'):>12}  "
              f"{str(e.get('reasoning', ''))[:90]}")
    return 0


def cmd_decide(args, decision: str) -> int:
    if not args.note:
        print("error: --note is required (audit trail needs a reason)", file=sys.stderr)
        return 2
    entries = read_ledger(args.ledger)
    matches = [e for e in entries
               if e.get("invoice_number") == args.invoice and not e.get("type")]
    if not matches:
        print(f"error: no ledger entry for invoice {args.invoice}", file=sys.stderr)
        return 1
    target = matches[-1]
    if target.get("outcome") != "HOLD_REVIEW":
        print(f"error: invoice {args.invoice} is {target.get('outcome')}, not held",
              file=sys.stderr)
        return 1
    record = {
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "type": "human_review",
        "run_id": target.get("run_id"),
        "invoice_number": args.invoice,
        "vendor": target.get("vendor"),
        "amount": target.get("amount"),
        "currency": target.get("currency"),
        "decision": decision,
        "note": args.note,
    }
    with open(args.ledger, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    print(f"Recorded: {decision.upper()} {args.invoice} "
          f"({target.get('vendor')}, {target.get('amount')})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Human review queue for held invoices.")
    ap.add_argument("--ledger", default="demo_ledger.jsonl",
                    help="ledger file (default: demo_ledger.jsonl)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true")
    g.add_argument("--approve", metavar="INVOICE_NUMBER")
    g.add_argument("--reject", metavar="INVOICE_NUMBER")
    ap.add_argument("--note", default="")
    args = ap.parse_args()
    if args.list:
        return cmd_list(args)
    if args.approve:
        args.invoice = args.approve
        return cmd_decide(args, "approved")
    args.invoice = args.reject
    return cmd_decide(args, "rejected")


if __name__ == "__main__":
    sys.exit(main())
