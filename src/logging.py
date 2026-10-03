"""Structured logging: every run emits JSONL events for auditability.

Console output stays human-readable; the machine-readable trail goes to
runs.jsonl (one event per line): pipeline start/end, stage boundaries with
durations, tool calls, and the final outcome.
"""
from __future__ import annotations

import datetime
import json
import time
import uuid
from typing import Any, Dict, Optional


class RunLogger:
    def __init__(self, path: str = "runs.jsonl", run_id: Optional[str] = None,
                 quiet: bool = False):
        self.path = path
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.quiet = quiet
        self._fh = open(path, "a", encoding="utf-8")
        self._stage_start: Dict[str, float] = {}

    def _emit(self, event: str, **data: Any) -> None:
        record = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "event": event,
            **data,
        }
        self._fh.write(json.dumps(record) + "\n")
        self._fh.flush()

    # -- human console -----------------------------------------------------
    def say(self, msg: str) -> None:
        if not self.quiet:
            print(msg)

    # -- structured events -------------------------------------------------
    def pipeline_start(self, source_file: str) -> None:
        self._emit("pipeline_start", source_file=source_file)

    def stage_start(self, stage: str) -> None:
        self._stage_start[stage] = time.time()
        self._emit("stage_start", stage=stage)

    def stage_end(self, stage: str, **data: Any) -> None:
        dur = time.time() - self._stage_start.pop(stage, time.time())
        self._emit("stage_end", stage=stage, duration_s=round(dur, 3), **data)

    def tool_call(self, agent: str, tool: str, args: Dict[str, Any],
                  ok: bool = True, note: str = "") -> None:
        self._emit("tool_call", agent=agent, tool=tool, args=args, ok=ok, note=note)

    def outcome(self, outcome: str, reason: str = "") -> None:
        self._emit("pipeline_end", outcome=outcome, reason=reason)

    def close(self) -> None:
        self._fh.close()
