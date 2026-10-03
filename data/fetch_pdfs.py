#!/usr/bin/env python3
"""Restore the 3 sample invoice PDFs.

The PDFs are binary files and are intentionally NOT committed to the repo
(they're listed in .gitignore). This script downloads the originals from the
public challenge repository so `demo.py` and the PDF tests run fully offline
afterwards.

    python data/fetch_pdfs.py
"""
from __future__ import annotations

import os
import urllib.request

BASE = ("https://raw.githubusercontent.com/galatiq-ai/galatiq-case-invoices"
        "/main/data/invoices")
FILES = ["invoice_1011.pdf", "invoice_1012.pdf", "invoice_1013.pdf"]


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    dest = os.path.join(here, "invoices")
    os.makedirs(dest, exist_ok=True)
    for name in FILES:
        url = f"{BASE}/{name}"
        out = os.path.join(dest, name)
        print(f"fetching {url} ...")
        urllib.request.urlretrieve(url, out)
        print(f"  -> {out} ({os.path.getsize(out)} bytes)")


if __name__ == "__main__":
    main()
