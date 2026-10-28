"""What a run reports (unified-execution-core D3): journal events to sinks, and one result.

A sink is any object with `journal(event, details)`. The engine sends every
journal event to each sink in order; it never prints itself.

  ConsoleSink  today's stderr line per event ("[HH:MM:SS] PREFLIGHT OK"), through
               the executor's own progress() so its format is unchanged
  JournalSink  execution_logs/<id>.jsonl: one JSON line per event, fsynced
               before the next step runs (unchanged format, D2)

Numeric `progress` events (bytes hashed, file n of m) are not here yet: the
hashing code that would emit them is shipped to the NAS byte for byte and
changes only with its own D8 review (see "Recorded by gate 3").
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ExecutionResult:
    status: str                 # SUCCESS, CHECK_ONLY or STOPPED
    execution_id: str
    last_event: Optional[str]
    error_type: Optional[str]
    reason: Optional[str]
    journal_path: Path


class ConsoleSink:
    def __init__(self, progress):
        self.progress = progress

    def journal(self, event, details):
        self.progress(event.replace('_', ' '))


class JournalSink:
    def __init__(self, stream, execution_id):
        self.stream = stream
        self.execution_id = execution_id

    def journal(self, event, details):
        self.stream.write(json.dumps(dict(utc=datetime.now(timezone.utc).isoformat(),
                                          execution_id=self.execution_id, event=event, **details)) + '\n')
        self.stream.flush()
        os.fsync(self.stream.fileno())


class Log:
    """The `log(event, **details)` callable strategies write to; fans out to the sinks."""

    def __init__(self, sinks):
        self.sinks = list(sinks)
        self.last_event = None

    def __call__(self, event, **details):
        for sink in self.sinks:
            sink.journal(event, details)
        self.last_event = event
