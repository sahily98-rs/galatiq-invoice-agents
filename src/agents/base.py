"""Base agent with structured logging."""
from __future__ import annotations

import datetime


class BaseAgent:
    name = "base"

    def log(self, msg: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [{self.name}] {msg}")
