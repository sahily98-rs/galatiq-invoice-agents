#!/usr/bin/env python3
"""Run the pipeline over every sample invoice and print a summary table.

The demo writes to its OWN ledger (demo_ledger.jsonl) so it never touches
or deletes the production audit trail (ledger.jsonl) — and so the brief's
own example command still works afterwards.

    python demo.py [--report] [--quiet]
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.orchestrator import InvoicePipeline


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true",
                    help="also write an HTML report (report.html)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    for f in ("demo_ledger.jsonl", "demo_runs.jsonl"):
        if os.path.exists(f):
            os.remove(f)

    paths = sorted(glob.glob("data/invoices/*"))
    pipeline = InvoicePipeline(ledger_path="demo_ledger.jsonl",
                               log_path="demo_runs.jsonl",
                               quiet=args.quiet)
    results = []
    try:
        for p in paths:
            results.append(pipeline.run(p))
    finally:
        pipeline.close()

    print("\n================ SUMMARY ================")
    print(f"{'file':28} {'outcome':20} {'vendor':22} {'total'}")
    for r in results:
        d = r.to_dict()
        total = d["invoice"]["total_amount"]
        print(f"{os.path.basename(d['source_file']):28} {d['outcome']:20} "
              f"{str(d['invoice']['vendor'])[:22]:22} "
              f"{d['invoice']['currency']} {total if total is not None else 'n/a'}")

    if args.report:
        from report import write_report
        out = write_report(results)
        print(f"\nHTML report written to {out}")


if __name__ == "__main__":
    main()
