"""Base agent: perceive -> act (tools) -> reflect, with traced tool calls."""
from __future__ import annotations

from typing import Any, Callable, Dict


class BaseAgent:
    name = "base"

    def __init__(self, tracer: Callable[[str, str, Dict[str, Any]], None] | None = None):
        # tracer(agent, tool, args) records structured tool-call events.
        self.tracer = tracer or (lambda a, t, args: None)

    def log(self, msg: str) -> None:
        print(f"[{self.name}] {msg}")

    def use_tool(self, tool: str, args: Dict[str, Any], fn: Callable[[], Any]) -> Any:
        """Run a tool with a structured trace. Exceptions propagate — the
        orchestrator converts them into HOLD outcomes, never silent drops."""
        try:
            result = fn()
            self.tracer(self.name, tool, {**args, "ok": True})
            return result
        except Exception as e:
            self.tracer(self.name, tool, {**args, "ok": False, "error": str(e)[:200]})
            raise
