#!/usr/bin/env python3
"""Create the mock inventory SQLite database (required setup step)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.tools import create_inventory_db

if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "inventory.db"
    create_inventory_db(db_path)
    print(f"inventory database created at {db_path}")
