#!/usr/bin/env python3
"""Run the pipeline over every sample invoice and print a summary table."""
from __future__ import annotations

import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.orchestrator import InvoicePipeline


def main() -> None:
    paths = sorted(glob.glob("data/invoices/*"))
    pipeline = InvoicePipeline()
    rows = []
    try:
        for p in paths:
            try:
                result = pipeline.run(p)
                rows.append((os.path.basename(p), result.outcome,
                             result.invoice.vendor, result.invoice.total_amount))
            except Exception as e:  # noqa: BLE001 - demo should continue
                rows.append((os.path.basename(p), f"ERROR: {e}", "", ""))
    finally:
        pipeline.close()

    print("\n================ SUMMARY ================")
    print(f"{'file':28} {'outcome':20} {'vendor':22} {'total'}")
    for name, outcome, vendor, total in rows:
        print(f"{name:28} {outcome:20} {str(vendor)[:22]:22} {total}")


if __name__ == "__main__":
    main()
